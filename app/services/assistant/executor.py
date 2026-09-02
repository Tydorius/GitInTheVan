"""Runs one tool.

HTTP tools are dispatched in-process against the running app with the caller's
own bearer token, so every ownership check, validator and content guard that
protects the web UI protects the assistant too, with no adapter layer and no
second copy of the rules.

The executor never sees a path from the model. It takes the registered tool's
own template and fills the placeholders, each URL-encoded on its own, so a
value containing a slash cannot become a path segment.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.assistant.context import ToolContext
from app.services.assistant.registry import Tool
from app.services.assistant.schema import path_param_names

logger = logging.getLogger(__name__)

# The base URL is a marker, not a destination: the ASGI transport never opens a
# socket. It exists so httpx can build a request line.
INTERNAL_BASE_URL = "http://assistant.internal"

# Serialized arguments larger than this are refused without dispatch. A model
# that has produced 32 KB of JSON arguments has lost the thread, and the
# routers' own size limits would reject it a moment later anyway.
MAX_ARGS_BYTES = 32 * 1024

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# Redaction patterns, applied to every tool result and to any log text.
# Deliberately blunt: a false positive costs the model a little context, a
# false negative puts a live credential in a conversation transcript.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+"), "[redacted]"),
    (re.compile(r"\bgitv_[A-Za-z0-9_-]{4,}"), "[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "[redacted]"),
)

# key=value / "key": "value" shapes. The value is replaced in place so a JSON
# document survives redaction as valid JSON.
_KV_PATTERN = re.compile(
    r"((?:api[_-]?key|password|secret|token)\"?\s*[=:]\s*\"?)([^\s\"',}\]]+)",
    re.IGNORECASE,
)

# Values that are already redaction markers, left alone so the mandatory
# endpoint projection stays stable and testable.
_ALREADY_SAFE = frozenset({"***", "[redacted]", "null", "true", "false"})


class AuthExpired(Exception):  # noqa: N818 - the name is the event, not an error class
    """An internal call came back 401: the caller's token is no longer good.

    Raised rather than returned because there is nothing the model can do with
    it -- the turn ends and the pane asks the user to sign in again.
    """


@dataclass
class ToolOutcome:
    """The result of one tool call, ready to become a `tool` message."""

    ok: bool
    result: Any
    status: int = 0
    truncated: bool = False
    duration_ms: float = 0.0


class _InternalApp:
    """ASGI wrapper that marks a request as originating inside this process.

    `rate_limit_middleware` honours `scope["gitv_internal"]`. The flag is
    unforgeable from outside -- an HTTP client cannot set an ASGI scope key --
    and the number of internal calls is bounded by the per-turn tool cap.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope = dict(scope)
            scope["gitv_internal"] = True
        await self._app(scope, receive, send)


def _kv_replace(match: re.Match[str]) -> str:
    value = match.group(2)
    if value in _ALREADY_SAFE:
        return match.group(0)
    return f"{match.group(1)}[redacted]"


