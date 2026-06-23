import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.services.admin import (
    apply_runtime_log_level,
    get_admin_settings,
    update_admin_settings,
)
from app.services.audit import list_logs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminSettingsResponse(BaseModel):
    max_driver_callable_turns: int
    max_verification_retries: int
    rate_limit_proxy_per_min: int
    rate_limit_api_per_min: int
    runtime_log_level: str
    effective_log_level: str


class AdminSettingsUpdate(BaseModel):
    max_driver_callable_turns: int | None = None
    max_verification_retries: int | None = None
    rate_limit_proxy_per_min: int | None = None
    rate_limit_api_per_min: int | None = None
    runtime_log_level: str | None = None


class AuditLogItem(BaseModel):
    id: str
    action: str
    target_type: str
    target_id: str
    details: str
    ip_address: str
    created_at: str


class AuditLogListResponse(BaseModel):
    logs: list[AuditLogItem]
    total: int


class ServerLogResponse(BaseModel):
    lines: list[str]
    total: int


@router.get("/settings", response_model=AdminSettingsResponse)
async def get_settings(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.admin import get_effective_log_level
    s = await get_admin_settings()
    effective = await get_effective_log_level()
    return AdminSettingsResponse(
        max_driver_callable_turns=s.max_driver_callable_turns,
        max_verification_retries=s.max_verification_retries,
        rate_limit_proxy_per_min=s.rate_limit_proxy_per_min,
        rate_limit_api_per_min=s.rate_limit_api_per_min,
        runtime_log_level=s.runtime_log_level,
        effective_log_level=effective,
    )


@router.put("/settings", response_model=AdminSettingsResponse)
async def update_settings(
    req: AdminSettingsUpdate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    updates: dict = {}
    if req.max_driver_callable_turns is not None:
        updates["max_driver_callable_turns"] = max(0, req.max_driver_callable_turns)
    if req.max_verification_retries is not None:
        updates["max_verification_retries"] = max(0, req.max_verification_retries)
    if req.rate_limit_proxy_per_min is not None:
        updates["rate_limit_proxy_per_min"] = max(0, req.rate_limit_proxy_per_min)
    if req.rate_limit_api_per_min is not None:
        updates["rate_limit_api_per_min"] = max(0, req.rate_limit_api_per_min)
    if req.runtime_log_level is not None:
        level = req.runtime_log_level.strip().upper()
        if level and level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid log level")
        updates["runtime_log_level"] = level
        if level:
            apply_runtime_log_level(level)

    s = await update_admin_settings(updates)
    from app.services.admin import get_effective_log_level
    effective = await get_effective_log_level()
    return AdminSettingsResponse(
        max_driver_callable_turns=s.max_driver_callable_turns,
        max_verification_retries=s.max_verification_retries,
        rate_limit_proxy_per_min=s.rate_limit_proxy_per_min,
        rate_limit_api_per_min=s.rate_limit_api_per_min,
        runtime_log_level=s.runtime_log_level,
        effective_log_level=effective,
    )


@router.get("/audit", response_model=AuditLogListResponse)
async def get_audit_logs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 100,
    offset: int = 0,
):
    logs = await list_logs(db, admin.id, limit, offset)
    return AuditLogListResponse(
        logs=[AuditLogItem(**log) for log in logs],
        total=len(logs),
    )


@router.get("/logs", response_model=ServerLogResponse)
async def get_server_logs(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    lines: int = 200,
):
    """Read recent server log output. Attempts to find a log file, falls back to empty."""
    target_lines = max(1, min(lines, 1000))
    log_lines: list[str] = []

    log_candidates = [
        Path("data/gitinthevan.log"),
        Path("gitinthevan.log"),
        Path("/var/log/gitinthevan/gitinthevan.log"),
    ]

    env_log = os.environ.get("GITV_LOG_FILE", "")
    if env_log:
        log_candidates.insert(0, Path(env_log))

    for log_path in log_candidates:
        if log_path.exists():
            try:
                all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                log_lines = all_lines[-target_lines:] if len(all_lines) > target_lines else all_lines
                break
            except Exception:
                continue

    if not log_lines:
        from app.services.admin import get_effective_log_level
        effective_level = await get_effective_log_level()
        log_lines = [
            "No log file found. Logs are written to stdout/stderr.",
            f"Current log level: {effective_level}",
            "To capture logs to a file, set GITV_LOG_FILE in your .env or redirect output.",
        ]

    return ServerLogResponse(lines=log_lines, total=len(log_lines))
