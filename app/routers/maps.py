import json
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/maps", tags=["maps"])


class ResourceInput(BaseModel):
    resource_type: str
    resource_id: str
    position: str = "pre_driver"
    sticky: bool = False


class StageInput(BaseModel):
    name: str
    description: str = ""
    system_instructions: str = ""
    endpoint_id: str | None = None
    model_override: str = ""
    driver_callable_turns: int = 0
    verification_enabled: bool = False
    verification_endpoint_id: str | None = None
    verification_model: str = ""
    verification_max_retries: int = 2
    verification_instructions: str = ""
    output_mode: str = "persist"
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
    model_override: str
    driver_callable_turns: int
    verification_enabled: bool
    verification_endpoint_id: str | None
    verification_model: str
    verification_max_retries: int
    verification_instructions: str
    output_mode: str
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
        model_override=s.model_override, driver_callable_turns=s.driver_callable_turns,
        verification_enabled=s.verification_enabled,
        verification_endpoint_id=s.verification_endpoint_id,
        verification_model=s.verification_model,
        verification_max_retries=s.verification_max_retries,
        verification_instructions=s.verification_instructions,
        output_mode=s.output_mode,
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
        model_override=stage_input.model_override,
        driver_callable_turns=stage_input.driver_callable_turns,
        verification_enabled=stage_input.verification_enabled,
        verification_endpoint_id=stage_input.verification_endpoint_id,
        verification_model=stage_input.verification_model,
        verification_max_retries=stage_input.verification_max_retries,
        verification_instructions=stage_input.verification_instructions,
        output_mode=stage_input.output_mode,
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
):
    """Export a map as a self-contained JSON with embedded lorebook and cantrip content."""
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

    export_data: dict[str, Any] = {
        "name": m.name,
        "description": m.description,
        "version": m.version,
        "author": m.author,
        "global_llm_instructions": m.global_llm_instructions,
        "stages": [],
    }

    for stage in sorted(m.stages, key=lambda s: s.stage_order):
        stage_data: dict[str, Any] = {
            "stage_order": stage.stage_order,
            "name": stage.name,
            "description": stage.description,
            "system_instructions": stage.system_instructions,
            "model_override": stage.model_override,
            "driver_callable_turns": stage.driver_callable_turns,
            "verification_enabled": stage.verification_enabled,
            "verification_model": stage.verification_model,
            "verification_max_retries": stage.verification_max_retries,
            "verification_instructions": stage.verification_instructions,
            "output_mode": stage.output_mode,
            "resources": [],
        }

        for res in stage.resources:
            res_data: dict[str, Any] = {
                "resource_type": res.resource_type,
                "position": res.position,
                "sticky": res.sticky,
            }

            if res.resource_type == "lorebook":
                from app.models.lorebook import Lorebook
                lb_result = await db.execute(
                    select(Lorebook)
                    .where(Lorebook.id == res.resource_id)
                    .options(selectinload(Lorebook.entries))
                )
                lb = lb_result.scalar_one_or_none()
                if lb:
                    res_data["resource_name"] = lb.name
                    res_data["resource_content"] = {
                        "name": lb.name,
                        "description": lb.description,
                        "entries": [
                            {
                                "name": e.name,
                                "keys": json.loads(e.keys) if e.keys else [],
                                "secondary_keys": json.loads(e.secondary_keys) if e.secondary_keys else [],
                                "content": e.content,
                                "content_summary": e.content_summary,
                                "content_bullets": e.content_bullets,
                                "position": e.position,
                                "insertion_order": e.insertion_order,
                                "is_constant": e.is_constant,
                                "is_selective": e.is_selective,
                                "is_disabled": e.is_disabled,
                                "character_limit": e.character_limit,
                            }
                            for e in lb.entries
                        ],
                    }

            elif res.resource_type == "cantrip":
                from app.models.cantrip import Cantrip
                c_result = await db.execute(
                    select(Cantrip).where(Cantrip.id == res.resource_id)
                )
                c = c_result.scalar_one_or_none()
                if c:
                    res_data["resource_name"] = c.name
                    res_data["resource_content"] = {
                        "name": c.name,
                        "description": c.description,
                        "llm_instructions": c.llm_instructions,
                        "code": c.code,
                    }

            stage_data["resources"].append(res_data)

        export_data["stages"].append(stage_data)

    return export_data


class MapImportRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    data: dict[str, Any]
    resource_mode: str = "keep_both"  # keep_both | reuse | overwrite


