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
    logger.debug("proxy headers: api_key_present=%s", "<redacted>" if "sk-" in str(url) else "check")


def _log_response(status_code: int, elapsed: float, response_body: str = "") -> None:
    logger.info("proxy response status=%d elapsed=%.2fs", status_code, elapsed)
    if status_code >= 400 and response_body:
        logger.debug("proxy error response body: %.500s", response_body[:500])


async def _resolve_target(request: Request) -> tuple[str, str, str | None, str, str, str, str, str] | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        if token.startswith("gitv_"):
            async with async_session() as db:
                result = await resolve_routing(token, db)
                if result:
                    return result.base_url, result.api_key, result.user_id, result.api_base_path, result.bypass_method, result.provider, result.model, result.endpoint_id
                return None
            return None

    base_url = settings.default_endpoint_url
    api_key = settings.default_endpoint_api_key
    api_base_path = settings.default_endpoint_api_base_path
    if base_url:
        return base_url, api_key, None, api_base_path, "none", "", "", ""
    return None


async def forward_request(request: Request) -> JSONResponse | StreamingResponse:
    try:
        return await _forward_request_impl(request)
    except Exception as exc:
        logger.exception("Unhandled error in proxy pipeline")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"Internal proxy error: {type(exc).__name__}: {exc}",
                    "type": "proxy_error",
                }
            },
        )


