import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.tag_group import TagGroup, TagGroupMember
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tag-groups", tags=["tag-groups"])


class MemberSpec(BaseModel):
    member_type: str
    member_id: str


class GroupCreate(BaseModel):
    name: str
    tag: str = ""
    is_active: bool = False
    members: list[MemberSpec] = []


class GroupUpdate(BaseModel):
    name: str | None = None
    tag: str | None = None
    is_active: bool | None = None


class MembersUpdate(BaseModel):
    members: list[MemberSpec]


class MemberResponse(BaseModel):
    id: str
    member_type: str
    member_id: str


class GroupResponse(BaseModel):
    id: str
    name: str
    tag: str
    is_active: bool
    members: list[MemberResponse]
    created_at: str


class GroupListResponse(BaseModel):
    groups: list[GroupResponse]


def _group_to_response(group: TagGroup) -> GroupResponse:
    return GroupResponse(
        id=group.id,
        name=group.name,
        tag=group.tag,
        is_active=group.is_active,
        members=[
            MemberResponse(id=m.id, member_type=m.member_type, member_id=m.member_id)
            for m in group.members
        ],
        created_at=group.created_at.isoformat() if group.created_at else "",
    )


@router.get("", response_model=GroupListResponse)
async def list_tag_groups(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.user_id == current_user.id)
        .options(selectinload(TagGroup.members))
        .order_by(TagGroup.created_at.desc())
    )
    groups = result.scalars().all()
    return GroupListResponse(groups=[_group_to_response(g) for g in groups])


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_tag_group(
    body: GroupCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    group = TagGroup(
        user_id=current_user.id,
        name=body.name,
        tag=body.tag,
        is_active=body.is_active,
    )
    db.add(group)
    await db.flush()

    for m in body.members:
        member = TagGroupMember(
            group_id=group.id,
            member_type=m.member_type,
            member_id=m.member_id,
        )
        db.add(member)

    await db.commit()
    await db.refresh(group, attribute_names=["members"])
    return _group_to_response(group)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_tag_group(
    group_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.id == group_id, TagGroup.user_id == current_user.id)
        .options(selectinload(TagGroup.members))
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag group not found")
    return _group_to_response(group)


@router.put("/{group_id}", response_model=GroupResponse)
async def update_tag_group(
    group_id: str,
    body: GroupUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.id == group_id, TagGroup.user_id == current_user.id)
        .options(selectinload(TagGroup.members))
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag group not found")

    if body.name is not None:
        group.name = body.name
    if body.tag is not None:
        group.tag = body.tag
    if body.is_active is not None:
        group.is_active = body.is_active

    await db.commit()
    await db.refresh(group, attribute_names=["members"])
    return _group_to_response(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag_group(
    group_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(TagGroup).where(TagGroup.id == group_id, TagGroup.user_id == current_user.id)
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag group not found")

    await db.delete(group)
    await db.commit()


@router.put("/{group_id}/members", response_model=GroupResponse)
async def update_group_members(
    group_id: str,
    body: MembersUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.id == group_id, TagGroup.user_id == current_user.id)
        .options(selectinload(TagGroup.members))
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag group not found")

    await db.execute(
        delete(TagGroupMember).where(TagGroupMember.group_id == group_id)
    )

    for m in body.members:
        member = TagGroupMember(
            group_id=group_id,
            member_type=m.member_type,
            member_id=m.member_id,
        )
        db.add(member)

    await db.commit()
    await db.refresh(group, attribute_names=["members"])
    return _group_to_response(group)
