"""Shared helpers for content write endpoints: size limits, input sanitization,
and safety-scanner findings, each logged via the audit trail. Kept as small,
explicit functions rather than a single "guard everything" wrapper — different
resource types (cantrip code vs. lorebook entries vs. plain text rules) need
different combinations of these checks, called out individually at each call site
in the routers so it's clear what's actually being checked for each field.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import sanitization
from app.services.admin import get_url_blocklist
from app.services.audit import log_action
from app.services.safety_scanner import ScanResult


def check_size(text: str, max_chars: int, resource_label: str) -> None:
    """Raise HTTP 413 if text exceeds the configured limit."""
    if len(text) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"{resource_label} exceeds size limit ({len(text)} chars, max {max_chars})",
        )


async def sanitize_and_log(
    db: AsyncSession,
    user_id: str,
    text: str,
    target_type: str,
    target_id: str = "",
    ip_address: str = "",
) -> str:
    """Strip control characters and flag zero-width/URL/injection findings to the
    audit log. Returns the (possibly control-char-stripped) text — see
    app.services.sanitization.sanitize_input for what is and isn't altered.
    """
    blocklist = await get_url_blocklist()
    result = sanitization.sanitize_input(text, blocklist)
    if result.flagged:
        details = "; ".join(f"{f.kind}: {f.detail}" for f in result.findings)
        await log_action(
            db, user_id, "content_sanitize_flag", target_type, target_id, details, ip_address,
        )
    return result.text


async def log_scan_findings(
    db: AsyncSession,
    user_id: str,
    scan: ScanResult,
    target_type: str,
    target_id: str = "",
    ip_address: str = "",
) -> None:
    """Log safety-scanner findings (critical or warning) to the audit trail.
    Does not block — callers decide whether a critical finding should be
    surfaced to the user for override, matching the existing packs.py pattern.
    """
    if not scan.findings:
        return
    details = scan.summary + ": " + "; ".join(f.description for f in scan.findings)
    await log_action(
        db, user_id, "content_safety_scan", target_type, target_id, details, ip_address,
    )
