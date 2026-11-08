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

from app.database import async_session
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
    user_id: str,
    scan: ScanResult,
    target_type: str,
    target_id: str = "",
    ip_address: str = "",
) -> None:
    """Log safety-scanner findings (critical or warning) to the audit trail.
    Does not block — callers decide whether a critical finding should be
    surfaced to the user for override, matching the existing packs.py pattern.

    The row is written and committed on its own session, not the caller's: the
    scan-then-gate flow logs first and raises the 422 second, and a row that
    merely flushed would be rolled back with the refused request — leaving the
    one case worth a record (a blocked write) with none. A committed log also
    survives any later failure of the write it accompanied, which is what an
    audit trail is for.
    """
    if not scan.findings:
        return
    details = scan.summary + ": " + "; ".join(f.description for f in scan.findings)
    async with async_session() as session:
        await log_action(
            session, user_id, "content_safety_scan", target_type, target_id, details, ip_address,
        )
        await session.commit()


def gate_scan_findings(scan: ScanResult, override: bool, resource_label: str) -> None:
    """Raise HTTP 422 when the scan found critical patterns and the request did
    not explicitly override them.

    Callers log the findings first (log_scan_findings), then gate. The response
    detail carries the critical findings so a client can show what was found
    and re-submit with the override set; a client that cannot ask (the proxy
    path, an import script) simply sees the 422.
    """
    if scan.safe or override:
        return
    raise HTTPException(
        status_code=422,
        detail={
            "message": f"{resource_label} failed the safety scan",
            "scan_findings": [
                {"severity": f.severity, "description": f.description, "line": f.line}
                for f in scan.findings
                if f.severity == "critical"
            ],
        },
    )
