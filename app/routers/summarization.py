import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.conversation_summary import ConversationSummary
from app.models.user import User
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

MIN_KEEP_RECENT = 3

router = APIRouter(prefix="/api/summarization", tags=["summarization"])


class SummarizationSettingsResponse(BaseModel):
    summarization_enabled: bool
    summarization_endpoint_id: str | None
    summarization_model: str
    summarization_token_threshold: int
    summarization_keep_recent: int
    summarization_prompt: str


class SummarizationSettingsUpdate(BaseModel):
    summarization_enabled: bool | None = None
    summarization_endpoint_id: str | None = None
    summarization_model: str | None = None
    summarization_token_threshold: int | None = None
    summarization_keep_recent: int | None = None
    summarization_prompt: str | None = None


class SummaryResponse(BaseModel):
    id: str
    internal_chat_id: str
    summary: str
    message_count: int
    token_estimate: int
    boundary_hash: str
    created_at: str
    updated_at: str


class SummaryListResponse(BaseModel):
    summaries: list[SummaryResponse]
    total: int


def _settings_to_response(s: UserSettings) -> SummarizationSettingsResponse:
    return SummarizationSettingsResponse(
        summarization_enabled=s.summarization_enabled,
        summarization_endpoint_id=s.summarization_endpoint_id,
        summarization_model=s.summarization_model,
        summarization_token_threshold=s.summarization_token_threshold,
        summarization_keep_recent=s.summarization_keep_recent,
        summarization_prompt=s.summarization_prompt,
    )


async def _get_or_create_settings(db: AsyncSession, user_id: str) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=user_id)
        db.add(user_settings)
        await db.commit()
        await db.refresh(user_settings)
    return user_settings


@router.get("/settings")
async def get_summarization_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user_settings = await _get_or_create_settings(db, current_user.id)
    return _settings_to_response(user_settings)


@router.put("/settings")
async def update_summarization_settings(
    req: SummarizationSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user_settings = await _get_or_create_settings(db, current_user.id)

    if req.summarization_enabled is not None:
        user_settings.summarization_enabled = req.summarization_enabled
    if req.summarization_endpoint_id is not None:
        user_settings.summarization_endpoint_id = req.summarization_endpoint_id or None
    if req.summarization_model is not None:
        user_settings.summarization_model = req.summarization_model
    if req.summarization_token_threshold is not None:
        user_settings.summarization_token_threshold = max(1, req.summarization_token_threshold)
    if req.summarization_keep_recent is not None:
        user_settings.summarization_keep_recent = max(MIN_KEEP_RECENT, req.summarization_keep_recent)
    if req.summarization_prompt is not None:
        user_settings.summarization_prompt = req.summarization_prompt

    await db.commit()
    await db.refresh(user_settings)
    return _settings_to_response(user_settings)


def _summary_to_response(s: ConversationSummary) -> SummaryResponse:
    return SummaryResponse(
        id=s.id,
        internal_chat_id=s.internal_chat_id,
        summary=s.summary,
        message_count=s.message_count,
        token_estimate=s.token_estimate,
        boundary_hash=s.boundary_hash,
        created_at=s.created_at.isoformat() if s.created_at else "",
        updated_at=s.updated_at.isoformat() if s.updated_at else "",
    )


@router.get("/summaries")
async def list_summaries(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    internal_chat_id: str | None = None,
):
    query = select(ConversationSummary).where(ConversationSummary.user_id == current_user.id)
    if internal_chat_id:
        query = query.where(ConversationSummary.internal_chat_id == internal_chat_id)
    result = await db.execute(query.order_by(ConversationSummary.updated_at.desc()))
    summaries = result.scalars().all()
    return SummaryListResponse(
        summaries=[_summary_to_response(s) for s in summaries],
        total=len(summaries),
    )


@router.delete("/summaries/{summary_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_summary(
    summary_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ConversationSummary).where(
            ConversationSummary.id == summary_id,
            ConversationSummary.user_id == current_user.id,
        )
    )
    summary = result.scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary not found")
    await db.delete(summary)
    await db.commit()
