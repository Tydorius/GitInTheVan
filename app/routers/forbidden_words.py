import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.forbidden_word import ForbiddenWord
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.forbidden_words import _scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/forbidden-words", tags=["forbidden-words"])


class ForbiddenWordCreate(BaseModel):
    phrase: str
    is_regex: bool = False


class ForbiddenWordResponse(BaseModel):
    id: str
    phrase: str
    is_regex: bool


class ForbiddenWordListResponse(BaseModel):
    words: list[ForbiddenWordResponse]
    total: int


class ForbiddenSettingsResponse(BaseModel):
    forbidden_words_enabled: bool
    forbidden_words_case_sensitive: bool


class ForbiddenSettingsUpdate(BaseModel):
    forbidden_words_enabled: bool | None = None
    forbidden_words_case_sensitive: bool | None = None


class ForbiddenTestRequest(BaseModel):
    content: str


class ForbiddenTestResponse(BaseModel):
    has_matches: bool
    summary: str
    match_count: int


async def _get_or_create_settings(db: AsyncSession, user_id: str) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


@router.get("/settings")
async def get_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings = await _get_or_create_settings(db, current_user.id)
    return ForbiddenSettingsResponse(
        forbidden_words_enabled=settings.forbidden_words_enabled,
        forbidden_words_case_sensitive=settings.forbidden_words_case_sensitive,
    )


@router.put("/settings")
async def update_settings(
    req: ForbiddenSettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings = await _get_or_create_settings(db, current_user.id)
    if req.forbidden_words_enabled is not None:
        settings.forbidden_words_enabled = req.forbidden_words_enabled
    if req.forbidden_words_case_sensitive is not None:
        settings.forbidden_words_case_sensitive = req.forbidden_words_case_sensitive
    await db.commit()
    await db.refresh(settings)
    return ForbiddenSettingsResponse(
        forbidden_words_enabled=settings.forbidden_words_enabled,
        forbidden_words_case_sensitive=settings.forbidden_words_case_sensitive,
    )


@router.get("")
async def list_words(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ForbiddenWord)
        .where(ForbiddenWord.user_id == current_user.id)
        .order_by(ForbiddenWord.created_at)
    )
    words = result.scalars().all()
    return ForbiddenWordListResponse(
        words=[
            ForbiddenWordResponse(id=w.id, phrase=w.phrase, is_regex=w.is_regex)
            for w in words
        ],
        total=len(words),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_word(
    req: ForbiddenWordCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    phrase = req.phrase.strip()
    if not phrase:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Phrase cannot be empty")
    word = ForbiddenWord(
        user_id=current_user.id,
        phrase=phrase,
        is_regex=req.is_regex,
    )
    db.add(word)
    await db.commit()
    await db.refresh(word)
    return ForbiddenWordResponse(id=word.id, phrase=word.phrase, is_regex=word.is_regex)


@router.delete("/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(
    word_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ForbiddenWord).where(
            ForbiddenWord.id == word_id,
            ForbiddenWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if word is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Forbidden word not found")
    await db.delete(word)
    await db.commit()


@router.post("/test")
async def test_scan(
    req: ForbiddenTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    settings = await _get_or_create_settings(db, current_user.id)
    result = await db.execute(
        select(ForbiddenWord)
        .where(ForbiddenWord.user_id == current_user.id)
        .order_by(ForbiddenWord.created_at)
    )
    words = list(result.scalars().all())
    if not words:
        return ForbiddenTestResponse(has_matches=False, summary="", match_count=0)

    scan_result = _scan(req.content, words, settings.forbidden_words_case_sensitive)
    return ForbiddenTestResponse(
        has_matches=scan_result.has_matches,
        summary=scan_result.summary,
        match_count=len(scan_result.matches),
    )