async def _forward_request_impl(request: Request) -> JSONResponse | StreamingResponse:
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

    base_url, api_key, user_id, api_base_path, endpoint_bypass, provider, endpoint_model, endpoint_id = target

    body = await request.body()
    path = request.url.path
    query = request.url.query
    upstream_url = _build_upstream_url(base_url, path, api_base_path)
    if query:
        upstream_url = f"{upstream_url}?{query}"

    headers = _build_forward_headers(request, api_key)

    logger.debug(
        "proxy resolved: base_url=%s path=%s api_base_path=%s upstream_url=%s api_key_present=%s provider=%s",
        base_url, path, api_base_path, upstream_url, bool(api_key), provider or "(none)",
    )

    body_json: dict[str, Any] = {}
    if body:
        try:
            body_json = json.loads(body)
        except Exception:
            pass

    model = body_json.get("model")
    if not model and endpoint_model:
        body_json["model"] = endpoint_model
        model = endpoint_model
    stream = body_json.get("stream", False)

    body_json["_gitv_provider"] = provider
    body_json["_gitv_base_url"] = base_url
    body_json["_gitv_api_key"] = api_key

    _log_request(request.method, upstream_url, model, stream)

    request_headers: dict[str, str] = {}
    if user_id and "messages" in body_json:
        request_headers = {k.lower(): v for k, v in request.headers.items()}

        from app.services.command_tags import (
            extract_command_tags_from_messages,
            strip_command_tags_from_messages,
        )
        from app.services.jslorebook import extract_from_messages as extract_jslorebook
        from app.services.tagging import extract_all_tags_from_messages, strip_tags

        tags = extract_all_tags_from_messages(body_json.get("messages", []))
        if tags:
            logger.info("Tags detected: %s", [t["raw"] for t in tags])
            for msg in body_json["messages"]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    msg["content"] = strip_tags(content)

        jslb_scripts, cleaned_msgs = extract_jslorebook(body_json.get("messages", []))
        if jslb_scripts:
            logger.info("jslorebook: %d embedded script(s) extracted", len(jslb_scripts))
            body_json["messages"] = cleaned_msgs
            body_json["_gitv_jslb_scripts"] = jslb_scripts

        message_commands = extract_command_tags_from_messages(body_json.get("messages", []))
        if message_commands:
            logger.info("Command tags detected: %s", [
                f"{c.command}:{c.setting}" + (":persist" if c.persist else "") for c in message_commands
            ])
            body_json["messages"] = strip_command_tags_from_messages(body_json["messages"])

        body_json["_gitv_tags"] = tags

        from app.services.debug import debug_capture, init_debug, is_debug_mode
        debug_on = await is_debug_mode(user_id)
        if debug_on:
            init_debug(body_json, tags)

        from app.services.conversation import (
            load_memories_for_chat,
            resolve_conversation,
        )
        from app.services.memory import build_memory_context_block as _build_mem_block

        messages_for_conv = body_json.get("messages", [])
        internal_chat_id, is_new_conv = await resolve_conversation(messages_for_conv, user_id)

        from app.services.command_tags import resolve_command_overrides
        command_overrides = await resolve_command_overrides(
            user_id, internal_chat_id, message_commands
        )
        body_json["_gitv_command_overrides"] = command_overrides.to_dict()

        cmd_overrides_mem = body_json.get("_gitv_command_overrides", {})
        memory_disabled = cmd_overrides_mem.get("memory") is False
        memories = {} if memory_disabled else await load_memories_for_chat(internal_chat_id, user_id)
        if memories:
            memory_block = _build_mem_block(memories)
            if memory_block:
                msgs = body_json["messages"]
                system_idx = None
                for i, msg in enumerate(msgs):
                    if msg.get("role") == "system":
                        system_idx = i
                        break
                if system_idx is not None:
                    msgs[system_idx] = {
                        **msgs[system_idx],
                        "content": msgs[system_idx]["content"] + "\n\n" + memory_block,
                    }
                else:
                    msgs.insert(0, {"role": "system", "content": memory_block})
                logger.info("Memory injection: %d keys for chat %s", len(memories), internal_chat_id[:12])
                if debug_on:
                    debug_capture(body_json, "memory_injection", "Memory Injection",
                        detail=f"Injected {len(memories)} memories for chat {internal_chat_id[:12]}",
                        setting="memory_enabled", setting_value=not memory_disabled,
                        metadata={"memory_keys": list(memories.keys())})
        elif debug_on:
            debug_capture(body_json, "memory_injection", "Memory Injection",
                detail="No memories found" if not memory_disabled else "Disabled by command override",
                setting="memory_enabled", setting_value=not memory_disabled)

        if user_id and command_overrides.to_dict().get("summary") is not False:
            from app.services.scenario_summarizer import maybe_summarize_scenario
            pre_summary_msgs = json.dumps(body_json.get("messages", []), default=str)
            body_json = await maybe_summarize_scenario(body_json, user_id, "pre")
            if debug_on:
                did_change = json.dumps(body_json.get("messages", []), default=str) != pre_summary_msgs
                debug_capture(body_json, "scenario_summarization_pre", "Scenario Summarization (Pre)",
                    detail="Summarized" if did_change else "No summarization triggered",
                    setting="scenario_summary_enabled", setting_value=True,
                    metadata={"changed": did_change})

        body_json = await _apply_lorebook_injection(body_json, user_id, tags)
        if debug_on:
            debug_capture(body_json, "lorebook_injection", "Lorebook Injection",
                detail="Lorebook entries processed",
                metadata={"tags": [t.get("raw", "") for t in (tags or [])]})

        if user_id and endpoint_id:
            from app.services.skills import inject_skills, load_skills_for_endpoint
            async with async_session() as skills_db:
                skill_contents, sample_contents = await load_skills_for_endpoint(
                    endpoint_id, user_id, skills_db
                )
            if skill_contents:
                body_json["messages"] = inject_skills(body_json["messages"], skill_contents)
            if sample_contents:
                body_json["_gitv_sample_contents"] = sample_contents
            if debug_on:
                debug_capture(body_json, "skills_injection", "Skills Injection",
                    detail=f"{len(skill_contents)} skill(s), {len(sample_contents)} sample(s) loaded",
                    setting="endpoint_skills", setting_value=len(skill_contents) > 0,
                    metadata={"skill_count": len(skill_contents), "sample_count": len(sample_contents)})
        elif debug_on:
            debug_capture(body_json, "skills_injection", "Skills Injection",
                detail="No endpoint configured", setting="endpoint_skills", setting_value=False)

        from app.services.budget import prepare_budget
        await prepare_budget(body_json, user_id, tags)
        if debug_on:
            debug_capture(body_json, "budget_preparation", "Budget Preparation",
                detail="Context budget allocated",
                metadata={"budget": body_json.get("_gitv_budget", {})})

        from app.services.map_pipeline import resolve_map, run_map_pipeline
        cmd_overrides_map = body_json.get("_gitv_command_overrides", {})
        map_active = None
        if cmd_overrides_map.get("map") is not False:
            map_active = await resolve_map(user_id, tags)

        if map_active:
            import httpx as _httpx
            map_timeout = _httpx.Timeout(settings.request_timeout, connect=10.0)
            logger.info("Map '%s' active — routing to map pipeline", map_active.name)
            body_json["_gitv_chat_id"] = internal_chat_id
            body_json = await process_cantrips(body_json, user_id, request_headers, tags, internal_chat_id=internal_chat_id)
            response_data = await run_map_pipeline(body_json, user_id, request_headers, map_active, map_timeout)

            if body_json.get("_gitv_chat_id"):
                response_data = await _extract_response_memories(response_data, user_id, body_json)

            if body_json.get("_gitv_debug") and user_id:
                await _save_debug_exchange(response_data, body_json, user_id)

            if stream:
                ux_settings = await _load_ux_settings(user_id)
                return _convert_to_sse(
                    JSONResponse(status_code=200, content=response_data),
                    model or body_json.get("model", ""),
                    gitv_status=ux_settings.get("gitv_status", False),
                    simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
                )

            return JSONResponse(status_code=200, content=response_data)

        body_json = await process_cantrips(body_json, user_id, request_headers, tags, internal_chat_id=internal_chat_id)

        body_json["_gitv_chat_id"] = internal_chat_id
        body_json["_gitv_messages_for_hash"] = body_json.get("messages", [])

        from app.services.summarization import maybe_summarize
        if body_json.get("_gitv_command_overrides", {}).get("summary") is not False:
            pre_summ_msgs = json.dumps(body_json.get("messages", []), default=str)
            body_json = await maybe_summarize(body_json, user_id, internal_chat_id, tags)
            if debug_on:
                summ_changed = json.dumps(body_json.get("messages", []), default=str) != pre_summ_msgs
                debug_capture(body_json, "conversation_summarization", "Conversation Summarization",
                    detail="Summarized" if summ_changed else "Not triggered",
                    setting="summarization_enabled", setting_value=True,
                    metadata={"changed": summ_changed})

        if user_id and body_json.get("_gitv_command_overrides", {}).get("summary") is not False:
            from app.services.scenario_summarizer import maybe_summarize_scenario
            pre_post_scn = json.dumps(body_json.get("messages", []), default=str)
            body_json = await maybe_summarize_scenario(body_json, user_id, "post")
            if debug_on:
                post_scn_changed = json.dumps(body_json.get("messages", []), default=str) != pre_post_scn
                debug_capture(body_json, "scenario_summarization_post", "Scenario Summarization (Post)",
                    detail="Summarized" if post_scn_changed else "No summarization triggered",
                    setting="scenario_summary_enabled", setting_value=True,
                    metadata={"changed": post_scn_changed})

        sample_contents = body_json.pop("_gitv_sample_contents", None)
        if sample_contents:
            from app.services.skills import inject_samples
            body_json["messages"] = inject_samples(body_json["messages"], sample_contents)
            if debug_on:
                debug_capture(body_json, "writing_samples", "Writing Samples Injection",
                    detail=f"{len(sample_contents)} sample(s) injected before last message",
                    setting="endpoint_samples", setting_value=True,
                    metadata={"sample_count": len(sample_contents)})
        elif debug_on:
            debug_capture(body_json, "writing_samples", "Writing Samples Injection",
                detail="No writing samples configured", setting="endpoint_samples", setting_value=False)

        from app.services.driver_callable import (
            build_tool_notification,
            load_driver_callable_config,
        )
        async with async_session() as db:
            dc_config = await load_driver_callable_config(db, user_id, tags)

        cmd_overrides_dc = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides_dc.get("driver_callable") is False:
            dc_config.enabled = False
        elif cmd_overrides_dc.get("driver_callable") is True and dc_config.tools:
            dc_config.enabled = True

        if dc_config.enabled:
            from app.services.admin import get_caps
            caps = await get_caps()
            dc_config.turns = min(dc_config.turns, caps["max_driver_callable_turns"])
            notification = build_tool_notification(dc_config.tools, dc_config.turns)
            if notification:
                body_json["_gitv_driver_callable"] = True
                body_json["_gitv_driver_callable_turns"] = dc_config.turns
                body_json["_gitv_driver_callable_tools"] = [
                    {"id": t.id, "name": t.name.lower()} for t in dc_config.tools
                ]
                body_json["messages"] = inject_tool_notification_safe(
                    body_json.get("messages", []), notification
                )
                if debug_on:
                    debug_capture(body_json, "driver_callable", "Driver-Callable Tools",
                        detail=f"{len(dc_config.tools)} tool(s), {dc_config.turns} turn(s)",
                        setting="driver_callable_enabled", setting_value=True,
                        metadata={"tools": [t.name for t in dc_config.tools], "turns": dc_config.turns})
        elif debug_on:
            debug_capture(body_json, "driver_callable", "Driver-Callable Tools",
                detail="Disabled" if not dc_config.tools else "No tools configured",
                setting="driver_callable_enabled", setting_value=False)

        from app.services.bypass import apply_bypass_encode
        from app.services.prefill import normalize_prefill

        async with async_session() as db:
            from app.models.user_settings import UserSettings
            us_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            us = us_result.scalar_one_or_none()
            prefill_on = us.prefill_enabled if us else False

        bypass_method = endpoint_bypass

        if prefill_on:
            body_json = normalize_prefill(body_json, base_url, enabled=True)
            if debug_on:
                debug_capture(body_json, "prefill", "Prefill Normalization",
                    detail="Prefill applied",
                    setting="prefill_enabled", setting_value=True)
        elif debug_on:
            debug_capture(body_json, "prefill", "Prefill Normalization",
                detail="Disabled", setting="prefill_enabled", setting_value=False)

        if bypass_method != "none":
            body_json = apply_bypass_encode(body_json, bypass_method)
            body_json["_gitv_bypass_method"] = bypass_method
            if debug_on:
                debug_capture(body_json, "bypass_encoding", "Bypass Encoding",
                    detail=f"Method: {bypass_method}",
                    setting="bypass_method", setting_value=bypass_method)
        elif debug_on:
            debug_capture(body_json, "bypass_encoding", "Bypass Encoding",
                detail="No bypass", setting="bypass_method", setting_value="none")

        if debug_on:
            debug_capture(body_json, "final_messages", "Final Messages (Pre-Forward)",
                detail="Messages ready to send to upstream LLM")

        body = json.dumps({k: v for k, v in body_json.items() if not k.startswith("_gitv")}).encode()

    timeout = httpx.Timeout(settings.request_timeout, connect=10.0)

    if stream and user_id:
        from app.services.verification import is_verification_enabled
        try:
            verification_on = await is_verification_enabled(user_id)
        except Exception:
            verification_on = False

        cmd_overrides = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides.get("verify") is False:
            verification_on = False
        elif cmd_overrides.get("verify") is True:
            verification_on = True

        dc_active = body_json.get("_gitv_driver_callable", False)

        if verification_on or dc_active:
            ux_settings = await _load_ux_settings(user_id)
            body_json_verified = dict(body_json)
            body_json_verified["stream"] = False
            body_verified = json.dumps(
                {k: v for k, v in body_json_verified.items() if not k.startswith("_gitv")}
            ).encode()
            if dc_active:
                logger.info("Driver-callable active: converting streaming to non-streaming for tool loop")
            elif verification_on:
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
        return await _forward_streaming(
            request.method, upstream_url, headers, body, timeout, user_id, body_json
        )

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
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout,
    user_id: str | None = None, body_json: dict[str, Any] | None = None,
) -> JSONResponse:
    response_data, status_code = await _do_forward(
        method, url, headers, body, timeout,
        provider=body_json.get("_gitv_provider", "") if body_json else "",
        base_url=body_json.get("_gitv_base_url", "") if body_json else "",
        api_key=body_json.get("_gitv_api_key", "") if body_json else "",
    )

    if status_code == 200 and user_id and body_json and body_json.get("_gitv_chat_id"):
        if body_json.get("_gitv_command_overrides", {}).get("memory") is not False:
            response_data = await _extract_response_memories(response_data, user_id, body_json)

    return JSONResponse(status_code=status_code, content=response_data)


