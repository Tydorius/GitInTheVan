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
    default_model: str = ""
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
    default_model: str | None = None
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
    default_model: str
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
                api_base_path=e.api_base_path, provider=e.provider,
                default_model=e.default_model, bypass_method=e.bypass_method, enabled=e.enabled,
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
        default_model=req.default_model,
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
        default_model=endpoint.default_model,
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
    if req.default_model is not None:
        endpoint.default_model = req.default_model
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
        default_model=endpoint.default_model,
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


class ModelListResponse(BaseModel):
    models: list[str]


@router.get("/{endpoint_id}/models", response_model=ModelListResponse)
async def list_endpoint_models(
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

    models: list[str] = []

    if endpoint.provider:
        try:
            import httpx

            from app.services.proxy import _do_forward_litellm
            test_body = b'{"model": "test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}'
            timeout = httpx.Timeout(15.0, connect=10.0)
            _, status_code = await _do_forward_litellm(
                test_body, endpoint.provider, endpoint.base_url, endpoint.api_key, timeout,
            )
            if status_code in (200, 400, 404):
                models = await _list_models_litellm(endpoint.provider, endpoint.api_key, endpoint.base_url)
        except Exception as exc:
            logger.warning("Model list via litellm failed: %s", exc)
    else:
        try:
            import httpx

            api_base = endpoint.api_base_path or "/v1"
            models_url = f"{endpoint.base_url}{api_base}/models"
            headers = {"Authorization": f"Bearer {endpoint.api_key}"}
            timeout = httpx.Timeout(15.0, connect=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(models_url, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                raw_models = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(raw_models, list):
                    for m in raw_models:
                        if isinstance(m, dict):
                            mid = m.get("id", "")
                            if mid:
                                models.append(mid)
                        elif isinstance(m, str):
                            models.append(m)
        except Exception as exc:
            logger.warning("Model list failed: %s", exc)

    if not models and endpoint.default_model:
        models = [endpoint.default_model]

    return ModelListResponse(models=sorted(models))


async def _list_models_litellm(provider: str, api_key: str, base_url: str) -> list[str]:
    """Fetch available models using litellm for provider-based endpoints."""
    try:
        import litellm
    except ImportError:
        return []

    try:
        api_base = base_url or None
        response = await litellm.alist_models(
            model=f"{provider}/",
            api_key=api_key,
            api_base=api_base,
        )
        if isinstance(response, list):
            return [str(m) for m in response]
        if hasattr(response, "data"):
            return [str(m.id) if hasattr(m, "id") else str(m) for m in response.data]
    except Exception as exc:
        logger.debug("litellm alist_models failed: %s", exc)

    return []
