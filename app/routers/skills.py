import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.endpoint import Endpoint
from app.models.skill import EndpointSkill, Skill
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    type: str = "skill"


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    type: str | None = None


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    content: str
    type: str
    endpoints: list[str]
    created_at: str


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]


class AttachRequest(BaseModel):
    endpoint_id: str


def _skill_to_response(skill: Skill, endpoint_ids: list[str] | None = None) -> SkillResponse:
    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        type=skill.type,
        endpoints=endpoint_ids or [],
        created_at=skill.created_at.isoformat() if skill.created_at else "",
    )


async def _get_endpoint_ids_for_skill(db: AsyncSession, skill_id: str) -> list[str]:
    result = await db.execute(
        select(EndpointSkill.endpoint_id).where(EndpointSkill.skill_id == skill_id)
    )
    return [r[0] for r in result.all()]


@router.get("", response_model=SkillListResponse)
async def list_skills(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Skill).where(Skill.user_id == current_user.id).order_by(Skill.created_at)
    )
    skills = result.scalars().all()

    skill_list = []
    for s in skills:
        ep_ids = await _get_endpoint_ids_for_skill(db, s.id)
        skill_list.append(_skill_to_response(s, ep_ids))

    return SkillListResponse(skills=skill_list)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SkillResponse)
async def create_skill(
    req: SkillCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if req.type not in ("skill", "sample"):
        raise HTTPException(status_code=400, detail="Type must be 'skill' or 'sample'")

    skill = Skill(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        content=req.content,
        type=req.type,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    logger.info("Skill created: %s (%s) for user: %s", skill.name, skill.type, current_user.username)
    return _skill_to_response(skill, [])


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id)
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    ep_ids = await _get_endpoint_ids_for_skill(db, skill_id)
    return _skill_to_response(skill, ep_ids)


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: str,
    req: SkillUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id)
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    if req.name is not None:
        skill.name = req.name
    if req.description is not None:
        skill.description = req.description
    if req.content is not None:
        skill.content = req.content
    if req.type is not None:
        if req.type not in ("skill", "sample"):
            raise HTTPException(status_code=400, detail="Type must be 'skill' or 'sample'")
        skill.type = req.type

    await db.commit()
    await db.refresh(skill)

    ep_ids = await _get_endpoint_ids_for_skill(db, skill_id)
    return _skill_to_response(skill, ep_ids)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id)
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    await db.delete(skill)
    await db.commit()


@router.get("/for-endpoint/{endpoint_id}", response_model=SkillListResponse)
async def get_skills_for_endpoint(
    endpoint_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    ep_result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.user_id == current_user.id)
    )
    if ep_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    result = await db.execute(
        select(Skill)
        .join(EndpointSkill, EndpointSkill.skill_id == Skill.id)
        .where(EndpointSkill.endpoint_id == endpoint_id, Skill.user_id == current_user.id)
        .order_by(Skill.created_at)
    )
    skills = result.scalars().all()
    return SkillListResponse(skills=[_skill_to_response(s) for s in skills])


@router.post("/{skill_id}/attach", status_code=status.HTTP_201_CREATED)
async def attach_skill(
    skill_id: str,
    req: AttachRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    skill_result = await db.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == current_user.id)
    )
    if skill_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    ep_result = await db.execute(
        select(Endpoint).where(Endpoint.id == req.endpoint_id, Endpoint.user_id == current_user.id)
    )
    if ep_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    existing = await db.execute(
        select(EndpointSkill).where(
            EndpointSkill.skill_id == skill_id,
            EndpointSkill.endpoint_id == req.endpoint_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return {"status": "already_attached"}

    link = EndpointSkill(skill_id=skill_id, endpoint_id=req.endpoint_id)
    db.add(link)
    await db.commit()
    return {"status": "attached"}


@router.delete("/{skill_id}/attach/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_skill(
    skill_id: str,
    endpoint_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(EndpointSkill).where(
            EndpointSkill.skill_id == skill_id,
            EndpointSkill.endpoint_id == endpoint_id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    await db.delete(link)
    await db.commit()