async def _extract_response_memories(
    response_data: dict[str, Any], user_id: str, body_json: dict[str, Any]
) -> dict[str, Any]:
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.conversation import record_post_hash, save_memories_for_chat
    from app.services.memory import parse_memstore_tags

    cleaned, memories = parse_memstore_tags(content)

    chat_id = body_json.get("_gitv_chat_id", "")
    messages_for_hash = body_json.get("_gitv_messages_for_hash", body_json.get("messages", []))

    if cleaned != content:
        response_data["choices"][0]["message"]["content"] = cleaned

    if chat_id and memories:
        await save_memories_for_chat(chat_id, user_id, memories)
        logger.info("Memory extraction: %d keys saved for chat %s", len(memories), chat_id[:12])

    if chat_id and body_json.get("_gitv_debug") is not None:
        from app.services.debug import debug_capture_response
        debug_capture_response(body_json, "memory_extraction", "Memory Extraction",
            content_before=content[:500],
            content_after=cleaned[:500],
            detail=f"Extracted {len(memories)} memories" if memories else "No memories found",
            metadata={"memory_keys": list(memories.keys()) if memories else []})

    if chat_id:
        await record_post_hash(messages_for_hash, cleaned, chat_id, user_id)

    return response_data


def inject_tool_notification_safe(
    messages: list[dict[str, Any]], notification: str
) -> list[dict[str, Any]]:
    from app.services.driver_callable import inject_tool_notification
    return inject_tool_notification(messages, notification)


