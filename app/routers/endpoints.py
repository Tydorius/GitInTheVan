import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.endpoint import Endpoint, EndpointModel
from app.models.user import User
from app.services.llm_params import ParameterDef, params_from_api, params_to_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


class EndpointModelInput(BaseModel):
    name: str
    description: str = ""
    parameters: list[ParameterDef] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Model name is required")
        if len(v) > 128:
            raise ValueError("Model name exceeds 128 characters")
        return v

    @field_validator("description")
    @classmethod
    def check_description(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > 512:
            raise ValueError("Model description exceeds 512 characters")
        return v


class EndpointModelResponse(BaseModel):
    id: str
    name: str
    description: str
    parameters: list[ParameterDef]


class EndpointCreate(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    api_base_path: str = ""
    provider: str = ""
    default_model: str = ""
    bypass_method: str = "none"
    enabled: bool = True
    role_tag: str = "default"
    priority: int = 1
    custom_tag: str = ""
    parameters: list[ParameterDef] = Field(default_factory=list)
    models: list[EndpointModelInput] = Field(default_factory=list)

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
    role_tag: str | None = None
    priority: int | None = None
    custom_tag: str | None = None
    parameters: list[ParameterDef] | None = None
    models: list[EndpointModelInput] | None = None

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
    role_tag: str
    priority: int
    custom_tag: str
    parameters: list[ParameterDef]
    models: list[EndpointModelResponse]


class EndpointListResponse(BaseModel):
    endpoints: list[EndpointResponse]


def _endpoint_to_response(e: Endpoint) -> EndpointResponse:
    return EndpointResponse(
        id=e.id,
        name=e.name,
        base_url=e.base_url,
        api_key=e.api_key,
        api_base_path=e.api_base_path,
        provider=e.provider,
        default_model=e.default_model,
        bypass_method=e.bypass_method,
        enabled=e.enabled,
        role_tag=e.role_tag,
        priority=e.priority,
        custom_tag=e.custom_tag,
        parameters=params_to_api(e.parameters_json),
        models=[
            EndpointModelResponse(
                id=m.id,
                name=m.name,
                description=m.description,
                parameters=params_to_api(m.parameters_json),
            )
            for m in e.models
        ],
    )


def _build_models(endpoint_id: str, models: list[EndpointModelInput]) -> list[EndpointModel]:
    """Build the rows for an endpoint's model list.

    Models are edited as part of the endpoint form, so they arrive as a complete
    list and are written wholesale rather than through their own CRUD routes --
    the same shape maps use for stages. Rows are built and added directly rather
    than through the relationship, because assigning to a collection that has
    not been loaded triggers IO in a context that cannot perform it.
    """
    seen: set[str] = set()
    rows: list[EndpointModel] = []
    for i, m in enumerate(models):
        if m.name in seen:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Duplicate model name '{m.name}' on this endpoint",
            )
        seen.add(m.name)
        rows.append(
            EndpointModel(
                endpoint_id=endpoint_id,
                name=m.name,
                description=m.description,
                sort_order=i,
                parameters_json=params_from_api(m.parameters, f"model '{m.name}' parameters"),
            )
        )
    return rows


@router.get("")
async def list_endpoints(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.user_id == current_user.id)
        .options(selectinload(Endpoint.models))
        .order_by(Endpoint.created_at)
    )
    endpoints = result.scalars().all()
    return EndpointListResponse(endpoints=[_endpoint_to_response(e) for e in endpoints])


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
        role_tag=req.role_tag,
        priority=req.priority,
        custom_tag=req.custom_tag,
        parameters_json=params_from_api(req.parameters, "endpoint parameters"),
    )
    db.add(endpoint)
    await db.flush()
    for row in _build_models(endpoint.id, req.models):
        db.add(row)
    await db.commit()

    result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint.id).options(selectinload(Endpoint.models))
    )
    logger.info("Endpoint created: %s for user: %s", endpoint.name, current_user.username)
    return _endpoint_to_response(result.scalar_one())


@router.put("/{endpoint_id}")
async def update_endpoint(
    endpoint_id: str,
    req: EndpointUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.id == endpoint_id, Endpoint.user_id == current_user.id)
        .options(selectinload(Endpoint.models))
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
    if req.role_tag is not None:
        endpoint.role_tag = req.role_tag
    if req.priority is not None:
        endpoint.priority = req.priority
    if req.custom_tag is not None:
        endpoint.custom_tag = req.custom_tag
    if req.parameters is not None:
        endpoint.parameters_json = params_from_api(req.parameters, "endpoint parameters")
    if req.models is not None:
        # Assign through the relationship rather than deleting the rows
        # individually: the collection is eagerly loaded here, and a row that is
        # still a member of a loaded collection gets re-persisted by the unit of
        # work even after db.delete(). delete-orphan removes the dropped ones.
        endpoint.models = _build_models(endpoint.id, req.models)

    await db.commit()

    result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint.id).options(selectinload(Endpoint.models))
    )
    return _endpoint_to_response(result.scalar_one())


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
    """Live-probe the provider for available models.

    This stays a probe. It is what the Endpoints UI offers as candidates when
    the user builds their curated list; it is not the curated list itself, which
    lives in `endpoint_models`.
    """
    result = await db.execute(
        select(Endpoint)
        .where(Endpoint.id == endpoint_id, Endpoint.user_id == current_user.id)
        .options(selectinload(Endpoint.models))
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

    # Fall back to what the user has already named rather than returning nothing
    # when the probe fails -- an endpoint behind a firewall still has a usable
    # model list.
    if not models:
        models = [m.name for m in endpoint.models]
    if not models and endpoint.default_model:
        models = [endpoint.default_model]

    return ModelListResponse(models=sorted(set(models)))


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
