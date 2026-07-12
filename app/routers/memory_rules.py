import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.memory_rule import MemoryRule
from app.models.user import User
from app.services.admin import get_admin_settings
from app.services.content_guard import check_size, sanitize_and_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory-rules", tags=["memory-rules"])


class MemoryRuleCreate(BaseModel):
    name: str
    description: str = ""
    summarization_enabled: bool = True
    token_threshold: int = 0
    keep_recent: int = 0
    prompt: str = ""
    tag: str = ""
    execution_order: int = 10
    is_active: bool = True


class MemoryRuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    summarization_enabled: bool | None = None
    token_threshold: int | None = None
    keep_recent: int | None = None
    prompt: str | None = None
    tag: str | None = None
    execution_order: int | None = None
    is_active: bool | None = None


class MemoryRuleResponse(BaseModel):
    id: str
    name: str
    description: str
    summarization_enabled: bool
    token_threshold: int
    keep_recent: int
    prompt: str
    tag: str
    execution_order: int
    is_active: bool


class MemoryRuleListItem(BaseModel):
    id: str
    name: str
    description: str
    summarization_enabled: bool
    token_threshold: int
    keep_recent: int
    tag: str
    execution_order: int
    is_active: bool


class MemoryRuleListResponse(BaseModel):
    rules: list[MemoryRuleListItem]


def _rule_to_response(rule: MemoryRule) -> MemoryRuleResponse:
    return MemoryRuleResponse(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        summarization_enabled=rule.summarization_enabled,
        token_threshold=rule.token_threshold,
        keep_recent=rule.keep_recent,
        prompt=rule.prompt,
        tag=rule.tag,
        execution_order=rule.execution_order,
        is_active=rule.is_active,
    )


def _rule_to_list_item(rule: MemoryRule) -> MemoryRuleListItem:
    return MemoryRuleListItem(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        summarization_enabled=rule.summarization_enabled,
        token_threshold=rule.token_threshold,
        keep_recent=rule.keep_recent,
        tag=rule.tag,
        execution_order=rule.execution_order,
        is_active=rule.is_active,
    )


async def _check_tag_unique(db: AsyncSession, tag: str, user_id: str, exclude_id: str | None = None) -> None:
    if not tag:
        return
    query = text("SELECT id FROM memory_rules WHERE tag = :tag AND user_id = :uid")
    params: dict = {"tag": tag, "uid": user_id}
    if exclude_id:
        query = text("SELECT id FROM memory_rules WHERE tag = :tag AND user_id = :uid AND id != :rid")
        params["rid"] = exclude_id
    result = await db.execute(query, params)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already in use")


@router.get("", response_model=MemoryRuleListResponse)
async def list_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(MemoryRule)
        .where(MemoryRule.user_id == current_user.id)
        .order_by(MemoryRule.execution_order, MemoryRule.created_at)
    )
    rules = result.scalars().all()
    return MemoryRuleListResponse(rules=[_rule_to_list_item(r) for r in rules])


@router.get("/{rule_id}", response_model=MemoryRuleResponse)
async def get_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(MemoryRule).where(MemoryRule.id == rule_id, MemoryRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory rule not found")
    return _rule_to_response(rule)


@router.post("", response_model=MemoryRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    req: MemoryRuleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _check_tag_unique(db, req.tag, current_user.id)

    admin_settings = await get_admin_settings()
    check_size(req.prompt, admin_settings.max_rule_size_kb * 1024, "Memory rule prompt")
    req.prompt = await sanitize_and_log(db, current_user.id, req.prompt, "memory_rule")

    rule = MemoryRule(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        summarization_enabled=req.summarization_enabled,
        token_threshold=req.token_threshold,
        keep_recent=req.keep_recent,
        prompt=req.prompt,
        tag=req.tag,
        execution_order=req.execution_order,
        is_active=req.is_active,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule_to_response(rule)


@router.put("/{rule_id}", response_model=MemoryRuleResponse)
async def update_rule(
    rule_id: str,
    req: MemoryRuleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(MemoryRule).where(MemoryRule.id == rule_id, MemoryRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory rule not found")

    if req.tag is not None and req.tag != rule.tag:
        await _check_tag_unique(db, req.tag, current_user.id, rule_id)
        rule.tag = req.tag
    if req.name is not None:
        rule.name = req.name
    if req.description is not None:
        rule.description = req.description
    if req.summarization_enabled is not None:
        rule.summarization_enabled = req.summarization_enabled
    if req.token_threshold is not None:
        rule.token_threshold = req.token_threshold
    if req.keep_recent is not None:
        rule.keep_recent = req.keep_recent
    if req.prompt is not None:
        admin_settings = await get_admin_settings()
        check_size(req.prompt, admin_settings.max_rule_size_kb * 1024, "Memory rule prompt")
        rule.prompt = await sanitize_and_log(db, current_user.id, req.prompt, "memory_rule", rule_id)
    if req.execution_order is not None:
        rule.execution_order = req.execution_order
    if req.is_active is not None:
        rule.is_active = req.is_active

    await db.commit()
    await db.refresh(rule)
    return _rule_to_response(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(MemoryRule).where(MemoryRule.id == rule_id, MemoryRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory rule not found")
    await db.delete(rule)
    await db.commit()
