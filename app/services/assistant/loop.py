"""The turn.

`run_turn` is an async generator of `(event_name, data)` pairs the router
renders as SSE frames. It is stateless between requests: every step is
committed to the conversation row *before* its event is emitted, so a reload or
an aborted stream lands on state that can be resumed. The only in-memory state
is a per-conversation lock, because two concurrent turns on one conversation
would interleave writes.

The permission decision is taken here and only here, before the executor is
reached. `pending_json` carries the per-turn tool counter, so a reload cannot
reset the cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.user_settings import UserSettings
from app.services.assistant import compaction as compaction_mod
from app.services.assistant import llm, prompt, store
from app.services.assistant.context import ToolContext
from app.services.assistant.executor import AuthExpired, execute, read_current
from app.services.assistant.permissions import effective, group_mode, parse_prefs, tool_mode
from app.services.assistant.registry import (
    GROUPS,
    NAV_PAGES,
    NAV_PARAM_KEYS,
    TOOLS,
    Tool,
    visible_tools,
)
from app.services.assistant.schema import schemas_for
from app.services.audit import log_action
from app.services.debug_metrics import extract_usage

logger = logging.getLogger(__name__)

CONTINUE_MARKER = "/continue"

# One lock per conversation, held for the length of a turn. A second stream on
# the same conversation is refused rather than queued: the user is looking at
# one pane, and a queued turn would run against state they cannot see.
_LOCKS: dict[str, asyncio.Lock] = {}

_ARGS_DIGEST_CHARS = 200


def _lock_for(conversation_id: str) -> asyncio.Lock:
    lock = _LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[conversation_id] = lock
    return lock


async def _load_prefs(user_id: str) -> dict[str, Any]:
    async with async_session() as db:
        result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        row = result.scalar_one_or_none()
        raw = row.assistant_permissions_json if row is not None else ""
        budget = row.assistant_context_tokens if row is not None else 64000
    return {"prefs": parse_prefs(raw), "budget": int(budget or 64000)}


async def _audit(ctx: ToolContext, action: str, tool_name: str, details: dict[str, Any]) -> None:
    """Record one assistant action. Never allowed to break the turn."""
    try:
        async with async_session() as db:
            await log_action(
                db,
                user_id=ctx.user_id,
                action=action,
                target_type="assistant_tool",
                target_id=tool_name[:64],
                details=json.dumps(details, default=str)[:2000],
            )
            await db.commit()
    except Exception:
        logger.exception("Assistant audit write failed for %s/%s", action, tool_name)


def _args_digest(args: dict[str, Any], secrets: tuple[str, ...]) -> str:
    from app.services.assistant.executor import redact_text

    try:
        text = json.dumps(args, default=str)
    except Exception:
        text = str(args)
    return redact_text(text, secrets)[:_ARGS_DIGEST_CHARS]


def _parse_call(call: dict[str, Any]) -> tuple[str, str, dict[str, Any], str | None]:
    """(call_id, name, args, error). `error` is set when arguments are junk."""
    call_id = str(call.get("id") or "")
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return call_id, name, raw, None
    if not raw:
        return call_id, name, {}, None
    try:
        parsed = json.loads(raw)
    except Exception:
        return call_id, name, {}, "Arguments were not valid JSON."
    if not isinstance(parsed, dict):
        return call_id, name, {}, "Arguments must be a JSON object."
    return call_id, name, parsed, None


def _tool_message(call_id: str, name: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str)
        except Exception:
            payload = str(payload)
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": payload}


def _validate_navigate(args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Check a `navigate` payload against the pages the UI actually has."""
    page = str(args.get("page") or "")
    if page not in NAV_PAGES:
        return None, f"Unknown page '{page}'. Valid pages: {', '.join(NAV_PAGES)}."
    raw_params = args.get("params") or {}
    if not isinstance(raw_params, dict):
        return None, "params must be an object."
    bad = [k for k in raw_params if k not in NAV_PARAM_KEYS]
    if bad:
        return None, f"Unsupported params {bad}; allowed keys are {sorted(NAV_PARAM_KEYS)}."
    return {"page": page, "params": {k: str(v) for k, v in raw_params.items()}}, None


