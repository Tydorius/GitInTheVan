from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

MAX_LOGS = 1000


async def log_action(
    db: AsyncSession,
    user_id: str,
    action: str,
    target_type: str = "",
    target_id: str = "",
    details: str = "",
    ip_address: str = "",
) -> None:
    """Record an audit log entry."""
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()

    result = await db.execute(
        select(AuditLog.id)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .offset(MAX_LOGS)
    )
    old_ids = [row[0] for row in result.fetchall()]
    if old_ids:
        await db.execute(delete(AuditLog).where(AuditLog.id.in_(old_ids)))


async def list_logs(
    db: AsyncSession, user_id: str, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """List audit logs for a user."""
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()
    return [
        {
            "id": log.id,
            "user_id": log.user_id,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "details": log.details,
            "ip_address": log.ip_address,
            "created_at": log.created_at.isoformat() if log.created_at else "",
        }
        for log in logs
    ]
