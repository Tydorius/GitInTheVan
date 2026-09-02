"""The assistant's outbound call.

The seventh hand-rolled copy of url / headers / body / `apply_to_body` /
`_do_forward` / `record_llm_call`, written generically over a
`FailoverEndpoint` so promoting the six existing copies to a shared helper
later is mechanical rather than a rewrite.

Two deliberate differences from the proxy path. There is no failover: an
assistant call that fails is shown to the user, not silently retried somewhere
else that might cost money on a different account. And `tools`, `tool_choice`
and `stream` are written *after* `apply_to_body`, so no configured parameter
layer can turn tool calling off or turn streaming on.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select

from app.database import async_session
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.services.llm_params import ParamDef, apply_to_body, parse_params, resolve
from app.services.routing import FailoverEndpoint

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 180


async def resolve_assistant_candidate(
    user_id: str,
) -> tuple[FailoverEndpoint, str, list[ParamDef]] | None:
    """The endpoint, model and user parameter layer for this user's assistant.

    Returns None when no assistant endpoint is chosen, when the chosen endpoint
    has been deleted, when it belongs to somebody else, or when it is disabled.
    The candidate is built inside the session because reading an `Endpoint`'s
    parameter columns needs the row attached.
    """
    from app.services.routing import _endpoint_to_candidate

    async with async_session() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = result.scalar_one_or_none()
        if user_settings is None or not user_settings.assistant_endpoint_id:
            return None

        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == user_settings.assistant_endpoint_id,
                Endpoint.user_id == user_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()
        if endpoint is None:
            return None

        model = user_settings.assistant_model or endpoint.default_model or ""
        user_layer = parse_params(
            user_settings.assistant_parameters_json, scope="user settings (assistant)"
        )
        candidate = _endpoint_to_candidate(endpoint, model)

    return candidate, model, user_layer


async def call_chat(
    candidate: FailoverEndpoint,
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    user_layer: list[ParamDef] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    trace_body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, float]:
    """One chat completion. Returns (data, status_code, latency_ms)."""
    from app.services.debug_metrics import PURPOSE_ASSISTANT, record_llm_call
    from app.services.proxy import _build_upstream_url, _do_forward

    layers: list[tuple[str, list[ParamDef]]] = [
        ("user settings (assistant)", list(user_layer or [])),
        (f"endpoint '{candidate.endpoint_name}'", list(candidate.parameters or [])),
        (f"model '{model}'", list(candidate.params_for_model(model))),
    ]
    resolved = resolve(layers)

    body: dict[str, Any] = {"model": model, "messages": messages}
    body = apply_to_body(body, resolved)

    # After apply_to_body, deliberately: these three are the pipeline's, not
    # the user's, and a configured layer must not be able to move them.
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    body["stream"] = False

    url = _build_upstream_url(
        candidate.base_url, "/v1/chat/completions", candidate.api_base_path
    )
    headers = {
        "Authorization": f"Bearer {candidate.api_key}",
        "Content-Type": "application/json",
    }
    httpx_timeout = httpx.Timeout(timeout, connect=15.0)

    started = time.monotonic()
    try:
        data, status_code = await _do_forward(
            "POST",
            url,
            headers,
            json.dumps(body).encode(),
            httpx_timeout,
            provider=candidate.provider,
            base_url=candidate.base_url,
            api_key=candidate.api_key,
            configured=resolved,
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - started) * 1000.0
        logger.exception("Assistant call failed on endpoint '%s'", candidate.endpoint_name)
        record_llm_call(
            trace_body or {},
            purpose=PURPOSE_ASSISTANT,
            latency_ms=latency_ms,
            status_code=0,
            response_data=None,
            messages=messages,
            endpoint_id=candidate.endpoint_id,
            endpoint_name=candidate.endpoint_name,
            provider=candidate.provider,
            model_requested=model,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return (
            {"error": {"message": f"{type(exc).__name__}: {exc}", "type": "assistant_error"}},
            0,
            latency_ms,
        )

    latency_ms = (time.monotonic() - started) * 1000.0
    record_llm_call(
        trace_body or {},
        purpose=PURPOSE_ASSISTANT,
        latency_ms=latency_ms,
        status_code=status_code,
        response_data=data,
        messages=messages,
        endpoint_id=candidate.endpoint_id,
        endpoint_name=candidate.endpoint_name,
        provider=candidate.provider,
        model_requested=model,
    )
    return data, status_code, latency_ms
