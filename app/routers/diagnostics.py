import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cantrip import Cantrip
from app.models.endpoint import Endpoint
from app.models.user import User
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


class DiagnosticResult(BaseModel):
    check: str
    passed: bool
    message: str
    detail: str = ""


class AuditResponse(BaseModel):
    results: list[DiagnosticResult]
    all_passed: bool


@router.get("/audit")
async def run_audit(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    endpoint_id: str | None = None,
):
    results: list[DiagnosticResult] = []

    endpoints_result = await db.execute(
        select(Endpoint).where(Endpoint.user_id == current_user.id)
    )
    endpoints = endpoints_result.scalars().all()

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    settings = settings_result.scalar_one_or_none()

    if not endpoints:
        results.append(DiagnosticResult(
            check="Endpoint Configuration",
            passed=False,
            message="No endpoints configured",
            detail="Add at least one endpoint in the Endpoints page.",
        ))
    else:
        results.append(DiagnosticResult(
            check="Endpoint Configuration",
            passed=True,
            message=f"{len(endpoints)} endpoint(s) configured",
        ))

        default_ep = None
        if settings and settings.default_endpoint_id:
            for ep in endpoints:
                if ep.id == settings.default_endpoint_id:
                    default_ep = ep
                    break

        if default_ep:
            results.append(DiagnosticResult(
                check="Default Endpoint",
                passed=True,
                message=f"Default endpoint: {default_ep.name}",
            ))
            results.append(DiagnosticResult(
                check="Endpoint API Key",
                passed=bool(default_ep.api_key),
                message="API key set" if default_ep.api_key else "No API key configured",
                detail="Set an API key in the endpoint configuration." if not default_ep.api_key else "",
            ))
        else:
            results.append(DiagnosticResult(
                check="Default Endpoint",
                passed=False,
                message="No default endpoint set",
                detail="Go to Settings and select a default endpoint.",
            ))

    cantrips_result = await db.execute(
        select(Cantrip).where(Cantrip.user_id == current_user.id, Cantrip.is_active.is_(True))
    )
    active_cantrips = cantrips_result.scalars().all()
    results.append(DiagnosticResult(
        check="Active Cantrips",
        passed=True,
        message=f"{len(active_cantrips)} active cantrip(s)",
    ))

    for ac in active_cantrips:
        code_check = bool(ac.code and ac.code.strip())
        results.append(DiagnosticResult(
            check=f"Cantrip: {ac.name}",
            passed=code_check,
            message="Has code" if code_check else "Empty code",
        ))

    if settings and settings.verification_enabled:
        results.append(DiagnosticResult(
            check="Verification Enabled",
            passed=True,
            message="Verification is active",
        ))
        if settings.verification_endpoint_id:
            v_ep = None
            for ep in endpoints:
                if ep.id == settings.verification_endpoint_id:
                    v_ep = ep
                    break
            results.append(DiagnosticResult(
                check="Verification Endpoint",
                passed=bool(v_ep),
                message=f"Using: {v_ep.name}" if v_ep else "Verification endpoint not found",
            ))
        else:
            results.append(DiagnosticResult(
                check="Verification Endpoint",
                passed=False,
                message="No verification endpoint configured",
                detail="Select a verification endpoint in Verification > Settings.",
            ))
    else:
        results.append(DiagnosticResult(
            check="Verification",
            passed=True,
            message="Not enabled (optional)",
        ))

    import os

    from app.services.deno_runner import DENO_PATH
    results.append(DiagnosticResult(
        check="Deno Runtime",
        passed=bool(DENO_PATH and os.path.exists(DENO_PATH)),
        message=f"Found at {DENO_PATH}" if DENO_PATH else "Not found",
        detail="Cantrips will not work without Deno. Install it or set GITV_DENO_PATH." if not DENO_PATH else "",
    ))

    target_ep = None
    if endpoint_id:
        for ep in endpoints:
            if ep.id == endpoint_id:
                target_ep = ep
                break
    elif default_ep:
        target_ep = default_ep

    if target_ep:
        try:
            import httpx

            api_base_path = target_ep.api_base_path or ""
            if api_base_path.endswith("/chat/completions"):
                test_url = f"{target_ep.base_url}{api_base_path}"
            else:
                path_prefix = api_base_path or "/v1"
                test_url = f"{target_ep.base_url}{path_prefix}/models"

            headers = {"Authorization": f"Bearer {target_ep.api_key}"}
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(test_url, headers=headers)

            if resp.status_code in (200, 401, 403):
                results.append(DiagnosticResult(
                    check="Endpoint Connectivity",
                    passed=resp.status_code == 200,
                    message=f"Endpoint responded with {resp.status_code}",
                    detail=f"Auth error - check API key for '{target_ep.name}'" if resp.status_code in (401, 403) else "",
                ))
            else:
                results.append(DiagnosticResult(
                    check="Endpoint Connectivity",
                    passed=False,
                    message=f"Endpoint returned {resp.status_code}",
                    detail=f"Check the endpoint URL and API base path for '{target_ep.name}'",
                ))
        except Exception as e:
            results.append(DiagnosticResult(
                check="Endpoint Connectivity",
                passed=False,
                message=f"Connection failed: {str(e)[:100]}",
                detail=f"Check that '{target_ep.name}' is running and accessible",
            ))

    all_passed = all(r.passed for r in results)

    return AuditResponse(results=results, all_passed=all_passed)
