"""Guarded lorebook-entry writes shared by the lorebook router and the
lexicon compiler (Phase 29a).

Rule 18: every create/update of user-authored text runs through these guards
no matter which path writes it. The router treats a refusal as an HTTP error
(413 for size, 422 for critical scan findings, both carrying the reason); the
lexicon pipeline catches the same exceptions and degrades to "forward
untouched, write nothing" -- a mid-request refusal must never fail the reply.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lorebook_entry import LorebookEntry
from app.services.admin import get_admin_settings
from app.services.content_guard import log_scan_findings, sanitize_and_log
from app.services.safety_scanner import ScanResult, scan_lorebook

logger = logging.getLogger(__name__)


async def enforce_lorebook_size(
    db: AsyncSession, lorebook_id: str, added_chars: int, exclude_entry_id: str | None = None
) -> None:
    """Enforce max_lorebook_size_kb against the total content across all entries
    in a lorebook (matches the original Phase 19b design: "total lorebook entry
    content per lorebook", not a per-entry cap). Raises HTTP 413 on breach.

    Reads admin caps on its own session, so callers must not hold flushed,
    uncommitted state (the snapshots.capture invariant).
    """
    admin_settings = await get_admin_settings()
    max_bytes = admin_settings.max_lorebook_size_kb * 1024

    q = select(LorebookEntry.content).where(LorebookEntry.lorebook_id == lorebook_id)
    if exclude_entry_id:
        q = q.where(LorebookEntry.id != exclude_entry_id)
    result = await db.execute(q)
    existing_total = sum(len(c or "") for c in result.scalars().all())

    total = existing_total + added_chars
    if total > max_bytes:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=413,
            detail=f"Lorebook content exceeds size limit ({total} chars, max {max_bytes})",
        )


async def sanitize_entry_content(
    db: AsyncSession, user_id: str, lorebook_id: str, content: str
) -> str:
    """Control-char stripping + zero-width/URL/injection flagging for entry text."""
    return await sanitize_and_log(db, user_id, content, "lorebook_entry", lorebook_id)


async def scan_entry(
    user_id: str, name: str, content: str, lorebook_id: str
) -> ScanResult:
    """Deterministic safety scan with findings logged to the audit trail.

    Does not block -- the caller gates (the router via gate_scan_findings so a
    user can override; the lexicon pipeline refuses outright, since nothing
    interactive can happen mid-request).
    """
    scan = scan_lorebook([{"name": name, "content": content}])
    await log_scan_findings(user_id, scan, "lorebook_entry", lorebook_id)
    return scan
