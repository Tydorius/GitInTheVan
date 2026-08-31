from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from app.database import async_session
from app.models.debug_exchange import DebugExchange
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

MAX_DEBUG_EXCHANGES = 20

# Bumped when the stored pipeline_data shape changes. 1 = pre-Phase-22 traces,
# which have no `run` block; readers must tolerate both.
SCHEMA_VERSION = 2


async def is_debug_mode(user_id: str) -> bool:
    """Check if debug mode is enabled for a user."""
    async with async_session() as db:
        result = await db.execute(
            select(UserSettings.debug_mode).where(UserSettings.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        return bool(row)


def _run_source(body_json: dict[str, Any]) -> str:
    """Where this run came from: live traffic, a replay, or a sandbox.

    Surfaced in the run list and every comparison column, because a sandbox run
    reads a forked conversation and a replay reads a throwaway one -- neither is
    directly comparable to live traffic without saying so.
    """
    if body_json.get("_gitv_sandbox"):
        return "sandbox"
    if body_json.get("_gitv_replay"):
        return "replay"
    return "live"


def original_client_params(body_json: dict[str, Any]) -> dict[str, Any]:
    """The client's own top-level body keys, minus the ones the pipeline owns.

    A replay that rebuilds the body as `{model, messages, stream}` drops whatever
    sampling the client sent, so it stops reproducing the run it claims to. These
    are captured before any stage writes to the body.

    JSON-safe values only: the trace is serialized, and a `_gitv` key can hold a
    live `FailoverEndpoint`.
    """
    return {
        k: v
        for k, v in body_json.items()
        if k not in ("messages", "model", "stream")
        and not k.startswith("_gitv")
        and isinstance(v, (str, int, float, bool, list, dict, type(None)))
    }


def init_debug(body_json: dict[str, Any], tags: list) -> None:
    """Initialize the debug container on body_json.

    Stores original message snapshot, extracted tags, and the ``run`` block that
    ``debug_metrics`` fills with per-call token, latency and endpoint records.

    ``original_params`` is additive: every reader reaches it through ``.get()``
    with a default and nothing branches on its presence, so it does not need a
    ``schema_version`` bump -- a schema 2 trace without it stays readable.
    """
    body_json["_gitv_debug"] = {
        "schema_version": SCHEMA_VERSION,
        "stages": [],
        "original_messages": json.dumps(body_json.get("messages", []), default=str),
        "original_params": original_client_params(body_json),
        "tags": [t.get("raw", "") for t in tags] if tags else [],
        "run": {
            "started_at": datetime.now(UTC).isoformat(),
            "source": _run_source(body_json),
            "replay_of": body_json.get("_gitv_replay_of", ""),
            "sandbox_id": body_json.get("_gitv_sandbox_id", ""),
            "llm_calls": [],
            "cantrips": [],
            "totals": {},
        },
    }


def debug_capture(
    body_json: dict[str, Any],
    name: str,
    label: str,
    *,
    item_id: str | None = None,
    item_name: str | None = None,
    detail: str = "",
    setting: str = "",
    setting_value: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Snapshot messages before/after a pipeline stage.

    Call this immediately AFTER a stage completes. The function captures
    the current message state as ``messages_after`` and the last stage's
    ``messages_after`` (or the original snapshot) as ``messages_before``.
    """
    debug = body_json.get("_gitv_debug")
    if debug is None:
        return

    stages = debug.get("stages", [])
    if stages:
        messages_before = stages[-1].get("messages_after")
    else:
        messages_before = debug.get("original_messages", "[]")

    messages_after = json.dumps(body_json.get("messages", []), default=str)

    stages.append({
        "name": name,
        "label": label,
        "item_id": item_id,
        "item_name": item_name,
        "detail": detail,
        "setting": setting,
        "setting_value": setting_value,
        "messages_before": messages_before,
        "messages_after": messages_after,
        "metadata": metadata or {},
    })
    debug["stages"] = stages


def debug_capture_response(
    body_json: dict[str, Any],
    name: str,
    label: str,
    *,
    content_before: str = "",
    content_after: str = "",
    detail: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Capture a response-side pipeline stage (post-LLM).

    Response stages track content transformation rather than messages.
    """
    debug = body_json.get("_gitv_debug")
    if debug is None:
        return

    debug.setdefault("stages", []).append({
        "name": name,
        "label": label,
        "item_id": None,
        "item_name": None,
        "detail": detail,
        "setting": "",
        "setting_value": None,
        "messages_before": None,
        "messages_after": None,
        "content_before": content_before,
        "content_after": content_after,
        "metadata": metadata or {},
    })


async def _serialize_pipeline(pipeline_data: dict[str, Any]) -> str:
    """Serialize a trace, bounding it by the admin size cap.

    Reasoning and response content are no longer truncated at capture time --
    a comparison built on silently clipped text is worse than no comparison.
    The bound is applied to the whole serialized trace instead, and when it
    bites it is *recorded*: the trace grows a `truncated` marker naming what was
    dropped and why, so a missing stage is visible rather than mysterious.

    Per-stage message snapshots are shed first. They are the storage hot spot --
    the full message list is stored twice per stage -- and they are the most
    reconstructible, since the stage before and after still carry theirs.
    """
    blob = json.dumps(pipeline_data, default=str)

    from app.services.admin import get_caps
    try:
        cap_kb = (await get_caps()).get("max_debug_exchange_kb", 512)
    except Exception:
        logger.debug("Could not read max_debug_exchange_kb; leaving trace unbounded")
        return blob

    cap_bytes = max(1, int(cap_kb)) * 1024
    if len(blob) <= cap_bytes:
        return blob

    trimmed = json.loads(blob)
    shed = 0
    for stage in trimmed.get("stages", []):
        for key in ("messages_before", "messages_after"):
            if stage.get(key):
                shed += len(stage[key])
                stage[key] = None
        stage["messages_dropped"] = True

    trimmed["truncated"] = True
    trimmed["truncated_reason"] = (
        f"Trace was {len(blob) // 1024} KB, over the {cap_kb} KB admin limit. "
        f"Per-stage message snapshots were dropped ({shed // 1024} KB). "
        "Reasoning, cantrip output and metrics are intact."
    )

    blob = json.dumps(trimmed, default=str)
    if len(blob) > cap_bytes:
        # Still over after shedding snapshots: the cantrip payloads or reasoning
        # are themselves oversized. Keep the run block and stage headers, which
        # is what the comparison view needs, and say so.
        for stage in trimmed.get("stages", []):
            stage["metadata"] = {"dropped": "oversized"}
            stage["content_before"] = ""
            stage["content_after"] = ""
        trimmed["truncated_reason"] += " Stage metadata was dropped as well."
        blob = json.dumps(trimmed, default=str)

    logger.info("Debug trace exceeded %d KB cap; snapshots shed", cap_kb)
    return blob


async def capture_exchange(
    user_id: str,
    chat_id: str,
    model: str,
    pipeline_data: dict[str, Any],
    response_content: str,
    verification_data: dict[str, Any] | None = None,
) -> None:
    """Store a debug exchange with full pipeline visibility.

    Prunes to the retention limit, skipping saved runs -- a run the user pinned
    as a comparison baseline must not be evicted by later traffic.
    """
    async with async_session() as db:
        exchange = DebugExchange(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            pipeline_data=await _serialize_pipeline(pipeline_data),
            response_content=response_content,
            verification_data=json.dumps(verification_data or {}, default=str),
        )
        db.add(exchange)
        await db.flush()

        old_result = await db.execute(
            select(DebugExchange.id)
            .where(
                DebugExchange.user_id == user_id,
                DebugExchange.saved.is_(False),
            )
            .order_by(DebugExchange.created_at.desc())
            .offset(MAX_DEBUG_EXCHANGES)
        )
        old_ids = [row[0] for row in old_result.fetchall()]

        if old_ids:
            await db.execute(
                delete(DebugExchange).where(DebugExchange.id.in_(old_ids))
            )

        await db.commit()

    logger.debug("Debug exchange captured for user %s, chat %s", user_id[:8], chat_id[:12])


async def list_exchanges(
    user_id: str,
    limit: int = 20,
    saved_only: bool = False,
) -> list[dict[str, Any]]:
    """List debug exchanges for a user, newest first.

    Saved runs sort ahead of unsaved ones so a pinned baseline stays at the top
    of the picker instead of sinking as new traffic arrives.
    """
    async with async_session() as db:
        query = select(DebugExchange).where(DebugExchange.user_id == user_id)
        if saved_only:
            query = query.where(DebugExchange.saved.is_(True))
        result = await db.execute(
            query.order_by(
                DebugExchange.saved.desc(),
                DebugExchange.created_at.desc(),
            ).limit(limit)
        )
        exchanges = result.scalars().all()
        return [_serialize_exchange(e) for e in exchanges]


async def latest_exchange_id(user_id: str, chat_id: str = "") -> str:
    """Id of the most recently created run, optionally within one chat.

    Ordered strictly by creation time. `list_exchanges` sorts saved runs first
    so a pinned baseline stays at the top of the picker, which makes it the
    wrong thing to ask "what was just created" -- replay used it and got the
    saved baseline back both before and after, so it concluded nothing had run.
    """
    async with async_session() as db:
        query = select(DebugExchange.id).where(DebugExchange.user_id == user_id)
        if chat_id:
            query = query.where(DebugExchange.chat_id == chat_id)
        result = await db.execute(
            query.order_by(DebugExchange.created_at.desc()).limit(1)
        )
        row = result.scalar_one_or_none()
        return row or ""


async def set_saved(user_id: str, exchange_id: str, saved: bool, label: str = "") -> str:
    """Pin or unpin a run. Returns "" on success, or a reason for refusal.

    Refuses rather than evicting when the cap is reached: the user picked which
    runs matter, so silently dropping the oldest saved one would throw away a
    deliberate choice to make room for another.
    """
    from app.services.admin import get_caps

    async with async_session() as db:
        result = await db.execute(
            select(DebugExchange).where(
                DebugExchange.id == exchange_id,
                DebugExchange.user_id == user_id,
            )
        )
        exchange = result.scalar_one_or_none()
        if exchange is None:
            return "Run not found"

        if saved and not exchange.saved:
            cap = (await get_caps()).get("max_saved_debug_runs", 10)
            count_result = await db.execute(
                select(DebugExchange.id).where(
                    DebugExchange.user_id == user_id,
                    DebugExchange.saved.is_(True),
                )
            )
            current = len(count_result.fetchall())
            if current >= cap:
                return (
                    f"You have {current} saved runs and the limit is {cap}. "
                    "Unsave one first, or ask an admin to raise the limit."
                )

        exchange.saved = saved
        exchange.saved_at = datetime.now(UTC) if saved else None
        if saved and label:
            exchange.label = label[:128]
        elif not saved:
            exchange.label = ""
        await db.commit()
        return ""


async def set_label(user_id: str, exchange_id: str, label: str) -> bool:
    """Rename a run. Returns False when it does not exist."""
    async with async_session() as db:
        result = await db.execute(
            select(DebugExchange).where(
                DebugExchange.id == exchange_id,
                DebugExchange.user_id == user_id,
            )
        )
        exchange = result.scalar_one_or_none()
        if exchange is None:
            return False
        exchange.label = label[:128]
        await db.commit()
        return True


async def delete_exchange(user_id: str, exchange_id: str) -> bool:
    """Delete one run, saved or not. Returns False when it does not exist."""
    async with async_session() as db:
        result = await db.execute(
            delete(DebugExchange).where(
                DebugExchange.id == exchange_id,
                DebugExchange.user_id == user_id,
            )
        )
        await db.commit()
        return bool(result.rowcount)


async def get_exchange(user_id: str, exchange_id: str) -> dict[str, Any] | None:
    """Get a single debug exchange."""
    async with async_session() as db:
        result = await db.execute(
            select(DebugExchange).where(
                DebugExchange.id == exchange_id,
                DebugExchange.user_id == user_id,
            )
        )
        e = result.scalar_one_or_none()
        if not e:
            return None
        return _serialize_exchange(e)


async def clear_exchanges(user_id: str, include_saved: bool = False) -> int:
    """Delete a user's debug exchanges. Returns count deleted.

    Saved runs are kept unless explicitly included -- Clear All is reached for
    tidying up noise, and losing a pinned baseline to it would be a surprise.
    """
    async with async_session() as db:
        query = delete(DebugExchange).where(DebugExchange.user_id == user_id)
        if not include_saved:
            query = query.where(DebugExchange.saved.is_(False))
        result = await db.execute(query)
        await db.commit()
        return result.rowcount or 0


def _serialize_exchange(e: DebugExchange) -> dict[str, Any]:
    """Serialize a DebugExchange row to a dict, handling legacy formats."""
    try:
        pipeline_data = json.loads(e.pipeline_data) if e.pipeline_data else {}
    except Exception:
        pipeline_data = {}

    if "stages" not in pipeline_data:
        pipeline_data = _migrate_legacy_pipeline(pipeline_data)

    # A schema-1 trace predates the run block. Fill an empty one rather than
    # leaving the key absent, so every consumer -- UI, export, comparison --
    # reads one shape and does not have to null-check per field.
    if "run" not in pipeline_data:
        pipeline_data["run"] = {
            "started_at": "",
            "source": "live",
            "replay_of": "",
            "sandbox_id": "",
            "llm_calls": [],
            "cantrips": [],
            "totals": {},
        }
    pipeline_data.setdefault("schema_version", 1)

    return {
        "id": e.id,
        "chat_id": e.chat_id,
        "model": e.model,
        "label": e.label,
        "saved": e.saved,
        "saved_at": e.saved_at.isoformat() if e.saved_at else "",
        "pipeline_data": pipeline_data,
        "response_content": e.response_content,
        "verification_data": json.loads(e.verification_data) if e.verification_data else {},
        "created_at": e.created_at.isoformat() if e.created_at else "",
    }


def _migrate_legacy_pipeline(data: dict[str, Any]) -> dict[str, Any]:
    """Convert old-format pipeline_data (pre-stage system) to stage-based format."""
    stages: list[dict[str, Any]] = []
    original = data.get("original_messages", "")
    modified = data.get("modified_messages", "")

    try:
        orig_msgs = json.loads(original) if original else []
    except Exception:
        orig_msgs = []
    try:
        mod_msgs = json.loads(modified) if modified else []
    except Exception:
        mod_msgs = []

    if orig_msgs:
        stages.append({
            "name": "original",
            "label": "Original Messages",
            "item_id": None,
            "item_name": None,
            "detail": "Messages as received from the client",
            "setting": "",
            "setting_value": None,
            "messages_before": None,
            "messages_after": original,
            "metadata": {},
        })

    if mod_msgs:
        stages.append({
            "name": "modified",
            "label": "Modified Messages",
            "item_id": None,
            "item_name": None,
            "detail": "Messages after pipeline processing",
            "setting": "",
            "setting_value": None,
            "messages_before": original,
            "messages_after": modified,
            "metadata": {"budget": data.get("budget", {})} if data.get("budget") else {},
        })

    return {
        "stages": stages,
        "original_messages": original,
        "tags": data.get("tags", []),
    }
