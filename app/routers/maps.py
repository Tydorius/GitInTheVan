import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.map import Map, MapStage, MapStageResource
from app.models.user import User
from app.services.llm_params import ParameterDef, params_from_api, params_to_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maps", tags=["maps"])


class ResourceInput(BaseModel):
    resource_type: str
    resource_id: str
    position: str = "pre_driver"
    sticky: bool = False

    from pydantic import field_validator

    @field_validator("resource_type")
    @classmethod
    def validate_resource_type(cls, v: str) -> str:
        allowed = {"lorebook", "cantrip", "skill", "sample"}
        if v not in allowed:
            raise ValueError(f"resource_type must be one of {allowed}")
        return v


class StageInput(BaseModel):
    name: str
    description: str = ""
    system_instructions: str = ""
    endpoint_id: str | None = None
    endpoint_tag: str = ""
    model_override: str = ""
    driver_callable_turns: int = 0
    verification_enabled: bool = False
    verification_endpoint_id: str | None = None
    verification_model: str = ""
    verification_max_retries: int = 2
    verification_instructions: str = ""
    output_mode: str = "persist"
    parameters: list[ParameterDef] = []
    verification_parameters: list[ParameterDef] = []
    resources: list[ResourceInput] = []


class MapCreate(BaseModel):
    name: str
    description: str = ""
    tag: str = ""
    is_public: bool = False
    is_active: bool = True
    version: str = "1.0"
    author: str = ""
    global_llm_instructions: str = ""
    stages: list[StageInput] = []


class MapUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    tag: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    version: str | None = None
    author: str | None = None
    global_llm_instructions: str | None = None
    stages: list[StageInput] | None = None


class ResourceResponse(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    position: str
    sticky: bool


class StageResponse(BaseModel):
    id: str
    stage_order: int
    name: str
    description: str
    system_instructions: str
    endpoint_id: str | None
    endpoint_tag: str
    model_override: str
    driver_callable_turns: int
    verification_enabled: bool
    verification_endpoint_id: str | None
    verification_model: str
    verification_max_retries: int
    verification_instructions: str
    output_mode: str
    parameters: list[ParameterDef]
    verification_parameters: list[ParameterDef]
    resources: list[ResourceResponse]


class MapResponse(BaseModel):
    id: str
    name: str
    description: str
    tag: str
    is_public: bool
    is_active: bool
    version: str
    author: str
    global_llm_instructions: str
    stages: list[StageResponse]
    # Populated by /import only: resources that could not be resolved, and the
    # repos that would resolve them. Empty everywhere else.
    import_warnings: list[str] = []
    requires_repos: list[str] = []


class MapListItem(BaseModel):
    id: str
    name: str
    description: str
    tag: str
    is_public: bool
    is_active: bool
    stage_count: int


class MapListResponse(BaseModel):
    maps: list[MapListItem]


def _resource_to_response(r: MapStageResource) -> ResourceResponse:
    return ResourceResponse(
        id=r.id, resource_type=r.resource_type, resource_id=r.resource_id,
        position=r.position, sticky=r.sticky,
    )


def _stage_to_response(s: MapStage) -> StageResponse:
    return StageResponse(
        id=s.id, stage_order=s.stage_order, name=s.name, description=s.description,
        system_instructions=s.system_instructions, endpoint_id=s.endpoint_id,
        endpoint_tag=s.endpoint_tag,
        model_override=s.model_override, driver_callable_turns=s.driver_callable_turns,
        verification_enabled=s.verification_enabled,
        verification_endpoint_id=s.verification_endpoint_id,
        verification_model=s.verification_model,
        verification_max_retries=s.verification_max_retries,
        verification_instructions=s.verification_instructions,
        output_mode=s.output_mode,
        parameters=params_to_api(s.parameters_json),
        verification_parameters=params_to_api(s.verification_parameters_json),
        resources=[_resource_to_response(r) for r in s.resources],
    )


def _map_to_response(m: Map) -> MapResponse:
    return MapResponse(
        id=m.id, name=m.name, description=m.description, tag=m.tag,
        is_public=m.is_public, is_active=m.is_active, version=m.version,
        author=m.author, global_llm_instructions=m.global_llm_instructions,
        stages=[_stage_to_response(s) for s in m.stages],
    )


async def _check_tag_unique(db: AsyncSession, tag: str, user_id: str, exclude_id: str | None = None) -> None:
    if not tag:
        return
    query = text("SELECT id FROM maps WHERE tag = :tag AND user_id = :uid")
    params: dict[str, Any] = {"tag": tag, "uid": user_id}
    if exclude_id:
        query = text("SELECT id FROM maps WHERE tag = :tag AND user_id = :uid AND id != :mid")
        params["mid"] = exclude_id
    result = await db.execute(query, params)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already in use")


def _build_stage(stage_input: StageInput, stage_order: int, map_id: str) -> MapStage:
    stage = MapStage(
        map_id=map_id,
        stage_order=stage_order,
        name=stage_input.name,
        description=stage_input.description,
        system_instructions=stage_input.system_instructions,
        endpoint_id=stage_input.endpoint_id,
        endpoint_tag=stage_input.endpoint_tag,
        model_override=stage_input.model_override,
        driver_callable_turns=stage_input.driver_callable_turns,
        verification_enabled=stage_input.verification_enabled,
        verification_endpoint_id=stage_input.verification_endpoint_id,
        verification_model=stage_input.verification_model,
        verification_max_retries=stage_input.verification_max_retries,
        verification_instructions=stage_input.verification_instructions,
        output_mode=stage_input.output_mode,
        parameters_json=params_from_api(
            stage_input.parameters, f"stage '{stage_input.name}' parameters"
        ),
        verification_parameters_json=params_from_api(
            stage_input.verification_parameters,
            f"stage '{stage_input.name}' verification parameters",
        ),
    )
    for r in stage_input.resources:
        stage.resources.append(MapStageResource(
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            position=r.position,
            sticky=r.sticky,
        ))
    return stage


@router.get("", response_model=MapListResponse)
async def list_maps(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Map)
        .where(Map.user_id == current_user.id)
        .options(selectinload(Map.stages))
        .order_by(Map.created_at)
    )
    maps = result.scalars().all()
    return MapListResponse(
        maps=[
            MapListItem(
                id=m.id, name=m.name, description=m.description, tag=m.tag,
                is_public=m.is_public, is_active=m.is_active, stage_count=len(m.stages),
            )
            for m in maps
        ]
    )


