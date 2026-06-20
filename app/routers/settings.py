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


class SettingsUpdate(BaseModel):
    default_endpoint_id: str | None = None
    default_model: str | None = None


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

    await db.commit()
    await db.refresh(user_settings)

    return SettingsResponse(
        default_endpoint_id=user_settings.default_endpoint_id,
        default_model=user_settings.default_model,
    )
