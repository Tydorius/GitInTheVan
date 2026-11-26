from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.services.llm_params import RESERVED_NAMES, ParamDef, apply_to_body, resolve
from app.services.routing import FailoverEndpoint

logger = logging.getLogger(__name__)

# Parameters LiteLLM accepts as named kwargs. Anything outside this set that the
# user has deliberately configured is passed through `extra_body` instead;
# anything outside it that only the *client* sent is still dropped, because the
# raw body is not forwarded on this path and guessing which unknown client keys
# a provider will tolerate is how the Gemini `top_k` failures started.
#
# `reasoning_effort` and `max_completion_tokens` were absent until Phase 23,
# which is why a client that sent either lost it on every provider endpoint.
# `tools`, `tool_choice` and `parallel_tool_calls` were absent until Phase 26a,
# the same class of bug: a client-sent tool definition was silently dropped on
# every provider endpoint, so native function calling never reached the model.
LITELLM_NATIVE_PARAMS: frozenset[str] = frozenset({
    "temperature", "max_tokens", "top_p", "top_k", "stream",
    "stop", "frequency_penalty", "presence_penalty", "seed",
    "n", "logprobs", "user",
    "reasoning_effort", "max_completion_tokens", "response_format", "thinking",
    "tools", "tool_choice", "parallel_tool_calls",
})


def extract_response_text(response_data: Any) -> str:
    """Safely extract assistant message text from an OpenAI-style response dict.

    Returns ``""`` for ``None``, non-dict responses, missing or empty
    ``choices``, or non-dict ``message`` entries without raising ``IndexError``.
    """
    if not isinstance(response_data, dict):
        return ""
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if content is None:
        return ""
    return content if isinstance(content, str) else str(content)


_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def _supports_anthropic_cache_control(provider: str, model: str, base_url: str) -> bool:
    """Return True if the target endpoint supports Anthropic-style cache_control blocks."""
    prov = (provider or "").strip().lower()
    if prov in ("anthropic", "openrouter"):
        return True
    url = (base_url or "").strip().lower()
    if "anthropic" in url or "openrouter.ai" in url:
        return True
    mdl = (model or "").strip().lower()
    return "claude" in mdl


def _has_cache_control(msg: dict[str, Any]) -> bool:
    if "cache_control" in msg:
        return True
    content = msg.get("content")
    if isinstance(content, list):
        return any(isinstance(block, dict) and "cache_control" in block for block in content)
    return False


