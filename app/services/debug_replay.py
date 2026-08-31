"""Re-run a stored debug exchange through the current configuration.

Debug is a passive tap on live proxy traffic, so without replay every A/B cycle
means returning to the chat client and resending the same message by hand. This
re-sends a run's stored ``original_messages`` and links the result back to the
run it came from, which is what makes iterating on a cantrip or a map cheap.

The replayed request goes through the *real* pipeline -- the same
``forward_request`` a client hits -- because a replay that took a shortcut would
stop being evidence about what the pipeline does.

**What replay isolates, and what it does not.**

Isolated: the conversation. A synthetic chat id keeps summaries, conversation
hashes and memory extraction away from the real conversation, and memory and
summarization *writes* are skipped while their read paths still run, so
injections behave identically.

Not isolated: cantrip side effects. A cantrip that mutates ``user_data`` or
``cantrip_data``, rolls a die, or moves a counter will do so again. Sandboxing
that would mean intercepting every persistence call in the Deno bridge, and a
sandboxed cantrip would no longer be the thing under test. The UI says so on the
button rather than implying a safety that does not exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from app.database import async_session
from app.services.debug import get_exchange, latest_exchange_id

logger = logging.getLogger(__name__)


class ReplayError(Exception):
    """Replay could not start. The message is safe to show the user."""


async def replay_exchange(user_id: str, exchange_id: str) -> str:
    """Replay a run. Returns the id of the new run it produced.

    Raises ReplayError when the source run is missing, carries no recoverable
    messages, or the user has no endpoint to send it to.
    """
    source = await get_exchange(user_id, exchange_id)
    if not source:
        raise ReplayError("Run not found")

    messages = _original_messages(source)
    if not messages:
        raise ReplayError(
            "This run has no recoverable original messages, so it cannot be replayed."
        )

    from app.services.routing import resolve_routing_for_user

    async with async_session() as db:
        routing = await resolve_routing_for_user(user_id, db)
    if routing is None:
        raise ReplayError("You have no enabled endpoint to replay against.")

    target = (
        routing.base_url, routing.api_key, routing.user_id,
        routing.api_base_path, routing.bypass_method, routing.provider,
        routing.model, routing.endpoint_id, routing.failover_chain,
    )

    body = {
        # The client's own parameters, so the replay sends what the original run
        # sent rather than a bare body the configured layers then write over.
        **_original_params(source),
        "model": source.get("model", "") or routing.model,
        "messages": messages,
        # Replay is always buffered. A streamed replay would return before the
        # exchange was saved, and the caller needs the new run's id to link it.
        "stream": False,
        "_gitv_replay": True,
        "_gitv_replay_of": exchange_id,
    }

    # Scoped to the synthetic chat, so a run created by other traffic during
    # the replay cannot be mistaken for its result.
    replay_chat_id = f"replay:{exchange_id}"
    before = await latest_exchange_id(user_id, replay_chat_id)

    from app.services.proxy import forward_request

    response = await forward_request(_synthetic_request(body), target=target)
    status_code = getattr(response, "status_code", 500)

    after = await latest_exchange_id(user_id, replay_chat_id)
    if after and after != before:
        return after

    # The pipeline ran but produced no exchange. Almost always debug mode being
    # off; say which rather than returning a bare failure.
    if status_code != 200:
        raise ReplayError(
            f"The replayed request failed upstream (HTTP {status_code}) and no run was recorded."
        )
    raise ReplayError(
        "The replay completed but no debug run was recorded. "
        "Check that debug mode is still enabled in Settings."
    )


def _original_params(exchange: dict[str, Any]) -> dict[str, Any]:
    """The client's own body parameters from the source run.

    Absent on any run captured before this was stored, which is the normal case
    for existing traces -- an empty dict just means the replay behaves as it did.
    """
    params = exchange.get("pipeline_data", {}).get("original_params")
    if not isinstance(params, dict):
        return {}
    return {
        k: v for k, v in params.items()
        if k not in ("messages", "model", "stream") and not k.startswith("_gitv")
    }


def _original_messages(exchange: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages as the client originally sent them.

    Stored as a JSON string on the trace. Falls back to the first stage's
    ``messages_before`` for a trace whose snapshots were shed by the size cap.
    """
    pipeline = exchange.get("pipeline_data", {})

    for candidate in (
        pipeline.get("original_messages"),
        *(s.get("messages_before") for s in pipeline.get("stages", [])[:1]),
    ):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate) if isinstance(candidate, str) else candidate
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list) and parsed:
            return parsed
    return []


def _synthetic_request(body: dict[str, Any]) -> Request:
    """A minimal ASGI request carrying the replay body.

    Routing is passed in separately, so this needs no credentials -- which is
    the point: API keys are stored hashed and there is no plaintext to forge.
    """
    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)