async def _run_driver_callable_loop(
    body_json: dict[str, Any],
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any] | None:
    """Run the Driver-Callable tool loop.

    Returns the final response_data dict if the loop ran, or None if
    driver-callable is not active for this request (caller should
    forward normally).
    """
    if not body_json.get("_gitv_driver_callable"):
        return None

    from app.services.driver_callable import (
        build_tool_notification,
        execute_tool_call,
        format_tool_result,
        parse_call_tags,
        strip_call_tags,
    )

    turns_remaining = body_json.get("_gitv_driver_callable_turns", 0)

    from app.services.cantrip import _load_active_cantrips

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id, "pre_driver")
        tool_cantrips = {c.name.lower(): c for c in all_cantrips if c.run_driver_callable}

    loop_messages = [msg.copy() for msg in body_json.get("messages", [])]
    conversation_id = body_json.get("_gitv_chat_id", "")

    loop_body = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    loop_body["stream"] = False
    loop_body["messages"] = loop_messages

    rounds = 0
    max_rounds = max(turns_remaining, 1)

    while rounds < max_rounds:
        body_bytes = json.dumps(loop_body).encode()
        response_data, status_code = await _do_forward(method, url, headers, body_bytes, timeout,
            provider=body_json.get("_gitv_provider", ""),
            base_url=body_json.get("_gitv_base_url", ""),
            api_key=body_json.get("_gitv_api_key", ""),
        )

        if status_code != 200:
            logger.warning("Driver-callable loop: forward returned %d", status_code)
            return response_data

        choices = response_data.get("choices", [])
        if not choices:
            return response_data

        content = choices[0].get("message", {}).get("content", "")
        calls = parse_call_tags(content)

        if not calls or turns_remaining <= 0:
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned
            return response_data

        turns_remaining -= 1
        rounds += 1

        assistant_content = strip_call_tags(content).strip()
        loop_messages.append({"role": "assistant", "content": assistant_content})

        for call in calls:
            tool = tool_cantrips.get(call.name)
            if tool is None:
                result_text = f"Error: tool '{call.name}' not found"
            else:
                result_text = await execute_tool_call(
                    tool, call, loop_messages, request_headers, user_id, conversation_id
                )

            tool_msg = format_tool_result(call.name, result_text)
            loop_messages.append({"role": "user", "content": tool_msg})
            logger.info(
                "Driver-callable: executed '%s' (turn %d, %d remaining)",
                call.name, rounds, turns_remaining,
            )

        if turns_remaining > 0:
            notification = build_tool_notification(
                [tc for tc in tool_cantrips.values()], turns_remaining
            )
            loop_messages = inject_tool_notification_safe(loop_messages, notification)

        loop_body["messages"] = loop_messages

    body_bytes = json.dumps(loop_body).encode()
    response_data, status_code = await _do_forward(method, url, headers, body_bytes, timeout,
        provider=body_json.get("_gitv_provider", ""),
        base_url=body_json.get("_gitv_base_url", ""),
        api_key=body_json.get("_gitv_api_key", ""),
    )

    if status_code == 200:
        choices = response_data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned

    return response_data


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
    if body_json.get("_gitv_driver_callable") and user_id:
        try:
            loop_result = await _run_driver_callable_loop(
                body_json, method, url, headers, timeout, user_id, request_headers,
            )
            if loop_result is not None:
                body_json["_gitv_driver_callable_completed"] = True
                response_data = loop_result
                status_code = 200

                if status_code == 200 and user_id:
                    response_data = await _apply_post_driver_processing(
                        response_data, body_json, user_id, request_headers
                    )
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

                    try:
                        response_data = await _apply_post_navigator_processing(
                            response_data, body_json, user_id, request_headers
                        )
                    except Exception:
                        logger.exception("Post-navigator processing failed")

                if status_code == 200 and body_json.get("_gitv_chat_id"):
                    response_data = await _extract_response_memories(response_data, user_id, body_json)

                if isinstance(response_data, dict):
                    response_data.pop("_gitv_forbidden_summary", None)

                if status_code == 200 and body_json.get("_gitv_bypass_method"):
                    from app.services.bypass import apply_bypass_decode
                    response_data = apply_bypass_decode(response_data, body_json["_gitv_bypass_method"])

                if body_json.get("_gitv_debug") and user_id:
                    await _save_debug_exchange(response_data, body_json, user_id, locals().get("vresult"))

                return JSONResponse(status_code=status_code, content=response_data)
        except Exception:
            logger.exception("Driver-callable loop failed, falling through to normal forward")

    response_data, status_code = await _do_forward(method, url, headers, body, timeout,
        provider=body_json.get("_gitv_provider", ""),
        base_url=body_json.get("_gitv_base_url", ""),
        api_key=body_json.get("_gitv_api_key", ""),
    )

    if status_code == 200 and user_id:
        response_data = await _apply_post_driver_processing(
            response_data, body_json, user_id, request_headers
        )

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

        try:
            response_data = await _apply_post_navigator_processing(
                response_data, body_json, user_id, request_headers
            )
        except Exception:
            logger.exception("Post-navigator processing failed")

    if status_code == 200 and user_id and body_json.get("_gitv_chat_id"):
        if body_json.get("_gitv_command_overrides", {}).get("memory") is not False:
            response_data = await _extract_response_memories(response_data, user_id, body_json)

    if isinstance(response_data, dict):
        response_data.pop("_gitv_forbidden_summary", None)

    if status_code == 200 and body_json.get("_gitv_bypass_method"):
        from app.services.bypass import apply_bypass_decode
        response_data = apply_bypass_decode(response_data, body_json["_gitv_bypass_method"])

    if body_json.get("_gitv_debug") and user_id:
        await _save_debug_exchange(response_data, body_json, user_id, locals().get("vresult"))

    return JSONResponse(status_code=status_code, content=response_data)


