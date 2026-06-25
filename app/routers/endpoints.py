import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.endpoint import Endpoint
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


class EndpointCreate(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    api_base_path: str = ""
    provider: str = ""
    bypass_method: str = "none"
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/") if v else v


class EndpointUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_base_path: str | None = None
    provider: str | None = None
    bypass_method: str | None = None
    enabled: bool | None = None

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v


class EndpointResponse(BaseModel):
    id: str
    name: str
    base_url: str
    api_key: str
    api_base_path: str
    provider: str
    bypass_method: str
    enabled: bool


class EndpointListResponse(BaseModel):
    endpoints: list[EndpointResponse]


@router.get("")
async def list_endpoints(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint).where(Endpoint.user_id == current_user.id).order_by(Endpoint.created_at)
    )
    endpoints = result.scalars().all()
    return EndpointListResponse(
        endpoints=[
            EndpointResponse(
                id=e.id, name=e.name, base_url=e.base_url, api_key=e.api_key,
                api_base_path=e.api_base_path, provider=e.provider, bypass_method=e.bypass_method, enabled=e.enabled,
            )
            for e in endpoints
        ]
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    req: EndpointCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    endpoint = Endpoint(
        user_id=current_user.id,
        name=req.name,
        base_url=req.base_url,
        api_key=req.api_key,
        api_base_path=req.api_base_path,
        provider=req.provider,
        bypass_method=req.bypass_method,
        enabled=req.enabled,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    logger.info("Endpoint created: %s for user: %s", endpoint.name, current_user.username)
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        api_base_path=endpoint.api_base_path,
        provider=endpoint.provider,
        bypass_method=endpoint.bypass_method,
        enabled=endpoint.enabled,
    )


@router.put("/{endpoint_id}")
async def update_endpoint(
    endpoint_id: str,
    req: EndpointUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.user_id == current_user.id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    if req.name is not None:
        endpoint.name = req.name
    if req.base_url is not None:
        endpoint.base_url = req.base_url
    if req.api_key is not None:
        endpoint.api_key = req.api_key
    if req.api_base_path is not None:
        endpoint.api_base_path = req.api_base_path
    if req.provider is not None:
        endpoint.provider = req.provider
    if req.bypass_method is not None:
        endpoint.bypass_method = req.bypass_method
    if req.enabled is not None:
        endpoint.enabled = req.enabled

    await db.commit()
    await db.refresh(endpoint)
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        api_base_path=endpoint.api_base_path,
        provider=endpoint.provider,
        bypass_method=endpoint.bypass_method,
        enabled=endpoint.enabled,
    )


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_endpoint(
    endpoint_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.user_id == current_user.id)
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    await db.delete(endpoint)
    await db.commit()
