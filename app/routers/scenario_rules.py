import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.scenario_rule import ScenarioRule
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scenario-rules", tags=["scenario-rules"])

DEFAULT_PROMPT = (
    "Summarize the following character definition and scenario information while preserving all essential details. Keep:\n"
    "- Character name, personality, appearance, and core traits\n"
    "- Key relationships and power dynamics\n"
    "- Setting/world details relevant to the scene\n"
    "- Important rules, restrictions, or behavioral guidelines\n"
    "- Any specific instructions about writing style or format\n\n"
    "Compress redundant descriptions and merge overlapping information. Be concise but complete.\n"
    "Output only the summarized text, no commentary."
)


class ScenarioRuleCreate(BaseModel):
    name: str
    token_threshold: int = 2000
    fire_position: str = "pre"
    endpoint_id: str | None = None
    model: str = ""
    prompt: str = ""
    is_active: bool = True


class ScenarioRuleUpdate(BaseModel):
    name: str | None = None
    token_threshold: int | None = None
    fire_position: str | None = None
    endpoint_id: str | None = None
    model: str | None = None
    prompt: str | None = None
    is_active: bool | None = None


class ScenarioRuleResponse(BaseModel):
    id: str
    name: str
    token_threshold: int
    fire_position: str
    endpoint_id: str | None
    model: str
    prompt: str
    is_active: bool
    created_at: str


class ScenarioRuleListResponse(BaseModel):
    rules: list[ScenarioRuleResponse]


def _rule_to_response(rule: ScenarioRule) -> ScenarioRuleResponse:
    return ScenarioRuleResponse(
        id=rule.id,
        name=rule.name,
        token_threshold=rule.token_threshold,
        fire_position=rule.fire_position,
        endpoint_id=rule.endpoint_id,
        model=rule.model,
        prompt=rule.prompt,
        is_active=rule.is_active,
        created_at=rule.created_at.isoformat() if rule.created_at else "",
    )


@router.get("", response_model=ScenarioRuleListResponse)
async def list_rules(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ScenarioRule)
        .where(ScenarioRule.user_id == current_user.id)
        .order_by(ScenarioRule.fire_position, ScenarioRule.token_threshold.desc())
    )
    rules = result.scalars().all()
    return ScenarioRuleListResponse(rules=[_rule_to_response(r) for r in rules])


@router.get("/default-prompt")
async def get_default_prompt(
    current_user: Annotated[User, Depends(get_current_user)],
):
    return {"prompt": DEFAULT_PROMPT}


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScenarioRuleResponse)
async def create_rule(
    req: ScenarioRuleCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if req.fire_position not in ("pre", "post"):
        raise HTTPException(status_code=400, detail="fire_position must be 'pre' or 'post'")

    rule = ScenarioRule(
        user_id=current_user.id,
        name=req.name,
        token_threshold=max(100, req.token_threshold),
        fire_position=req.fire_position,
        endpoint_id=req.endpoint_id or None,
        model=req.model,
        prompt=req.prompt,
        is_active=req.is_active,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    logger.info("Scenario rule created: %s (%s) for user: %s", rule.name, rule.fire_position, current_user.username)
    return _rule_to_response(rule)


@router.get("/{rule_id}", response_model=ScenarioRuleResponse)
async def get_rule(
    rule_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ScenarioRule).where(ScenarioRule.id == rule_id, ScenarioRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _rule_to_response(rule)


@router.put("/{rule_id}", response_model=ScenarioRuleResponse)
async def update_rule(
    rule_id: str,
    req: ScenarioRuleUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ScenarioRule).where(ScenarioRule.id == rule_id, ScenarioRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    if req.name is not None:
        rule.name = req.name
    if req.token_threshold is not None:
        rule.token_threshold = max(100, req.token_threshold)
    if req.fire_position is not None:
        if req.fire_position not in ("pre", "post"):
            raise HTTPException(status_code=400, detail="fire_position must be 'pre' or 'post'")
        rule.fire_position = req.fire_position
    if req.endpoint_id is not None:
        rule.endpoint_id = req.endpoint_id or None
    if req.model is not None:
        rule.model = req.model
    if req.prompt is not None:
        rule.prompt = req.prompt
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
        select(ScenarioRule).where(ScenarioRule.id == rule_id, ScenarioRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Rule not found")

    await db.delete(rule)
    await db.commit()