def _stamp_message_cache_control(msg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a shallow-copied message with an ephemeral cache_control breakpoint."""
    if _has_cache_control(msg):
        return msg, False

    content = msg.get("content")
    if isinstance(content, str):
        if not content:
            return msg, False
        new_msg = {
            **msg,
            "content": [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
                }
            ],
        }
        return new_msg, True

    if isinstance(content, list):
        last_text_idx = None
        for idx in range(len(content) - 1, -1, -1):
            block = content[idx]
            if isinstance(block, dict) and block.get("type") == "text":
                last_text_idx = idx
                break
        if last_text_idx is None:
            return msg, False
        new_blocks = list(content)
        new_blocks[last_text_idx] = {
            **content[last_text_idx],
            "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
        }
        return {**msg, "content": new_blocks}, True

    return msg, False


def apply_auto_cache_control(
    messages: list[dict[str, Any]],
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Stamp up to 2 Anthropic/OpenRouter ephemeral cache_control breakpoints.

    Breakpoints are placed on:
    1. The last ``role == "system"`` message.
    2. The second-to-last non-system message (when at least 2 non-system messages exist).

    Never mutates caller message dicts or content lists. Returns
    ``(new_messages, breakpoints_added)``.
    """
    if not messages or not _supports_anthropic_cache_control(provider, model, base_url):
        return messages, 0

    new_messages = list(messages)
    breakpoints_added = 0

    last_system_idx: int | None = None
    non_system_indices: list[int] = []
    for idx, msg in enumerate(new_messages):
        if msg.get("role") == "system":
            last_system_idx = idx
        else:
            non_system_indices.append(idx)

    if last_system_idx is not None:
        stamped_msg, stamped = _stamp_message_cache_control(new_messages[last_system_idx])
        if stamped:
            new_messages[last_system_idx] = stamped_msg
            breakpoints_added += 1

    if len(non_system_indices) >= 2:
        target_idx = non_system_indices[-2]
        stamped_msg, stamped = _stamp_message_cache_control(new_messages[target_idx])
        if stamped:
            new_messages[target_idx] = stamped_msg
            breakpoints_added += 1

    if breakpoints_added == 0:
        return messages, 0
    return new_messages, breakpoints_added


def stamp_body_cache_control(
    body_bytes: bytes,
    body_json: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
) -> bytes:
    """Apply automatic cache_control breakpoints to a serialized wire body if enabled.

    Never mutates ``body_json["messages"]`` or ``_gitv_messages_for_hash``. Emits a
    ``cache_control`` debug capture stage when breakpoints are added.
    """
    if not body_json.get("_gitv_auto_cache_control"):
        return body_bytes
    try:
        payload = json.loads(body_bytes)
    except Exception:
        return body_bytes
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return body_bytes
    resolved_model = model or payload.get("model", "")
    stamped, count = apply_auto_cache_control(
        messages,
        provider=provider,
        model=resolved_model,
        base_url=base_url,
    )
    if count <= 0:
        return body_bytes
    payload["messages"] = stamped
    from app.services.debug import debug_capture

    target_label = provider or base_url or resolved_model
    debug_capture(
        body_json,
        "cache_control",
        "Prompt Cache Breakpoints",
        detail=f"Stamped {count} ephemeral cache_control breakpoint(s) for {target_label}",
        metadata={
            "breakpoints": count,
            "provider": provider,
            "model": resolved_model,
        },
    )
    return json.dumps(payload).encode()


def _resolve_proxy_override(name: str, local_fn: Any) -> Any:
    """Resolve a transport helper through ``app.services.proxy`` when patched.

    Existing tests monkeypatch ``app.services.proxy._do_forward`` or
    ``_do_forward_litellm``; checking ``sys.modules`` preserves that seam
    without introducing a circular import.
    """
    proxy_mod = sys.modules.get("app.services.proxy")
    if proxy_mod is not None:
        candidate = getattr(proxy_mod, name, None)
        if candidate is not None and candidate is not local_fn:
            return candidate
    return local_fn


def auth_headers(api_key: str) -> dict[str, str]:
    """Headers for internal LLM calls (judges, summarizers, map stages,
    lexicon compiles). A keyless endpoint (local LLMs, LAN gateways) must
    not get an Authorization header at all: httpcore rejects `Bearer ` with
    an empty credential as an illegal header value, so the unconditional
    form failed every internal call on keyless endpoints."""
    headers = {"Content-Type": "application/json"}
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


def _strip_think_tags(content: str) -> str:
    """Remove <think>...</think> blocks from content."""
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)


def _convert_to_sse(
    response: JSONResponse,
    model: str,
    gitv_status: bool = False,
    simulated_speed: int = 0,
    preserve_thinking: bool = True,
) -> StreamingResponse:
    data = json.loads(response.body)
    choices = data.get("choices") or [{}]
    message = (choices[0] if isinstance(choices[0], dict) else {}).get("message", {}) or {}
    content = message.get("content", "")
    reasoning = (
        message.get("reasoning_content")
        or message.get("thinking")
        or message.get("reasoning")
        or ""
    )
    chunk_id = data.get("id", "chatcmpl-converted")
    created = data.get("created", 0)

    if not preserve_thinking and content:
        content = _strip_think_tags(content)

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

        if reasoning and preserve_thinking:
            yield make_chunk(reasoning)

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


