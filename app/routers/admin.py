import logging
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
    max_map_stages: int
    rate_limit_proxy_per_min: int
    rate_limit_api_per_min: int
    runtime_log_level: str
    effective_log_level: str


class AdminSettingsUpdate(BaseModel):
    max_driver_callable_turns: int | None = None
    max_verification_retries: int | None = None
    max_map_stages: int | None = None
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
        max_map_stages=s.max_map_stages,
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
    if req.max_map_stages is not None:
        updates["max_map_stages"] = max(1, req.max_map_stages)
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
        max_map_stages=s.max_map_stages,
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
    """Read recent server log output."""
    from app.services.log_manager import read_recent_logs
    log_lines = read_recent_logs(max(1, min(lines, 1000)))

    return ServerLogResponse(lines=log_lines, total=len(log_lines))


class SSLStatusResponse(BaseModel):
    cert_configured: bool
    cert_exists: bool
    cert_path: str | None
    key_path: str | None
    ca_cert_path: str | None
    cert_info: dict | None
    is_active: bool


class SSLGenerateRequest(BaseModel):
    extra_ips: list[str] | None = None
    extra_dns: list[str] | None = None


@router.get("/ssl/status", response_model=SSLStatusResponse)
async def get_ssl_status(
    admin: Annotated[User, Depends(require_admin)],
):
    from app.services.ssl_manager import get_ssl_status as _get_status
    s = _get_status()
    return SSLStatusResponse(**s)


@router.post("/ssl/generate", response_model=SSLStatusResponse)
async def generate_ssl_cert(
    req: SSLGenerateRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.audit import create_log
    from app.services.ssl_manager import (
        LEAF_CERT_PATH,
        LEAF_KEY_PATH,
        generate_self_signed_cert,
        get_ssl_status,
    )

    generate_self_signed_cert(
        extra_ips=req.extra_ips,
        extra_dns=req.extra_dns,
    )

    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    _update_env_ssl(env_path, str(LEAF_CERT_PATH), str(LEAF_KEY_PATH))

    await create_log(
        db, admin.id, "ssl.generate", "ssl", "ca-and-leaf",
        "Generated local CA and leaf SSL certificate",
    )

    s = get_ssl_status()
    return SSLStatusResponse(**s)


def _update_env_ssl(env_path: Path, cert_val: str, key_val: str):
    """Write SSL cert/key paths into .env file, replacing or appending safely."""
    lines = []
    found_cert = False
    found_key = False

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GITV_SSL_CERTFILE="):
                lines.append(f"GITV_SSL_CERTFILE={cert_val}")
                found_cert = True
            elif line.strip().startswith("GITV_SSL_KEYFILE="):
                lines.append(f"GITV_SSL_KEYFILE={key_val}")
                found_key = True
            else:
                lines.append(line)

    if not found_cert or not found_key:
        if lines and lines[-1].strip():
            lines.append("")

    if not found_cert:
        lines.append(f"GITV_SSL_CERTFILE={cert_val}")
    if not found_key:
        lines.append(f"GITV_SSL_KEYFILE={key_val}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@router.get("/ssl/ca-cert")
async def download_ca_cert(
    admin: Annotated[User, Depends(require_admin)],
):
    from fastapi.responses import Response

    from app.services.ssl_manager import CA_CERT_PATH

    if not CA_CERT_PATH.exists():
        raise HTTPException(status_code=404, detail="CA certificate not found")

    return Response(
        content=CA_CERT_PATH.read_bytes(),
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": "attachment; filename=gitinthevan-ca.pem"},
    )


class UpdateCheckResponse(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str = ""
    release_notes: str = ""
    zip_url: str = ""
    error: str = ""


@router.get("/update/check", response_model=UpdateCheckResponse)
async def check_update(
    admin: Annotated[User, Depends(require_admin)],
):
    from app.services.updater import check_for_update
    result = await check_for_update()
    return UpdateCheckResponse(**result)


class UpdateDownloadResponse(BaseModel):
    zip_url: str
    current_version: str
    latest_version: str
    instructions: str


@router.get("/update/download-info", response_model=UpdateDownloadResponse)
async def get_update_download_info(
    admin: Annotated[User, Depends(require_admin)],
):
    from app.services.updater import check_for_update, get_current_version
    result = await check_for_update()
    current = get_current_version()
    latest = result.get("latest_version", current)
    return UpdateDownloadResponse(
        zip_url=result.get("zip_url", ""),
        current_version=current,
        latest_version=latest,
        instructions=(
            "Download the zip, stop the server, back up your database (data/gitinthevan.db), "
            "extract the zip over your existing installation, then run the deploy script for your platform. "
            "See the update scripts in the scripts/ directory."
        ),
    )