def redact_text(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Scrub credentials from arbitrary text.

    Exact secrets first -- the user's own live endpoint keys, which may be in
    any format at all -- then the shape-based patterns for everything else.
    """
    if not text:
        return text
    out = text
    for secret in secrets or ():
        if secret and len(secret) >= 4:
            out = out.replace(secret, "[redacted]")
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return _KV_PATTERN.sub(_kv_replace, out)


def _redact_one_endpoint(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    raw = out.get("api_key")
    out["api_key"] = "***"
    out["api_key_set"] = bool(raw)
    return out


def redact_endpoints(result: Any) -> Any:
    """Mandatory projection over anything shaped like an endpoint payload.

    Applied before the generic redaction, so the model is told whether a key is
    configured without ever being shown one. Handles a single endpoint, a bare
    list, and the `{"endpoints": [...]}` list response.
    """
    if isinstance(result, list):
        return [_redact_one_endpoint(i) if isinstance(i, dict) else i for i in result]
    if isinstance(result, dict):
        if isinstance(result.get("endpoints"), list):
            out = dict(result)
            out["endpoints"] = [
                _redact_one_endpoint(i) if isinstance(i, dict) else i
                for i in result["endpoints"]
            ]
            return out
        if "api_key" in result:
            return _redact_one_endpoint(result)
    return result


def _split_args(tool: Tool, args: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """Split the model's arguments into path, query and body parts."""
    path_names = set(path_param_names(tool.path))
    query_names = set(tool.query_params or {})

    path_values: dict[str, str] = {}
    query_values: dict[str, Any] = {}
    body_values: dict[str, Any] = {}

    for key, value in (args or {}).items():
        if key in path_names:
            path_values[key] = str(value)
        elif key in query_names:
            if value is not None:
                query_values[key] = value
        else:
            body_values[key] = value

    return path_values, query_values, body_values


def bad_path_values(path_values: dict[str, str]) -> list[str]:
    """Path parameter values that could change the shape of the URL.

    Percent-encoding alone is not enough here. `httpx.ASGITransport` builds the
    ASGI scope from the *decoded* path, so an encoded slash comes back as a
    real separator before Starlette routes it -- `%2F..%2F` would leave the
    tool's own route template entirely. Values are therefore rejected outright
    rather than escaped, which is correct anyway: every id in this product is a
    UUID or a short slug, and none of them contains a separator.
    """
    bad: list[str] = []
    for name, value in path_values.items():
        text = str(value)
        if not text or "/" in text or "\\" in text or "%" in text or text in (".", ".."):
            bad.append(name)
    return bad


def _build_path(template: str, path_values: dict[str, str]) -> str:
    """Fill a route template. Each value is encoded on its own."""
    out = template
    for name in path_param_names(template):
        raw = path_values.get(name, "")
        out = out.replace("{" + name + "}", urllib.parse.quote(str(raw), safe=""))
    return out


def _coerce_query(values: dict[str, Any]) -> dict[str, str]:
    """Query values as strings; booleans in the form FastAPI parses."""
    out: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        else:
            out[key] = str(value)
    return out


def _error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except Exception:
        return response.text[:2000]
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    return payload


async def _dispatch(
    ctx: ToolContext,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[Any, int]:
    """One in-process HTTP call as the caller. Returns (payload, status)."""
    transport = httpx.ASGITransport(app=_InternalApp(ctx.app))
    headers = {
        "Authorization": f"Bearer {ctx.bearer_token}",
        "X-GITV-Assistant": "1",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url=INTERNAL_BASE_URL, timeout=120.0
    ) as client:
        response = await client.request(
            method, path, params=query or None, json=body, headers=headers
        )

    if response.status_code == 401:
        raise AuthExpired("Internal call rejected the caller's token")

    if response.status_code == 204 or not response.content:
        return ({"ok": True} if response.is_success else {"detail": ""}), response.status_code

    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text}

    if not response.is_success:
        return {"error": {"status": response.status_code, "detail": _error_detail(response)}}, response.status_code

    return payload, response.status_code


def _finish(ctx: ToolContext, result: Any) -> tuple[Any, bool]:
    """Redact and truncate. Returns (result, truncated)."""
    try:
        serialized = json.dumps(result, default=str)
    except Exception:
        serialized = str(result)

    scrubbed = redact_text(serialized, ctx.secrets)

    limit_kb = int(getattr(ctx.admin, "max_assistant_tool_result_kb", 32) or 32)
    limit = max(1, limit_kb) * 1024
    if len(scrubbed) > limit:
        return (
            {
                "truncated": True,
                "note": "Result truncated; narrow the query",
                "partial": scrubbed[:limit],
            },
            True,
        )

    try:
        return json.loads(scrubbed), False
    except Exception:
        # Redaction broke the JSON shape (a key pattern inside a string value).
        # The content still matters, so hand it over as text rather than losing
        # it -- silently dropping a tool result is worse than an odd shape.
        logger.warning("Redacted tool result did not re-parse as JSON; returning as text")
        return {"text": scrubbed}, False


async def read_current(ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> dict | None:
    """Read the object a write tool is about to change, for the diff card.

    Best-effort by design: a failure here must never stop a confirmation from
    being offered, so anything that goes wrong returns None.
    """
    if not tool.current_reader:
        return None
    method, template = tool.current_reader
    path_values = {
        name: str(args.get(name, "")) for name in path_param_names(template)
    }
    try:
        payload, status = await _dispatch(ctx, method, _build_path(template, path_values))
    except AuthExpired:
        raise
    except Exception:
        logger.warning("current_reader failed for tool '%s'", tool.name, exc_info=True)
        return None
    if status >= 400:
        return None
    result, _ = _finish(ctx, redact_endpoints(payload) if "endpoint" in tool.name else payload)
    return result if isinstance(result, dict) else {"current": result}


async def execute(ctx: ToolContext, tool: Tool, args: dict[str, Any]) -> ToolOutcome:
    """Run one tool and return its outcome. Client tools never reach here."""
    started = time.monotonic()
    args = dict(args or {})

    try:
        args_size = len(json.dumps(args, default=str))
    except Exception:
        args_size = MAX_ARGS_BYTES + 1
    if args_size > MAX_ARGS_BYTES:
        return ToolOutcome(
            ok=False,
            result={
                "error": {
                    "status": 0,
                    "detail": f"Arguments too large ({args_size} bytes); the limit is {MAX_ARGS_BYTES}.",
                }
            },
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    if tool.project_args is not None:
        args = tool.project_args(args)

    if tool.kind == "client":
        # The loop handles these; reaching the executor means a caller bug.
        return ToolOutcome(
            ok=False,
            result={"error": {"status": 0, "detail": f"'{tool.name}' is performed by the UI, not the server."}},
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    if tool.kind == "local":
        if tool.handler is None:
            return ToolOutcome(
                ok=False,
                result={"error": {"status": 0, "detail": f"'{tool.name}' has no handler."}},
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
        try:
            raw = tool.handler(ctx, args)
            if hasattr(raw, "__await__"):
                raw = await raw
        except Exception as exc:
            logger.exception("Local assistant tool '%s' failed", tool.name)
            return ToolOutcome(
                ok=False,
                result={"error": {"status": 0, "detail": f"{type(exc).__name__}: {exc}"[:500]}},
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
        result, truncated = _finish(ctx, raw)
        return ToolOutcome(
            ok=True,
            result=result,
            status=200,
            truncated=truncated,
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    path_values, query_values, body_values = _split_args(tool, args)
    bad = bad_path_values({name: path_values.get(name, "") for name in path_param_names(tool.path)})
    if bad:
        return ToolOutcome(
            ok=False,
            result={
                "error": {
                    "status": 0,
                    "detail": (
                        f"Invalid value for {', '.join(sorted(bad))}: an id must be a single "
                        "path segment with no slashes or escapes."
                    ),
                }
            },
            duration_ms=(time.monotonic() - started) * 1000.0,
        )
    path = _build_path(tool.path, path_values)
    body = body_values if tool.method.upper() in _BODY_METHODS else None

    try:
        payload, status = await _dispatch(
            ctx,
            tool.method.upper(),
            path,
            query=_coerce_query(query_values),
            body=body,
        )
    except AuthExpired:
        raise
    except Exception as exc:
        logger.exception("Assistant tool '%s' failed", tool.name)
        return ToolOutcome(
            ok=False,
            result={"error": {"status": 0, "detail": f"{type(exc).__name__}: {exc}"[:500]}},
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    ok = 200 <= status < 300
    if ok and tool.project_result is not None:
        payload = tool.project_result(payload, args)
        if isinstance(payload, dict) and "error" in payload and len(payload) == 1:
            ok = False

    result, truncated = _finish(ctx, payload)
    return ToolOutcome(
        ok=ok,
        result=result,
        status=status,
        truncated=truncated,
        duration_ms=(time.monotonic() - started) * 1000.0,
    )