def _record_call(
    body_json: dict[str, Any],
    candidate: FailoverEndpoint,
    attempt: int,
    started: float,
    *,
    status_code: int,
    response_data: dict[str, Any] | None,
    error: str = "",
    purpose: str | None = None,
    messages: Any = None,
    stage_index: int | None = None,
    rule_id: str | None = None,
) -> None:
    """Record one upstream attempt on the debug trace.

    Called for failures as well as successes: a run whose first endpoint 502'd
    and whose second answered took the sum of both latencies, and a comparison
    that only showed the winner would mis-attribute the wall-clock time.
    """
    from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call

    record_llm_call(
        body_json,
        purpose=purpose or PURPOSE_MAIN,
        latency_ms=(time.monotonic() - started) * 1000.0,
        status_code=status_code,
        response_data=response_data,
        messages=messages if messages is not None else body_json.get("messages"),
        stage_index=stage_index,
        endpoint_id=candidate.endpoint_id,
        endpoint_name=candidate.endpoint_name,
        provider=candidate.provider,
        model_requested=candidate.model or body_json.get("model", ""),
        failover_attempt=attempt,
        error=error,
        rule_id=rule_id,
    )


async def forward_llm_chain(
    body_json: dict[str, Any],
    chain: list[FailoverEndpoint],
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    body_bytes: bytes | None = None,
    *,
    purpose: str = "main",
    stage_index: int | None = None,
    rule_id: str | None = None,
    update_body_meta: bool = True,
) -> tuple[dict[str, Any], int]:
    """Forward a request through a failover chain of ``FailoverEndpoint`` candidates.

    Handles per-candidate URL building, per-candidate ``Authorization``
    headers, fresh per-candidate ``llm_params.resolve()``, ``_do_forward``
    (raw httpx vs LiteLLM), per-attempt ``started = time.monotonic()``, and
    ``record_llm_call()`` on both success and exception.
    """
    if not chain:
        return (
            {"error": {"message": "No endpoint configured for this request", "type": "proxy_error"}},
            503,
        )

    effective_timeout = timeout or httpx.Timeout(settings.request_timeout, connect=10.0)
    forward_fn = _resolve_proxy_override("_do_forward", _do_forward)
    failover_notes: list[str] = []

    for idx, candidate in enumerate(chain):
        upstream_url = _build_upstream_url(candidate.base_url, path, candidate.api_base_path)

        if update_body_meta:
            # Re-stash per-candidate fields so downstream debug/metrics pick them up.
            body_json["_gitv_provider"] = candidate.provider
            body_json["_gitv_base_url"] = candidate.base_url
            body_json["_gitv_api_key"] = candidate.api_key
            body_json["_gitv_endpoint_id"] = candidate.endpoint_id

        if headers is not None:
            trial_headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
            if candidate.api_key:
                trial_headers["Authorization"] = f"Bearer {candidate.api_key}"
        else:
            trial_headers = auth_headers(candidate.api_key)

        effective_model = candidate.model or body_json.get("model", "")

        # Candidates may carry different endpoint- and model-level parameters, so
        # the first candidate's resolved set is not reusable. Re-resolve against
        # this one. The pure user-settings layer travels on body_json from the
        # driver path and does not include Candidate 0's endpoint/model params.
        if idx == 0 and body_bytes is not None and "_gitv_llm_params" in body_json:
            candidate_params = body_json.get("_gitv_llm_params", {})
        else:
            layers: list[tuple[str, list[ParamDef]]] = [
                (
                    f"endpoint '{candidate.endpoint_name}'"
                    if candidate.endpoint_name
                    else "endpoint",
                    candidate.parameters,
                ),
                (f"model '{effective_model}'", candidate.params_for_model(effective_model)),
            ]
            resolved = resolve(layers)
            user_params = body_json.get("_gitv_user_llm_params", {})
            candidate_params = {**user_params, **resolved.values}

        if body_bytes is not None:
            forward_body = body_bytes
            needs_rebuild = (candidate.model and body_json.get("model") != candidate.model) or (
                idx > 0 and candidate_params != body_json.get("_gitv_llm_params", {})
            )
            if needs_rebuild:
                trial_body = dict(body_json)
                if idx > 0:
                    for prev_key in body_json.get("_gitv_llm_params", {}):
                        if prev_key not in candidate_params:
                            trial_body.pop(prev_key, None)
                if candidate.model:
                    trial_body["model"] = candidate.model
                trial_body = apply_to_body(trial_body, candidate_params)
                forward_body = json.dumps(
                    {k: v for k, v in trial_body.items() if not k.startswith("_gitv")}
                ).encode()
        else:
            trial_body = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
            if effective_model:
                trial_body["model"] = effective_model
            trial_body["stream"] = False
            trial_body = apply_to_body(trial_body, candidate_params)
            forward_body = json.dumps(trial_body).encode()

        forward_body = stamp_body_cache_control(
            forward_body,
            body_json,
            provider=candidate.provider,
            model=effective_model,
            base_url=candidate.base_url,
        )

        started = time.monotonic()
        try:
            response_data, status_code = await forward_fn(
                method, upstream_url, trial_headers, forward_body, effective_timeout,
                provider=candidate.provider,
                base_url=candidate.base_url,
                api_key=candidate.api_key,
                configured=candidate_params,
            )
        except Exception as exc:
            logger.exception(
                "Failover: endpoint '%s' (%s) raised an exception, trying next",
                candidate.endpoint_name, upstream_url,
            )
            _record_call(
                body_json, candidate, idx, started,
                status_code=0, response_data=None, error=str(exc)[:500],
                purpose=purpose, stage_index=stage_index, rule_id=rule_id,
            )
            failover_notes.append(f"'{candidate.endpoint_name}' failed with an exception")
            continue

        _record_call(
            body_json, candidate, idx, started,
            status_code=status_code, response_data=response_data,
            purpose=purpose, stage_index=stage_index, rule_id=rule_id,
        )

        if status_code == 200:
            if failover_notes:
                logger.info(
                    "Failover: succeeded on candidate %d/%d '%s' after %d failure(s)",
                    idx + 1, len(chain), candidate.endpoint_name, len(failover_notes),
                )
            return response_data, 200

        logger.info(
            "Failover: endpoint '%s' (%s) returned %d, trying next",
            candidate.endpoint_name, upstream_url, status_code,
        )
        failover_notes.append(f"'{candidate.endpoint_name}' returned HTTP {status_code}")

    logger.warning("Failover: all %d endpoint(s) exhausted", len(chain))
    return (
        {
            "error": {
                "message": f"All endpoints failed: {'; '.join(failover_notes)}",
                "type": "proxy_error",
            }
        },
        503,
    )