def _title_from(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            text = message["content"].strip()
            if text and text != CONTINUE_MARKER:
                return text[:store.TITLE_LIMIT]
    return ""


async def run_turn(
    ctx: ToolContext,
    *,
    user_message: str | None = None,
    decision: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Drive one turn. Yields `(event_name, data)` until a terminal event."""
    lock = _lock_for(ctx.conversation_id)
    if lock.locked():
        yield "error", {"code": "busy", "message": "This conversation already has a turn running."}
        return

    async with lock:
        async for event in _run_locked(ctx, user_message=user_message, decision=decision):
            yield event


async def _run_locked(  # noqa: C901 - the turn algorithm is one readable sequence
    ctx: ToolContext,
    *,
    user_message: str | None,
    decision: dict[str, Any] | None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    row = await store.get(ctx.user_id, ctx.conversation_id)
    if row is None:
        yield "error", {"code": "not_found", "message": "Conversation not found."}
        return

    loaded = await _load_prefs(ctx.user_id)
    prefs: dict[str, Any] = loaded["prefs"]
    budget_tokens: int = loaded["budget"]

    messages: list[dict[str, Any]] = store.loads(row.messages_json, [])
    compaction: dict[str, Any] = store.loads(row.compaction_json, {}) or {}
    pending: dict[str, Any] | None = store.loads(row.pending_json, None)
    session_allows: set[str] = set(store.loads(row.session_allows_json, []) or [])
    yolo = bool(row.yolo)
    title = row.title or ""

    cap = max(1, int(getattr(ctx.admin, "max_assistant_tool_calls_per_turn", 16) or 16))
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "tool_calls": 0}

    visible = visible_tools(ctx)
    allowed: list[Tool] = []
    denied: list[tuple[Tool, Any]] = []
    for tool in visible:
        group = GROUPS[tool.group]
        if effective(tool, group, prefs, session_allows=session_allows, yolo=yolo) == "deny":
            denied.append((tool, group))
        else:
            allowed.append(tool)
    allowed_names = {t.name for t in allowed}
    schemas = schemas_for(allowed)

    resolved = await llm.resolve_assistant_candidate(ctx.user_id)
    if resolved is None:
        yield "error", {
            "code": "no_endpoint",
            "message": "No assistant endpoint is configured. Choose one on the Settings page.",
        }
        return
    candidate, model, user_layer = resolved

    tool_calls_used = 0
    queue: list[dict[str, Any]] = []

    # ---- resume ----------------------------------------------------------
    if decision is not None:
        if not pending:
            yield "error", {"code": "no_pending", "message": "Nothing is waiting for a decision."}
            return
        if str(decision.get("call_id") or "") != str(pending.get("call_id") or ""):
            yield "error", {"code": "no_pending", "message": "That decision does not match the pending call."}
            return

        tool_calls_used = int(pending.get("tool_calls_used") or 0)
        queue = list(pending.get("queue") or [])
        call_id = str(pending.get("call_id") or "")
        name = str(pending.get("name") or "")
        args = dict(pending.get("args") or {})
        tool = TOOLS.get(name)

        if pending.get("kind") == "client":
            client_result = decision.get("client_result")
            messages.append(_tool_message(call_id, name, client_result if client_result is not None else {"ok": True}))
        else:
            choice = str(decision.get("decision") or "")
            note = str(decision.get("note") or "")
            if choice == "reject":
                messages.append(
                    _tool_message(
                        call_id,
                        name,
                        {"rejected": True, "detail": "The user rejected this call.", "note": note},
                    )
                )
            elif tool is None:
                messages.append(_tool_message(call_id, name, {"error": {"status": 0, "detail": "Unknown tool."}}))
            else:
                if choice == "allow_session":
                    session_allows.add(name)
                await _audit(
                    ctx,
                    "assistant_tool_approved",
                    name,
                    {"conversation_id": ctx.conversation_id, "call_id": call_id, "decision": choice},
                )
                try:
                    outcome = await execute(ctx, tool, args)
                except AuthExpired:
                    pending = None
                    await store.save_state(row.id, messages=messages, pending=None, session_allows=session_allows)
                    yield "error", {"code": "auth_expired", "message": "Your session expired. Sign in again."}
                    return
                usage_totals["tool_calls"] += 1
                messages.append(_tool_message(call_id, name, outcome.result))
                await _audit(
                    ctx,
                    "assistant_tool_call",
                    name,
                    {
                        "conversation_id": ctx.conversation_id,
                        "call_id": call_id,
                        "status": outcome.status,
                        "args_digest": _args_digest(args, ctx.secrets),
                    },
                )
                await store.save_state(row.id, messages=messages, pending=None, session_allows=session_allows)
                yield "tool_result", {
                    "call_id": call_id,
                    "name": name,
                    "ok": outcome.ok,
                    "status": outcome.status,
                    "truncated": outcome.truncated,
                    "result": outcome.result,
                }

        pending = None
        await store.save_state(row.id, messages=messages, pending=None, session_allows=session_allows)

    # ---- new message -----------------------------------------------------
    if user_message is not None:
        text = user_message
        if text.strip() == CONTINUE_MARKER:
            tool_calls_used = 0
            text = "Continue."
        messages.append({"role": "user", "content": text})
        if not title:
            title = _title_from(messages)
        await store.save_state(
            row.id,
            messages=messages,
            pending=None,
            title=title or None,
            last_route=str((ctx.route or {}).get("page") or "") or None,
        )

    yield "meta", {
        "conversation_id": ctx.conversation_id,
        "model": model,
        "endpoint_name": candidate.endpoint_name,
        "endpoint_id": candidate.endpoint_id,
        "tool_calls_used": tool_calls_used,
        "tool_call_cap": cap,
        "yolo": yolo,
        "tools_offered": len(schemas),
        "tools_denied": len(denied),
    }

    # ---- the loop --------------------------------------------------------
    while True:
        if not queue:
            if tool_calls_used >= cap:
                async for event in _end_on_cap(ctx, row, messages, cap, usage_totals):
                    yield event
                return

            plan = compaction_mod.plan(messages, compaction, budget_tokens)
            if plan.needs_summary and plan.fold_through > 0:
                summary = await compaction_mod.summarize(
                    candidate,
                    model,
                    user_layer,
                    messages[: plan.fold_through],
                    str(compaction.get("summary") or ""),
                )
                usage_totals["llm_calls"] += 1
                if summary:
                    compaction = compaction_mod.apply(compaction, summary, plan.fold_through)
                    messages = compaction_mod.trim_stored(messages, plan.fold_through)
                    await store.save_state(row.id, messages=messages, compaction=compaction, pending=pending)
                    plan = compaction_mod.plan(messages, compaction, budget_tokens)
                else:
                    # Fail open, but never silently: the turn continues on
                    # elision alone and the user is told the context is thinner.
                    yield "error", {
                        "code": "compaction_failed",
                        "message": (
                            "Could not summarise the earlier part of this conversation; "
                            "continuing with older tool results elided."
                        ),
                        "terminal": False,
                    }

            system_prompt = prompt.build_system_prompt(
                ctx, allowed, denied, str(compaction.get("summary") or "") or None
            )
            outbound = [{"role": "system", "content": system_prompt}, *plan.messages]

            data, status, _latency = await llm.call_chat(
                candidate,
                model,
                outbound,
                tools=schemas,
                user_layer=user_layer,
                timeout=llm.DEFAULT_TIMEOUT_SECONDS,
            )
            usage_totals["llm_calls"] += 1

            usage = extract_usage(data) or {}
            usage_totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            usage_totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)

            if status != 200:
                await store.save_state(
                    row.id, messages=messages, compaction=compaction, pending=None,
                    session_allows=session_allows, usage_delta=usage_totals,
                )
                yield "usage", dict(usage_totals)
                yield "error", {
                    "code": "upstream",
                    "status": status,
                    "message": _upstream_message(data),
                }
                return

            message = _assistant_message(data)
            messages.append(message)
            await store.save_state(
                row.id, messages=messages, compaction=compaction, pending=None,
                session_allows=session_allows,
            )
            yield "assistant_message", {
                "content": message.get("content") or "",
                "tool_calls": message.get("tool_calls") or [],
            }

            calls = message.get("tool_calls") or []
            if not calls:
                await store.save_state(row.id, usage_delta=usage_totals, pending=None)
                yield "usage", dict(usage_totals)
                yield "done", {"reason": "stop"}
                return
            queue = list(calls)

        # ---- drain the calls in order ------------------------------------
        while queue:
            call = queue.pop(0)
            call_id, name, args, arg_error = _parse_call(call)

            if tool_calls_used >= cap:
                async for event in _end_on_cap(ctx, row, messages, cap, usage_totals):
                    yield event
                return
            tool_calls_used += 1

            tool = TOOLS.get(name)
            if tool is None:
                messages.append(
                    _tool_message(call_id, name, {"error": {"status": 0, "detail": f"No tool named '{name}'."}})
                )
                await store.save_state(row.id, messages=messages, pending=None)
                yield "tool_result", {"call_id": call_id, "name": name, "ok": False, "status": 0,
                                      "truncated": False, "result": {"error": {"detail": f"No tool named '{name}'."}}}
                continue

            if arg_error is not None:
                messages.append(_tool_message(call_id, name, {"error": {"status": 0, "detail": arg_error}}))
                await store.save_state(row.id, messages=messages, pending=None)
                yield "tool_result", {"call_id": call_id, "name": name, "ok": False, "status": 0,
                                      "truncated": False, "result": {"error": {"detail": arg_error}}}
                continue

            group = GROUPS[tool.group]
            yield "tool_call", {"call_id": call_id, "name": name, "args": args, "risk": str(tool.risk)}

            if name not in allowed_names:
                text = (
                    "Forbidden by the user's security settings. The user can change "
                    f"this on {group.label}."
                )
                messages.append(_tool_message(call_id, name, {"denied": True, "detail": text}))
                await _audit(
                    ctx,
                    "assistant_tool_denied",
                    name,
                    {
                        "conversation_id": ctx.conversation_id,
                        "call_id": call_id,
                        "args_digest": _args_digest(args, ctx.secrets),
                    },
                )
                await store.save_state(row.id, messages=messages, pending=None)
                yield "tool_result", {"call_id": call_id, "name": name, "ok": False, "status": 0,
                                      "truncated": False, "result": {"denied": True, "detail": text}}
                continue

            verdict = effective(tool, group, prefs, session_allows=session_allows, yolo=yolo)
            # An explicit Always Ask is a security setting; the pane must not offer
            # to silence it for the session.
            own_mode = tool_mode(tool, prefs)
            can_allow_session = own_mode != "always_ask" and not (
                own_mode == "inherit" and group_mode(group, prefs) == "always_ask"
            )

            if verdict == "ask":
                current = None
                try:
                    current = await read_current(ctx, tool, args)
                except AuthExpired:
                    await store.save_state(row.id, messages=messages, pending=None)
                    yield "error", {"code": "auth_expired", "message": "Your session expired. Sign in again."}
                    return
                pending = {
                    "turn_id": call_id,
                    "tool_calls_used": tool_calls_used,
                    "queue": queue,
                    "kind": "confirm",
                    "call_id": call_id,
                    "name": name,
                    "args": args,
                    "current": current,
                    "summary": tool.summary,
                    "risk": str(tool.risk),
                    "group": group.label,
                    "page": group.route,
                    "can_allow_session": can_allow_session,
                }
                await store.save_state(
                    row.id, messages=messages, compaction=compaction, pending=pending,
                    session_allows=session_allows, usage_delta=usage_totals,
                )
                yield "usage", dict(usage_totals)
                yield "awaiting_confirmation", {
                    "call_id": call_id,
                    "name": name,
                    "summary": tool.summary,
                    "risk": str(tool.risk),
                    "group": group.label,
                    "page": group.route,
                    "args": args,
                    "current": current,
                    "can_allow_session": can_allow_session,
                }
                return

            if tool.kind == "client":
                payload, error = _validate_navigate(args) if name == "navigate" else (args, None)
                if error is not None:
                    messages.append(_tool_message(call_id, name, {"error": {"status": 0, "detail": error}}))
                    await store.save_state(row.id, messages=messages, pending=None)
                    yield "tool_result", {"call_id": call_id, "name": name, "ok": False, "status": 0,
                                          "truncated": False, "result": {"error": {"detail": error}}}
                    continue
                pending = {
                    "turn_id": call_id,
                    "tool_calls_used": tool_calls_used,
                    "queue": queue,
                    "kind": "client",
                    "call_id": call_id,
                    "name": name,
                    "args": payload,
                    "current": None,
                }
                await store.save_state(
                    row.id, messages=messages, compaction=compaction, pending=pending,
                    session_allows=session_allows, usage_delta=usage_totals,
                )
                yield "usage", dict(usage_totals)
                yield "client_action", {"call_id": call_id, "name": name, "args": payload}
                yield "awaiting_client", {"call_id": call_id, "name": name}
                return

            try:
                outcome = await execute(ctx, tool, args)
            except AuthExpired:
                await store.save_state(row.id, messages=messages, pending=None)
                yield "error", {"code": "auth_expired", "message": "Your session expired. Sign in again."}
                return

            usage_totals["tool_calls"] += 1
            messages.append(_tool_message(call_id, name, outcome.result))
            await _audit(
                ctx,
                "assistant_tool_call",
                name,
                {
                    "conversation_id": ctx.conversation_id,
                    "call_id": call_id,
                    "status": outcome.status,
                    "args_digest": _args_digest(args, ctx.secrets),
                },
            )
            await store.save_state(row.id, messages=messages, compaction=compaction, pending=None)
            yield "tool_result", {
                "call_id": call_id,
                "name": name,
                "ok": outcome.ok,
                "status": outcome.status,
                "truncated": outcome.truncated,
                "result": outcome.result,
            }


def _assistant_message(data: dict[str, Any]) -> dict[str, Any]:
    """The assistant message from an upstream response, verbatim.

    Kept as the provider sent it (minus keys the transcript has no use for) so
    the next request echoes back exactly the `tool_calls` structure the
    provider expects to see.
    """
    try:
        raw = data["choices"][0]["message"]
    except Exception:
        raw = {}
    message: dict[str, Any] = {"role": "assistant", "content": raw.get("content") or ""}
    calls = raw.get("tool_calls")
    if calls:
        message["tool_calls"] = calls
    reasoning = raw.get("reasoning_content") or raw.get("thinking")
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _upstream_message(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:500]
        if error:
            return str(error)[:500]
    return str(data)[:500]


async def _end_on_cap(
    ctx: ToolContext,
    row: Any,
    messages: list[dict[str, Any]],
    cap: int,
    usage_totals: dict[str, int],
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """End the turn on the per-turn tool cap.

    Not a kill switch: a person in the chair presses Continue and a fresh turn
    starts with a fresh counter, while an unattended loop stops here.
    """
    note = (
        f"Stopped after {cap} tool calls in one turn, the configured limit. "
        "The user can press Continue to carry on."
    )
    messages.append({"role": "user", "content": note})
    await store.save_state(row.id, messages=messages, pending=None, usage_delta=usage_totals)
    yield "usage", dict(usage_totals)
    yield "done", {"reason": "tool_cap", "message": note}
