import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.endpoint import Endpoint
from app.models.user import User
from app.models.verification import VerificationLog, VerificationRule
from app.services.verification import (
    check_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/verification", tags=["verification"])


class RuleCreate(BaseModel):
    name: str
    description: str = ""
    prompt: str = ""
    is_active: bool = True
    max_retries: int = 2
    execution_order: int = 10
    resubmission_strategy: str = "add_instructions"
    tag: str = ""
    verification_endpoint_id: str | None = None
    verification_model: str = ""


class RuleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    is_active: bool | None = None
    max_retries: int | None = None
    execution_order: int | None = None
    resubmission_strategy: str | None = None
    tag: str | None = None
    verification_endpoint_id: str | None = None
    verification_model: str | None = None


class RuleResponse(BaseModel):
    id: str
    name: str
    description: str
    prompt: str
    is_active: bool
    max_retries: int
    execution_order: int
    resubmission_strategy: str
    tag: str
    verification_endpoint_id: str | None
    verification_model: str


class RuleListItem(BaseModel):
    id: str
    name: str
    description: str
    prompt: str
    is_active: bool
    max_retries: int
    execution_order: int
    resubmission_strategy: str
    tag: str
    verification_endpoint_id: str | None
    verification_model: str


async def _check_tag_unique(
    db: AsyncSession, user_id: str, resource_type: str, tag: str, exclude_id: str | None = None
) -> bool:
    if not tag:
        return True
    table_map = {"lore": "lorebooks", "cantrip": "cantrips", "verify": "verification_rules"}
    table = table_map.get(resource_type, "")
    if not table:
        return True
    query = f"SELECT id FROM {table} WHERE tag = :tag AND user_id = :user_id"
    params = {"tag": tag, "user_id": user_id}
    if exclude_id:
        query += " AND id != :exclude_id"
        params["exclude_id"] = exclude_id
    result = await db.execute(text(query), params)
    return result.scalar_one_or_none() is None


class RuleListResponse(BaseModel):
    rules: list[RuleListItem]


class LogResponse(BaseModel):
    id: str
    rule_name: str
    conversation_id: str
    response_snippet: str
    violation_detected: bool
    violation_reason: str
    severity: str
    retries_used: int
    approved: bool
    created_at: str


class LogListResponse(BaseModel):
    logs: list[LogResponse]
    total: int


class VerificationTestRequest(BaseModel):
    content: str
    prompt: str | None = None
    rule_id: str | None = None
    endpoint_id: str | None = None
    model: str = ""


class VerificationTestResponse(BaseModel):
    violation: bool
    reason: str
    severity: str
    rule_name: str
    approved: bool


class VerificationSettingsResponse(BaseModel):
    verification_enabled: bool
    verification_endpoint_id: str | None
    verification_model: str


class VerificationSettingsUpdate(BaseModel):
    verification_enabled: bool | None = None
    verification_endpoint_id: str | None = None
    verification_model: str | None = None


def _rule_to_response(rule: VerificationRule) -> RuleResponse:
    return RuleResponse(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        prompt=rule.prompt,
        is_active=rule.is_active,
        max_retries=rule.max_retries,
        execution_order=rule.execution_order,
        resubmission_strategy=rule.resubmission_strategy,
        tag=rule.tag,
        verification_endpoint_id=rule.verification_endpoint_id,
        verification_model=rule.verification_model,
    )


def _rule_to_list_item(rule: VerificationRule) -> RuleListItem:
    return RuleListItem(
        id=rule.id,
        name=rule.name,
        description=rule.description,
        prompt=rule.prompt,
        is_active=rule.is_active,
        max_retries=rule.max_retries,
        execution_order=rule.execution_order,
        resubmission_strategy=rule.resubmission_strategy,
        tag=rule.tag,
        verification_endpoint_id=rule.verification_endpoint_id,
        verification_model=rule.verification_model,
    )


@router.get("/rules")
async def list_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(VerificationRule)
        .where(VerificationRule.user_id == current_user.id)
        .order_by(VerificationRule.execution_order, VerificationRule.created_at)
    )
    rules = result.scalars().all()
    return RuleListResponse(rules=[_rule_to_list_item(r) for r in rules])


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(VerificationRule).where(
            VerificationRule.id == rule_id, VerificationRule.user_id == current_user.id
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    return _rule_to_response(rule)


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    req: RuleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rule = VerificationRule(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        prompt=req.prompt,
        is_active=req.is_active,
        max_retries=req.max_retries,
        execution_order=req.execution_order,
        resubmission_strategy=req.resubmission_strategy,
        tag=req.tag,
        verification_endpoint_id=req.verification_endpoint_id,
        verification_model=req.verification_model,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule_to_response(rule)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    req: RuleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(VerificationRule).where(
            VerificationRule.id == rule_id, VerificationRule.user_id == current_user.id
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

    if req.tag is not None and req.tag != rule.tag:
        if not await _check_tag_unique(db, current_user.id, "verify", req.tag, rule_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already in use")

    for key, value in req.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)

    await db.commit()
    await db.refresh(rule)
    return _rule_to_response(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(VerificationRule).where(
            VerificationRule.id == rule_id, VerificationRule.user_id == current_user.id
        )
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
    await db.delete(rule)
    await db.commit()


@router.get("/logs")
async def list_logs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
):
    count_result = await db.execute(
        select(VerificationLog).where(VerificationLog.user_id == current_user.id)
    )
    total = len(count_result.scalars().all())

    result = await db.execute(
        select(VerificationLog)
        .where(VerificationLog.user_id == current_user.id)
        .order_by(VerificationLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    logs = result.scalars().all()
    return LogListResponse(
        logs=[
            LogResponse(
                id=log.id,
                rule_name=log.rule_name,
                conversation_id=log.conversation_id,
                response_snippet=log.response_snippet[:200],
                violation_detected=log.violation_detected,
                violation_reason=log.violation_reason,
                severity=log.severity,
                retries_used=log.retries_used,
                approved=log.approved,
                created_at=log.created_at.isoformat() if log.created_at else "",
            )
            for log in logs
        ],
        total=total,
    )


@router.post("/test")
async def test_verification(
    req: VerificationTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    prompt = req.prompt
    rule_name = "ad-hoc"

    if req.rule_id:
        rule_result = await db.execute(
            select(VerificationRule).where(
                VerificationRule.id == req.rule_id, VerificationRule.user_id == current_user.id
            )
        )
        rule = rule_result.scalar_one_or_none()
        if rule is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")
        prompt = rule.prompt
        rule_name = rule.name

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either prompt or rule_id must be provided",
        )

    from app.models.user_settings import UserSettings

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    user_settings = settings_result.scalar_one_or_none()

    endpoint_id = req.endpoint_id
    if not endpoint_id and user_settings:
        endpoint_id = user_settings.verification_endpoint_id

    if not endpoint_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No verification endpoint configured. Set one in settings or provide endpoint_id.",
        )

    ep_result = await db.execute(
        select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.enabled.is_(True))
    )
    endpoint = ep_result.scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Verification endpoint not found or disabled"
        )

    model = req.model or (user_settings.verification_model if user_settings else "")

    ad_hoc_rule = VerificationRule(
        user_id=current_user.id,
        name=rule_name,
        prompt=prompt,
        is_active=True,
        max_retries=0,
        execution_order=0,
    )

    check_result = await check_response(req.content, [ad_hoc_rule], endpoint, model)

    if check_result.judgments:
        j = check_result.judgments[0]
        return VerificationTestResponse(
            violation=j.violation,
            reason=j.reason,
            severity=j.severity,
            rule_name=j.rule_name,
            approved=check_result.approved,
        )

    return VerificationTestResponse(
        violation=False, reason="No judgment returned", severity="none",
        rule_name=rule_name, approved=True,
    )


@router.get("/settings")
async def get_verification_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.models.user_settings import UserSettings

    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        return VerificationSettingsResponse(
            verification_enabled=False, verification_endpoint_id=None, verification_model=""
        )

    return VerificationSettingsResponse(
        verification_enabled=user_settings.verification_enabled,
        verification_endpoint_id=user_settings.verification_endpoint_id,
        verification_model=user_settings.verification_model,
    )


@router.put("/settings")
async def update_verification_settings(
    req: VerificationSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.models.user_settings import UserSettings

    result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id)
        db.add(user_settings)

    if req.verification_enabled is not None:
        user_settings.verification_enabled = req.verification_enabled
    if req.verification_endpoint_id is not None:
        user_settings.verification_endpoint_id = req.verification_endpoint_id or None
    if req.verification_model is not None:
        user_settings.verification_model = req.verification_model

    await db.commit()
    await db.refresh(user_settings)

    return VerificationSettingsResponse(
        verification_enabled=user_settings.verification_enabled,
        verification_endpoint_id=user_settings.verification_endpoint_id,
        verification_model=user_settings.verification_model,
    )