async def _forward_with_failover(
    body_json: dict[str, Any],
    chain: list[FailoverEndpoint],
    method: str,
    path: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    body_bytes: bytes,
) -> tuple[dict[str, Any], int]:
    """Forward a request through the failover chain.

    Iterates candidates in priority order. Any failure (any HTTP status != 200
    or any exception) advances to the next candidate — there is no
    retryable/non-retryable distinction, because different endpoints may carry
    different models, API keys, or providers (e.g. a free OpenRouter key
    failing over to a paid endpoint). Only total exhaustion returns an error.

    The `path` is the request path (e.g. '/v1/chat/completions'); each
    candidate's base_url + api_base_path produce a distinct upstream URL."""
    return await forward_llm_chain(
        body_json, chain, method, path, headers, timeout, body_bytes,
    )


async def _do_forward(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout,
    provider: str = "", base_url: str = "", api_key: str = "",
    configured: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Forward one request upstream.

    `configured` is the resolved LLM parameter set. The raw httpx path does not
    need it -- the caller already wrote those keys into `body`, which is
    forwarded verbatim. LiteLLM does need it, because it builds its call from
    named kwargs rather than the body, so it has to be told which keys are
    deliberate rather than incidental.
    """
    if provider:
        litellm_fn = _resolve_proxy_override("_do_forward_litellm", _do_forward_litellm)
        return await litellm_fn(body, provider, base_url, api_key, timeout, configured)

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
    body: bytes, provider: str, base_url: str, api_key: str, timeout: httpx.Timeout,
    configured: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Forward request via LiteLLM for provider-specific compatibility.

    Unlike the raw httpx path, which forwards the body verbatim, this builds the
    call from named kwargs -- so anything not named here is silently dropped.
    That is how a client's `reasoning_effort` reached a raw endpoint intact and
    vanished the moment the endpoint had a provider set.
    """
    try:
        import litellm
    except ImportError:
        return (
            {"error": {"message": "LiteLLM is not installed. Provider-based endpoints require it. Run: pip install litellm==1.110.2", "type": "proxy_error"}},
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

    is_gemini_family = (
        provider in ("gemini", "vertex_ai", "google")
        or litellm_model.startswith(("gemini/", "vertex_ai/"))
    )
    gemini_thinking_level: Any = body_json.get("thinking_level") if is_gemini_family else None

    for param in LITELLM_NATIVE_PARAMS:
        if param in body_json:
            kwargs[param] = body_json[param]

    # Configured parameters are applied after the client's, so a value the user
    # set on an endpoint, model, rule or map stage wins. Names LiteLLM models
    # natively go as kwargs; anything else rides in extra_body, which is how
    # LiteLLM forwards provider-specific keys it has no opinion about. Without
    # this an arbitrary user-named parameter could never reach a provider.
    for name, value in (configured or {}).items():
        if name in RESERVED_NAMES or name.startswith("_gitv"):
            continue
        if is_gemini_family and name == "thinking_level":
            gemini_thinking_level = value
            continue
        if name in LITELLM_NATIVE_PARAMS:
            kwargs[name] = value
        else:
            kwargs.setdefault("extra_body", {})[name] = value

    # LiteLLM 1.110.2 natively maps `reasoning_effort` to Gemini 3/4
    # `thinkingConfig.thinkingLevel`. Translate `thinking_level` on Gemini/Vertex
    # endpoints instead of forwarding it in `extra_body`, which Google rejects.
    # An explicit `reasoning_effort` (client or configured) outranks the
    # `thinking_level` alias.
    if (
        is_gemini_family
        and gemini_thinking_level is not None
        and "reasoning_effort" not in kwargs
    ):
        level_str = str(gemini_thinking_level).strip().lower()
        if level_str:
            kwargs["reasoning_effort"] = level_str

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
            reasoning_parts: list[str] = []
            final_metadata: dict[str, Any] = {}
            async for chunk in response:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                chunks.append(chunk_dict)
                if chunk_dict.get("choices"):
                    delta = chunk_dict["choices"][0].get("delta", {})
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    reasoning_delta = (
                        delta.get("reasoning_content")
                        or delta.get("thinking")
                        or delta.get("reasoning")
                    )
                    if reasoning_delta:
                        reasoning_parts.append(reasoning_delta)
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
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts),
                        **({"reasoning_content": "".join(reasoning_parts)} if reasoning_parts else {}),
                    },
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


class _StreamAccumulator:
    """Reassembles an SSE stream into a non-streaming response shape.

    Only what the debug trace needs: content, reasoning, finish reason and the
    usage block some providers append to the final chunk. Chunk boundaries fall
    anywhere, including mid-line, so a partial tail is carried between feeds.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.usage: dict[str, Any] | None = None
        self.finish_reason = ""
        self.model = ""

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk.decode("utf-8", "replace")
        except Exception:
            return

        # Keep the last partial line; SSE events end with a newline.
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            self._absorb(event)

    def _absorb(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        if event.get("model"):
            self.model = event["model"]
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.content.append(delta["content"])
            if delta.get("reasoning_content"):
                self.reasoning.append(delta["reasoning_content"])
            elif delta.get("thinking"):
                self.reasoning.append(delta["thinking"])
            elif delta.get("reasoning"):
                self.reasoning.append(delta["reasoning"])

    def as_response(self, fallback_model: str) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content),
        }
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)

        response: dict[str, Any] = {
            "object": "chat.completion",
            "model": self.model or fallback_model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason or "stop",
            }],
        }
        if self.usage:
            response["usage"] = self.usage
        return response
