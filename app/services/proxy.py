import json
import logging
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models.lorebook import Lorebook
from app.services.cantrip import process_cantrips
from app.services.lorebook import inject_entries, match_entries
from app.services.routing import resolve_routing
from app.services.verification import run_verification_loop

logger = logging.getLogger(__name__)

FORWARD_HEADERS = {"content-type", "accept"}
SKIP_HEADERS = {"host", "authorization", "content-length", "transfer-encoding"}


def _build_forward_headers(request: Request, api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in SKIP_HEADERS:
            continue
        if lower in FORWARD_HEADERS:
            headers[key] = value
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_upstream_url(base_url: str, path: str, api_base_path: str = "") -> str:
    if api_base_path:
        if api_base_path.endswith("/chat/completions"):
            if path == "/v1/chat/completions":
                path = api_base_path
            elif path.startswith("/v1/"):
                suffix = path[3:]
                base = api_base_path[: -len("/chat/completions")]
                path = base + suffix
            elif path == "/v1":
                path = api_base_path[: -len("/chat/completions")]
        elif path.startswith("/v1/"):
            path = api_base_path + path[3:]
        elif path == "/v1":
            path = api_base_path
    return f"{base_url}{path}"


def _log_request(method: str, url: str, model: str | None, stream: bool) -> None:
    logger.info("proxy %s %s model=%s stream=%s", method, url, model, stream)


def _log_response(status_code: int, elapsed: float) -> None:
    logger.info("proxy response status=%d elapsed=%.2fs", status_code, elapsed)


async def _resolve_target(request: Request) -> tuple[str, str, str | None, str] | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        if token.startswith("gitv_"):
            async with async_session() as db:
                result = await resolve_routing(token, db)
                if result:
                    return result.base_url, result.api_key, result.user_id, result.api_base_path
                return None
            return None

    base_url = settings.default_endpoint_url
    api_key = settings.default_endpoint_api_key
    api_base_path = settings.default_endpoint_api_base_path
    if base_url:
        return base_url, api_key, None, api_base_path
    return None


async def forward_request(request: Request) -> JSONResponse | StreamingResponse:
    target = await _resolve_target(request)

    if target is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "No endpoint configured for this request",
                    "type": "proxy_error",
                }
            },
        )

    base_url, api_key, user_id, api_base_path = target

    body = await request.body()
    path = request.url.path
    query = request.url.query
    upstream_url = _build_upstream_url(base_url, path, api_base_path)
    if query:
        upstream_url = f"{upstream_url}?{query}"

    headers = _build_forward_headers(request, api_key)

    body_json: dict[str, Any] = {}
    if body:
        try:
            body_json = json.loads(body)
        except Exception:
            pass

    model = body_json.get("model")
    stream = body_json.get("stream", False)
    _log_request(request.method, upstream_url, model, stream)

    request_headers: dict[str, str] = {}
    if user_id and "messages" in body_json:
        request_headers = {k.lower(): v for k, v in request.headers.items()}

        from app.services.tagging import extract_all_tags_from_messages, strip_tags
        tags = extract_all_tags_from_messages(body_json.get("messages", []))
        if tags:
            logger.info("Tags detected: %s", [t["raw"] for t in tags])
            for msg in body_json["messages"]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    msg["content"] = strip_tags(content)

        body_json = await _apply_lorebook_injection(body_json, user_id, tags)
        body_json = await process_cantrips(body_json, user_id, request_headers, tags)
        body = json.dumps(body_json).encode()

    timeout = httpx.Timeout(settings.request_timeout, connect=10.0)

    if stream and user_id:
        from app.services.verification import is_verification_enabled
        try:
            verification_on = await is_verification_enabled(user_id)
        except Exception:
            verification_on = False

        if verification_on:
            ux_settings = await _load_ux_settings(user_id)
            body_json_verified = dict(body_json)
            body_json_verified["stream"] = False
            body_verified = json.dumps(body_json_verified).encode()
            logger.info("Verification enabled: converting streaming request to non-streaming for verification")
            response = await _forward_non_streaming_verified(
                request.method, upstream_url, headers, body_verified, body_json,
                timeout, user_id, request_headers,
            )
            if response.status_code == 200 and stream:
                response = _convert_to_sse(
                    response, model or body_json.get("model", ""),
                    gitv_status=ux_settings.get("gitv_status", False),
                    simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
                )
            return response

    if stream:
        return await _forward_streaming(request.method, upstream_url, headers, body, timeout)

    return await _forward_non_streaming_verified(
        request.method, upstream_url, headers, body, body_json, timeout, user_id, request_headers
    )


async def _load_ux_settings(user_id: str) -> dict[str, Any]:
    from app.models.user_settings import UserSettings
    try:
        async with async_session() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            s = result.scalar_one_or_none()
            if s:
                return {
                    "gitv_status": s.gitv_status,
                    "simulated_streaming_speed": s.simulated_streaming_speed,
                    "preserve_thinking": s.preserve_thinking,
                }
    except Exception:
        pass
    return {"gitv_status": False, "simulated_streaming_speed": 0, "preserve_thinking": True}