@router.post("/import", response_model=MapResponse, status_code=status.HTTP_201_CREATED)
async def import_map(
    req: MapImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Import a map from exported JSON. Creates copies of embedded lorebooks and cantrips.

    resource_mode controls how duplicate resources are handled:
    - keep_both (default): Always create new copies
    - reuse: Link to existing resource with same name+content if found
    - overwrite: Update existing resource with same name
    """
    data = req.data
    mode = req.resource_mode

    async def _find_existing_lorebook(name: str, user_id: str):
        from app.models.lorebook import Lorebook
        result = await db.execute(
            select(Lorebook).where(Lorebook.user_id == user_id, Lorebook.name == name)
        )
        return result.scalar_one_or_none()

    async def _find_existing_cantrip(name: str, user_id: str):
        from app.models.cantrip import Cantrip
        result = await db.execute(
            select(Cantrip).where(Cantrip.user_id == user_id, Cantrip.name == name)
        )
        return result.scalar_one_or_none()

    m = Map(
        user_id=current_user.id,
        name=req.name or data.get("name", "Imported Map"),
        description=req.description or data.get("description", ""),
        tag="",
        is_public=False,
        is_active=True,
        version=data.get("version", "1.0"),
        author=data.get("author", ""),
        global_llm_instructions=data.get("global_llm_instructions", ""),
    )
    db.add(m)
    await db.flush()

    for stage_data in data.get("stages", []):
        stage = MapStage(
            map_id=m.id,
            stage_order=stage_data.get("stage_order", 1),
            name=stage_data.get("name", "Stage"),
            description=stage_data.get("description", ""),
            system_instructions=stage_data.get("system_instructions", ""),
            model_override=stage_data.get("model_override", ""),
            driver_callable_turns=stage_data.get("driver_callable_turns", 0),
            verification_enabled=stage_data.get("verification_enabled", False),
            verification_model=stage_data.get("verification_model", ""),
            verification_max_retries=stage_data.get("verification_max_retries", 2),
            verification_instructions=stage_data.get("verification_instructions", ""),
            output_mode=stage_data.get("output_mode", "persist"),
        )
        db.add(stage)
        await db.flush()

        for res_data in stage_data.get("resources", []):
            res_type = res_data.get("resource_type", "")
            content = res_data.get("resource_content", {})

            if res_type == "lorebook" and content:
                from app.models.lorebook import Lorebook
                from app.models.lorebook_entry import LorebookEntry
                lb_name = content.get("name", "Imported Lorebook")

                resource_id = None
                if mode != "keep_both":
                    existing_lb = await _find_existing_lorebook(lb_name, current_user.id)
                    if existing_lb:
                        if mode == "reuse":
                            resource_id = existing_lb.id
                        elif mode == "overwrite":
                            existing_lb.description = content.get("description", "")
                            await db.flush()
                            from sqlalchemy import delete as sa_delete
                            await db.execute(sa_delete(LorebookEntry).where(LorebookEntry.lorebook_id == existing_lb.id))
                            for entry_data in content.get("entries", []):
                                db.add(LorebookEntry(
                                    lorebook_id=existing_lb.id,
                                    name=entry_data.get("name", ""),
                                    keys=json.dumps(entry_data.get("keys", [])),
                                    secondary_keys=json.dumps(entry_data.get("secondary_keys", entry_data.get("secondary_key", []))),
                                    content=entry_data.get("content", ""),
                                    content_summary=entry_data.get("content_summary", ""),
                                    content_bullets=entry_data.get("content_bullets", ""),
                                    position=entry_data.get("position", "before_last_message"),
                                    insertion_order=entry_data.get("insertion_order", 10),
                                    is_constant=entry_data.get("is_constant", False),
                                    is_selective=entry_data.get("is_selective", False),
                                    is_disabled=entry_data.get("is_disabled", False),
                                    character_limit=entry_data.get("character_limit", 0),
                                ))
                            resource_id = existing_lb.id

                if not resource_id:
                    lb = Lorebook(
                        user_id=current_user.id,
                        name=lb_name,
                        description=content.get("description", ""),
                        is_active=True,
                    )
                    db.add(lb)
                    await db.flush()

                    for entry_data in content.get("entries", []):
                        db.add(LorebookEntry(
                            lorebook_id=lb.id,
                            name=entry_data.get("name", ""),
                            keys=json.dumps(entry_data.get("keys", [])),
                            secondary_keys=json.dumps(entry_data.get("secondary_keys", entry_data.get("secondary_key", []))),
                            content=entry_data.get("content", ""),
                            content_summary=entry_data.get("content_summary", ""),
                            content_bullets=entry_data.get("content_bullets", ""),
                            position=entry_data.get("position", "before_last_message"),
                            insertion_order=entry_data.get("insertion_order", 10),
                            is_constant=entry_data.get("is_constant", False),
                            is_selective=entry_data.get("is_selective", False),
                            is_disabled=entry_data.get("is_disabled", False),
                            character_limit=entry_data.get("character_limit", 0),
                        ))
                    resource_id = lb.id

            elif res_type == "cantrip" and content:
                from app.models.cantrip import Cantrip
                c_name = content.get("name", "Imported Cantrip")

                resource_id = None
                if mode != "keep_both":
                    existing_c = await _find_existing_cantrip(c_name, current_user.id)
                    if existing_c:
                        if mode == "reuse":
                            resource_id = existing_c.id
                        elif mode == "overwrite":
                            existing_c.description = content.get("description", "")
                            existing_c.llm_instructions = content.get("llm_instructions", "")
                            existing_c.code = content.get("code", "")
                            await db.flush()
                            resource_id = existing_c.id

                if not resource_id:
                    c = Cantrip(
                        user_id=current_user.id,
                        name=c_name,
                        description=content.get("description", ""),
                        llm_instructions=content.get("llm_instructions", ""),
                        code=content.get("code", ""),
                        is_active=True,
                        run_driver_callable=True,
                    )
                    db.add(c)
                    await db.flush()
                    resource_id = c.id

            else:
                resource_id = ""

            if resource_id:
                db.add(MapStageResource(
                    map_stage_id=stage.id,
                    resource_type=res_type,
                    resource_id=resource_id,
                    position=res_data.get("position", "pre_driver"),
                    sticky=res_data.get("sticky", False),
                ))

    await db.commit()

    result = await db.execute(
        select(Map)
        .where(Map.id == m.id)
        .options(selectinload(Map.stages).selectinload(MapStage.resources))
    )
    return _map_to_response(result.scalar_one())