async def _save_debug_exchange(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    vresult: Any = None,
) -> None:
    """Capture pipeline data and response for debug mode."""
    from app.services.debug import capture_exchange, debug_capture_response

    response_content = ""
    choices = response_data.get("choices", []) if isinstance(response_data, dict) else []
    if choices:
        response_content = choices[0].get("message", {}).get("content", "")

    verification_data: dict[str, Any] = {}
    if vresult:
        verification_data = {
            "approved": vresult.approved,
            "retries_used": vresult.retries_used,
            "check_history": [
                {
                    "approved": c.approved,
                    "violations": c.violations,
                }
                for c in vresult.check_history
            ],
        }
        debug_capture_response(body_json, "verification", "Verification",
            content_after=response_content[:500],
            detail=f"{'Approved' if vresult.approved else 'Rejected'}, {vresult.retries_used} retry/retries",
            metadata=verification_data)
    elif body_json.get("_gitv_debug"):
        debug_capture_response(body_json, "verification", "Verification",
            detail="Not enabled")

    if body_json.get("_gitv_bypass_method"):
        debug_capture_response(body_json, "bypass_decoding", "Bypass Decoding",
            content_after=response_content[:500],
            detail=f"Method: {body_json['_gitv_bypass_method']}")

    debug_capture_response(body_json, "llm_response", "LLM Response",
        content_after=response_content[:500],
        detail="Final response from upstream LLM")

    pipeline_data = body_json.get("_gitv_debug", {})

    await capture_exchange(
        user_id=user_id,
        chat_id=body_json.get("_gitv_chat_id", ""),
        model=body_json.get("model", ""),
        pipeline_data=pipeline_data,
        response_content=response_content,
        verification_data=verification_data,
    )


