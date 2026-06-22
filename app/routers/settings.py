import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    default_endpoint_id: str | None
    default_model: str
    preserve_thinking: bool
    gitv_status: bool
    simulated_streaming_speed: int
    driver_callable_turns: int
    bypass_method: str
    prefill_enabled: bool
    context_budget_percent: float
    context_window_override: int
    debug_mode: bool


class SettingsUpdate(BaseModel):
    default_endpoint_id: str | None = None
    default_model: str | None = None
    preserve_thinking: bool | None = None
    gitv_status: bool | None = None
    simulated_streaming_speed: int | None = None
    driver_callable_turns: int | None = None
    bypass_method: str | None = None
    prefill_enabled: bool | None = None
    context_budget_percent: float | None = None
    context_window_override: int | None = None
    debug_mode: bool | None = None


@router.get("")
async def get_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id)
        db.add(user_settings)
        await db.commit()
        await db.refresh(user_settings)

    return SettingsResponse(
        default_endpoint_id=user_settings.default_endpoint_id,
        default_model=user_settings.default_model,
        preserve_thinking=user_settings.preserve_thinking,
        gitv_status=user_settings.gitv_status,
        simulated_streaming_speed=user_settings.simulated_streaming_speed,
        driver_callable_turns=user_settings.driver_callable_turns,
        bypass_method=user_settings.bypass_method,
        prefill_enabled=user_settings.prefill_enabled,
        context_budget_percent=user_settings.context_budget_percent,
        context_window_override=user_settings.context_window_override,
        debug_mode=user_settings.debug_mode,
    )


@router.put("")
async def update_settings(
    req: SettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id)
        db.add(user_settings)

    if req.default_endpoint_id is not None:
        user_settings.default_endpoint_id = req.default_endpoint_id or None
    if req.default_model is not None:
        user_settings.default_model = req.default_model
    if req.preserve_thinking is not None:
        user_settings.preserve_thinking = req.preserve_thinking
    if req.gitv_status is not None:
        user_settings.gitv_status = req.gitv_status
    if req.simulated_streaming_speed is not None:
        user_settings.simulated_streaming_speed = req.simulated_streaming_speed
    if req.driver_callable_turns is not None:
        user_settings.driver_callable_turns = max(0, req.driver_callable_turns)
    if req.bypass_method is not None:
        user_settings.bypass_method = req.bypass_method
    if req.prefill_enabled is not None:
        user_settings.prefill_enabled = req.prefill_enabled
    if req.context_budget_percent is not None:
        user_settings.context_budget_percent = max(0.0, min(100.0, req.context_budget_percent))
    if req.context_window_override is not None:
        user_settings.context_window_override = max(0, req.context_window_override)
    if req.debug_mode is not None:
        user_settings.debug_mode = req.debug_mode

    await db.commit()
    await db.refresh(user_settings)

    return SettingsResponse(
        default_endpoint_id=user_settings.default_endpoint_id,
        default_model=user_settings.default_model,
        preserve_thinking=user_settings.preserve_thinking,
        gitv_status=user_settings.gitv_status,
        simulated_streaming_speed=user_settings.simulated_streaming_speed,
        driver_callable_turns=user_settings.driver_callable_turns,
        bypass_method=user_settings.bypass_method,
        prefill_enabled=user_settings.prefill_enabled,
        context_budget_percent=user_settings.context_budget_percent,
        context_window_override=user_settings.context_window_override,
        debug_mode=user_settings.debug_mode,
    )
