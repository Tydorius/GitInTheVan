import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.api_key import ApiKey
from app.models.user import User
from app.services.auth import generate_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    label: str = ""
    endpoint_id: str | None = None


class ApiKeyResponse(BaseModel):
    id: str
    label: str
    endpoint_id: str | None
    is_active: bool
    created_at: str
    last_used_at: str | None


class ApiKeyCreateResponse(BaseModel):
    id: str
    label: str
    endpoint_id: str | None
    is_active: bool
    api_key: str
    created_at: str


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyResponse]


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == current_user.id).order_by(ApiKey.created_at)
    )
    keys = result.scalars().all()
    return ApiKeyListResponse(
        keys=[
            ApiKeyResponse(
                id=k.id,
                label=k.label,
                endpoint_id=k.endpoint_id,
                is_active=k.is_active,
                created_at=k.created_at.isoformat() if k.created_at else "",
                last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
            )
            for k in keys
        ]
    )


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    req: ApiKeyCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw_key, key_hash = generate_api_key()
    api_key = ApiKey(
        user_id=current_user.id,
        key_hash=key_hash,
        label=req.label or "default",
        endpoint_id=req.endpoint_id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return ApiKeyCreateResponse(
        id=api_key.id,
        label=api_key.label,
        endpoint_id=api_key.endpoint_id,
        is_active=api_key.is_active,
        api_key=raw_key,
        created_at=api_key.created_at.isoformat() if api_key.created_at else "",
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(
    key_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == current_user.id)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    await db.delete(api_key)
    await db.commit()


@router.put("/{key_id}/toggle", response_model=ApiKeyResponse)
async def toggle_api_key(
    key_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == current_user.id)
    )
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    api_key.is_active = not api_key.is_active
    await db.commit()
    await db.refresh(api_key)
    return ApiKeyResponse(
        id=api_key.id,
        label=api_key.label,
        endpoint_id=api_key.endpoint_id,
        is_active=api_key.is_active,
        created_at=api_key.created_at.isoformat() if api_key.created_at else "",
        last_used_at=api_key.last_used_at.isoformat() if api_key.last_used_at else None,
    )