@router.get("/{map_id}", response_model=MapResponse)
async def get_map(
    map_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Map)
        .where(Map.id == map_id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if m.user_id != current_user.id and not m.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return _map_to_response(m)


@router.post("", response_model=MapResponse, status_code=status.HTTP_201_CREATED)
async def create_map(
    req: MapCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _check_tag_unique(db, req.tag, current_user.id)
    m = Map(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        tag=req.tag,
        is_public=req.is_public,
        is_active=req.is_active,
        version=req.version,
        author=req.author,
        global_llm_instructions=req.global_llm_instructions,
    )
    db.add(m)
    await db.flush()

    for i, stage_input in enumerate(req.stages):
        stage = _build_stage(stage_input, i + 1, m.id)
        db.add(stage)

    await db.commit()

    result = await db.execute(
        select(Map)
        .where(Map.id == m.id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    return _map_to_response(result.scalar_one())


@router.put("/{map_id}", response_model=MapResponse)
async def update_map(
    map_id: str,
    req: MapUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Map)
        .where(Map.id == map_id, Map.user_id == current_user.id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")

    if req.tag is not None and req.tag != m.tag:
        await _check_tag_unique(db, req.tag, current_user.id, map_id)
        m.tag = req.tag
    if req.name is not None:
        m.name = req.name
    if req.description is not None:
        m.description = req.description
    if req.is_public is not None:
        m.is_public = req.is_public
    if req.is_active is not None:
        m.is_active = req.is_active
    if req.version is not None:
        m.version = req.version
    if req.author is not None:
        m.author = req.author
    if req.global_llm_instructions is not None:
        m.global_llm_instructions = req.global_llm_instructions

    if req.stages is not None:
        for stage in m.stages:
            await db.delete(stage)
        await db.flush()

        for i, stage_input in enumerate(req.stages):
            stage = _build_stage(stage_input, i + 1, m.id)
            db.add(stage)

    await db.commit()

    result = await db.execute(
        select(Map)
        .where(Map.id == m.id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    return _map_to_response(result.scalar_one())


@router.delete("/{map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_map(
    map_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Map).where(Map.id == map_id, Map.user_id == current_user.id)
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    await db.delete(m)
    await db.commit()


@router.get("/{map_id}/export")
async def export_map(
    map_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    mode: str = "embedded",
):
    """Export a map as self-contained JSON.

    mode=embedded (default) writes the full content of every attached resource,
    so the file installs anywhere. mode=linked writes only a source reference
    for resources whose origin is known, so the map tracks upstream and the
    author maintains each resource in one place.
    """
    result = await db.execute(
        select(Map)
        .where(Map.id == map_id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    m = result.scalar_one_or_none()
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
    if m.user_id != current_user.id and not m.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    from app.services.map_transfer import serialize_map_to_export

    return await serialize_map_to_export(db, m, mode=mode)


class MapImportRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    data: dict[str, Any]
    resource_mode: str = "smart"  # smart | keep_both | reuse | overwrite


@router.post("/import", response_model=MapResponse, status_code=status.HTTP_201_CREATED)
async def import_map(
    req: MapImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Import a map from exported JSON.

    resource_mode controls how a resource that collides with something you
    already have is handled:
    - smart (default): reuse by origin, then by content hash, else create a copy
    - keep_both: always create new copies
    - reuse: link to an existing same-named resource
    - overwrite: update an existing same-named resource

    Linked resources (a source with no embedded content) are resolved from a
    repo you have already linked. allow_fetch is off here: a hand-uploaded JSON
    file never causes a clone.
    """
    from app.services.map_transfer import build_map_from_export

    outcome = await build_map_from_export(
        db, current_user.id, req.data,
        name=req.name, description=req.description, resource_mode=req.resource_mode,
        allow_fetch=False,
    )
    m = outcome.map_obj

    await db.commit()

    result = await db.execute(
        select(Map)
        .where(Map.id == m.id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    response = _map_to_response(result.scalar_one())
    response.import_warnings = outcome.warnings
    response.requires_repos = outcome.requires_repos
    return response
