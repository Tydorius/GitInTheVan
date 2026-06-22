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

        await db.execute(
            select(DebugExchange.id, DebugExchange.created_at)
            .where(DebugExchange.user_id == user_id)
            .order_by(DebugExchange.created_at.desc())
            .offset(MAX_DEBUG_EXCHANGES)
        )
        old_ids = [row[0] for row in result.fetchall()] if (result := await db.execute(
            select(DebugExchange.id)
            .where(DebugExchange.user_id == user_id)
            .order_by(DebugExchange.created_at.desc())
            .offset(MAX_DEBUG_EXCHANGES)
        )) else []

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
        return [
            {
                "id": e.id,
                "chat_id": e.chat_id,
                "model": e.model,
                "pipeline_data": json.loads(e.pipeline_data) if e.pipeline_data else {},
                "response_content": e.response_content,
                "verification_data": json.loads(e.verification_data) if e.verification_data else {},
                "created_at": e.created_at.isoformat() if e.created_at else "",
            }
            for e in exchanges
        ]


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
        return {
            "id": e.id,
            "chat_id": e.chat_id,
            "model": e.model,
            "pipeline_data": json.loads(e.pipeline_data) if e.pipeline_data else {},
            "response_content": e.response_content,
            "verification_data": json.loads(e.verification_data) if e.verification_data else {},
            "created_at": e.created_at.isoformat() if e.created_at else "",
        }


async def clear_exchanges(user_id: str) -> int:
    """Delete all debug exchanges for a user. Returns count deleted."""
    async with async_session() as db:
        result = await db.execute(
            delete(DebugExchange).where(DebugExchange.user_id == user_id)
        )
        await db.commit()
        return result.rowcount or 0
