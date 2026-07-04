from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import delete, select

from app.database import async_session
from app.models.debug_exchange import DebugExchange
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

MAX_DEBUG_EXCHANGES = 20


async def is_debug_mode(user_id: str) -> bool:
    """Check if debug mode is enabled for a user."""
    async with async_session() as db:
        result = await db.execute(
            select(UserSettings.debug_mode).where(UserSettings.user_id == user_id)
        )
        row = result.scalar_one_or_none()
        return bool(row)


def init_debug(body_json: dict[str, Any], tags: list) -> None:
    """Initialize the debug container on body_json.

    Stores original message snapshot and extracted tags.
    """
    body_json["_gitv_debug"] = {
        "stages": [],
        "original_messages": json.dumps(body_json.get("messages", []), default=str),
        "tags": [t.get("raw", "") for t in tags] if tags else [],
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


async def capture_exchange(
    user_id: str,
    chat_id: str,
    model: str,
    pipeline_data: dict[str, Any],
    response_content: str,
    verification_data: dict[str, Any] | None = None,
) -> None:
    """Store a debug exchange with full pipeline visibility.

    Keeps only the last MAX_DEBUG_EXCHANGES per user.
    """
    async with async_session() as db:
        exchange = DebugExchange(
            user_id=user_id,
            chat_id=chat_id,
            model=model,
            pipeline_data=json.dumps(pipeline_data, default=str),
            response_content=response_content[:10000],
            verification_data=json.dumps(verification_data or {}, default=str),
        )
        db.add(exchange)
        await db.flush()

        old_result = await db.execute(
            select(DebugExchange.id)
            .where(DebugExchange.user_id == user_id)
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


async def list_exchanges(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """List debug exchanges for a user, newest first."""
    async with async_session() as db:
        result = await db.execute(
            select(DebugExchange)
            .where(DebugExchange.user_id == user_id)
            .order_by(DebugExchange.created_at.desc())
            .limit(limit)
        )
        exchanges = result.scalars().all()
        return [_serialize_exchange(e) for e in exchanges]


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


async def clear_exchanges(user_id: str) -> int:
    """Delete all debug exchanges for a user. Returns count deleted."""
    async with async_session() as db:
        result = await db.execute(
            delete(DebugExchange).where(DebugExchange.user_id == user_id)
        )
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

    return {
        "id": e.id,
        "chat_id": e.chat_id,
        "model": e.model,
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