async def _apply_post_driver_processing(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any]:
    """Run pre-Navigator cantrips and forbidden word scan on Driver response."""
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    body_json_for_cantrip = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        new_content = await process_cantrips_post_driver(
            content, body_json_for_cantrip, user_id, request_headers,
            tags=tags, position="pre_navigator",
        )
        if new_content != content:
            response_data["choices"][0]["message"]["content"] = new_content
            content = new_content
            logger.info("Pre-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "pre_navigator_cantrips", "Pre-Navigator Cantrips",
                    content_before=response_data["choices"][0]["message"]["content"][:500],
                    content_after=content[:500],
                    detail="Response modified by pre-navigator cantrips")
        elif debug_on:
            from app.services.debug import debug_capture_response
            debug_capture_response(body_json, "pre_navigator_cantrips", "Pre-Navigator Cantrips",
                detail="No changes")
    except Exception:
        logger.exception("Pre-navigator cantrip processing failed")

    try:
        from app.services.forbidden_words import scan_response
        cmd_overrides = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides.get("forbidden") is not False:
            scan_result = await scan_response(content, user_id)
            if scan_result.has_matches:
                response_data["_gitv_forbidden_summary"] = scan_result.summary
                logger.info(
                    "Forbidden words: %d phrase(s) matched",
                    len(scan_result.matches),
                )
                if debug_on:
                    from app.services.debug import debug_capture_response
                    debug_capture_response(body_json, "forbidden_words", "Forbidden Words Scan",
                        detail=f"{len(scan_result.matches)} match(es) found",
                        metadata={"matches": [m.word if hasattr(m, 'word') else str(m) for m in scan_result.matches]})
            elif debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "forbidden_words", "Forbidden Words Scan",
                    detail="No matches")
    except Exception:
        logger.exception("Forbidden word scan failed")

    return response_data