def _convert_to_sse(
    response: JSONResponse,
    model: str,
    gitv_status: bool = False,
    simulated_speed: int = 0,
) -> StreamingResponse:
    import time

    data = json.loads(response.body)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    chunk_id = data.get("id", "chatcmpl-converted")
    created = data.get("created", 0)

    def make_chunk(delta_content: str, finish: str | None = None) -> str:
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": delta_content}, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def generate():
        if gitv_status:
            status_text = "<think>\n<gitv>\nVerification complete. Streaming response to client.\n</gitv>\n</think>\n"
            yield make_chunk(status_text)

        if simulated_speed > 0 and content:
            words = content.split(" ")
            for i in range(len(words)):
                if i == 0:
                    yield make_chunk(words[i])
                else:
                    yield make_chunk(" " + words[i])
                time.sleep(60.0 / simulated_speed / 10)
        elif content:
            yield make_chunk(content)

        yield make_chunk("", finish="stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _apply_lorebook_injection(
    body_json: dict[str, Any], user_id: str, tags: list | None = None
) -> dict[str, Any]:
    try:
        from app.services.tagging import should_activate_resource

        async with async_session() as db:
            result = await db.execute(
                select(Lorebook)
                .where(Lorebook.user_id == user_id)
                .options(selectinload(Lorebook.entries))
            )
            lorebooks = result.scalars().all()

            active_lorebooks = []
            for lb in lorebooks:
                if lb.tag and tags:
                    activate = should_activate_resource(
                        lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, user_id, tags
                    )
                    if activate:
                        active_lorebooks.append(lb)
                elif lb.is_active:
                    active_lorebooks.append(lb)

            all_entries: list[dict] = []
            for lb in active_lorebooks:
                for entry in lb.entries:
                    all_entries.append(
                        {
                            "name": entry.name,
                            "keys": entry.keys,
                            "secondary_keys": entry.secondary_keys,
                            "content": entry.content,
                            "position": entry.position,
                            "insertion_order": entry.insertion_order,
                            "is_constant": entry.is_constant,
                            "is_selective": entry.is_selective,
                            "is_disabled": entry.is_disabled,
                            "character_limit": entry.character_limit,
                        }
                    )

            if not all_entries:
                return body_json

            messages = body_json.get("messages", [])
            matched = match_entries(messages, all_entries)
            if matched:
                body_json["messages"] = inject_entries(messages, matched)
                logger.info(
                    "Lorebook injection: %d entries matched for user %s", len(matched), user_id
                )

            return body_json
    except Exception:
        logger.exception("Lorebook injection failed, forwarding original request")
        return body_json


async def _forward_non_streaming(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout
) -> JSONResponse:
    response_data, status_code = await _do_forward(method, url, headers, body, timeout)
    return JSONResponse(status_code=status_code, content=response_data)


async def _forward_non_streaming_verified(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    body_json: dict[str, Any],
    timeout: httpx.Timeout,
    user_id: str | None,
    request_headers: dict[str, str],
) -> JSONResponse:
    response_data, status_code = await _do_forward(method, url, headers, body, timeout)

    if status_code == 200 and user_id:
        conversation_id = request_headers.get("x-conversation-id", "")
        try:
            response_data, vresult = await run_verification_loop(
                response_data, body_json, method, url, headers, timeout,
                user_id, conversation_id,
            )
            if vresult:
                logger.info(
                    "Verification: approved=%s retries=%d violations_total=%d",
                    vresult.approved,
                    vresult.retries_used,
                    sum(len(c.violations) for c in vresult.check_history),
                )
        except Exception:
            logger.exception("Verification loop failed, returning original response")

    return JSONResponse(status_code=status_code, content=response_data)


async def _do_forward(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout
) -> tuple[dict[str, Any], int]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.request(method, url, headers=headers, content=body)
        except httpx.ConnectError:
            logger.error("proxy connection error: %s", url)
            return (
                {"error": {"message": "Failed to connect to upstream endpoint", "type": "proxy_error"}},
                502,
            )
        except httpx.TimeoutException:
            logger.error("proxy timeout: %s", url)
            return (
                {"error": {"message": "Upstream endpoint timed out", "type": "proxy_error"}},
                504,
            )

    _log_response(response.status_code, response.elapsed.total_seconds())

    try:
        return response.json(), response.status_code
    except Exception:
        return (
            {"error": {"message": response.text, "type": "upstream_error"}},
            response.status_code,
        )


async def _forward_streaming(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout
) -> StreamingResponse:
    client = httpx.AsyncClient(timeout=timeout)

    async def stream_generator():
        try:
            async with client.stream(method, url, headers=headers, content=body) as response:
                _log_response(response.status_code, 0)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.ConnectError:
            logger.error("proxy streaming connection error: %s", url)
            yield (
                b'data: {"error":{"message":"Failed to connect to upstream endpoint",'
                b'"type":"proxy_error"}}\n\n'
            )
        except httpx.TimeoutException:
            logger.error("proxy streaming timeout: %s", url)
            yield (
                b'data: {"error":{"message":"Upstream endpoint timed out",'
                b'"type":"proxy_error"}}\n\n'
            )
        finally:
            await client.aclose()

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