async def _apply_post_navigator_processing(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any]:
    """Run post-Navigator cantrips (final cleanup) on the approved response."""
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    body_json_for_cantrip = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        new_content = await process_cantrips_post_driver(
            content, body_json_for_cantrip, user_id, request_headers,
            tags=tags, position="post_navigator",
        )
        if new_content != content:
            response_data["choices"][0]["message"]["content"] = new_content
            logger.info("Post-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                    content_before=content[:500], content_after=new_content[:500],
                    detail="Response modified by post-navigator cantrips")
        elif debug_on:
            from app.services.debug import debug_capture_response
            debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                detail="No changes")
    except Exception:
        logger.exception("Post-navigator cantrip processing failed")

    return response_data


async def _do_forward(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout,
    provider: str = "", base_url: str = "", api_key: str = "",
) -> tuple[dict[str, Any], int]:
    if provider:
        return await _do_forward_litellm(body, provider, base_url, api_key, timeout)

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

    _log_response(response.status_code, response.elapsed.total_seconds(),
                  response.text if response.status_code >= 400 else "")

    try:
        return response.json(), response.status_code
    except Exception:
        return (
            {"error": {"message": response.text, "type": "upstream_error"}},
            response.status_code,
        )


async def _do_forward_litellm(
    body: bytes, provider: str, base_url: str, api_key: str, timeout: httpx.Timeout
) -> tuple[dict[str, Any], int]:
    """Forward request via LiteLLM for provider-specific compatibility."""
    try:
        import litellm
    except ImportError:
        return (
            {"error": {"message": "LiteLLM is not installed. Provider-based endpoints require it. Run: pip install litellm==1.89.4", "type": "proxy_error"}},
            500,
        )

    try:
        body_json = json.loads(body)
    except Exception:
        return {"error": {"message": "Invalid JSON body", "type": "proxy_error"}}, 400

    model = body_json.get("model", "")
    messages = body_json.get("messages", [])
    stream = body_json.get("stream", False)

    if not model:
        return {"error": {"message": "Model not specified", "type": "proxy_error"}}, 400

    litellm_model = f"{provider}/{model}" if not model.startswith(f"{provider}/") else model

    kwargs: dict[str, Any] = {"api_key": api_key}

    if provider in ("openai_compatible", "custom_openai"):
        litellm_model = f"openai/{model}"
        kwargs["api_base"] = base_url

    litellm_params = (
        "temperature", "max_tokens", "top_p", "top_k", "stream",
        "stop", "frequency_penalty", "presence_penalty", "seed",
        "n", "logprobs", "user",
    )
    for param in litellm_params:
        if param in body_json:
            kwargs[param] = body_json[param]

    if stream:
        kwargs["stream"] = True

    timeout_seconds = timeout.read if hasattr(timeout, "read") and timeout.read else settings.request_timeout

    logger.info("litellm forward: provider=%s model=%s stream=%s", provider, model, stream)

    try:
        response = await litellm.acompletion(
            model=litellm_model,
            messages=messages,
            timeout=timeout_seconds,
            **kwargs,
        )

        if stream:
            chunks: list[dict[str, Any]] = []
            content_parts: list[str] = []
            final_metadata: dict[str, Any] = {}
            async for chunk in response:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                chunks.append(chunk_dict)
                if chunk_dict.get("choices"):
                    delta = chunk_dict["choices"][0].get("delta", {})
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                if chunk_dict.get("usage"):
                    final_metadata["usage"] = chunk_dict["usage"]
                if chunk_dict.get("model"):
                    final_metadata["model"] = chunk_dict["model"]

            combined = {
                "id": chunks[0].get("id", "chatcmpl-litellm") if chunks else "chatcmpl-litellm",
                "object": "chat.completion",
                "model": final_metadata.get("model", model),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(content_parts)},
                    "finish_reason": chunks[-1]["choices"][0].get("finish_reason", "stop") if chunks and chunks[-1].get("choices") else "stop",
                }],
                **final_metadata,
            }
            _log_response(200, 0.0)
            return combined, 200

        result = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        _log_response(200, 0.0)
        return result, 200

    except Exception as exc:
        import litellm as _litellm

        status_code = 500
        error_msg = str(exc)
        error_type = type(exc).__name__

        for exc_class, code, msg in [
            ("AuthenticationError", 401, "Authentication failed"),
            ("RateLimitError", 429, "Rate limited"),
            ("BadRequestError", 400, None),
            ("NotFoundError", 404, None),
            ("Timeout", 504, "Request timed out"),
            ("APIConnectionError", 502, "Failed to connect to provider"),
            ("InternalServerError", 500, None),
        ]:
            exc_cls = getattr(_litellm.exceptions, exc_class, None)
            if exc_cls and isinstance(exc, exc_cls):
                status_code = code
                error_msg = msg or str(exc)
                break

        if status_code < 500:
            logger.warning("litellm forward error (%d): %s", status_code, error_msg[:200])
        else:
            logger.exception("litellm forward error: %s", exc)

        _log_response(status_code, 0.0, error_msg)
        return {"error": {"message": error_msg, "type": error_type}}, status_code


async def _forward_streaming(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: httpx.Timeout,
    user_id: str | None = None,
    body_json: dict[str, Any] | None = None,
) -> StreamingResponse:
    has_memory_processing = (
        user_id is not None
        and body_json is not None
        and body_json.get("_gitv_chat_id")
    )

    if not has_memory_processing:
        return await _forward_streaming_raw(method, url, headers, body, timeout)

    return await _forward_streaming_with_memory(
        method, url, headers, body, timeout, user_id, body_json
    )


async def _forward_streaming_raw(
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


async def _forward_streaming_with_memory(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: httpx.Timeout,
    user_id: str,
    body_json: dict[str, Any],
) -> StreamingResponse:
    """Buffer a streaming response, extract and strip <memstore> tags,
    record the post-hash, then re-emit as SSE. Memory tags span content
    chunks so they can't be reliably extracted from individual chunks."""
    response_data, status_code = await _do_forward(method, url, headers, body, timeout,
        provider=body_json.get("_gitv_provider", "") if body_json else "",
        base_url=body_json.get("_gitv_base_url", "") if body_json else "",
        api_key=body_json.get("_gitv_api_key", "") if body_json else "",
    )

    if status_code == 200:
        response_data = await _extract_response_memories(response_data, user_id, body_json)
        model = body_json.get("model", "")
        return _convert_to_sse(
            JSONResponse(status_code=200, content=response_data),
            model,
        )

    return JSONResponse(status_code=status_code, content=response_data)
