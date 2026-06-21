import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_admin
from app.models.cantrip import Cantrip
from app.models.chat_data import ChatData
from app.models.conversation_hash import ConversationHash
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.forbidden_word import ForbiddenWord
from app.models.lorebook import Lorebook
from app.models.memory import Memory
from app.models.user import User
from app.models.user_settings import UserSettings
from app.models.verification import VerificationLog, VerificationRule
from app.services.auth import generate_api_key, hash_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    password: str


class UpdateUserRequest(BaseModel):
    username: str | None = None
    is_disabled: bool | None = None


class ResetPasswordRequest(BaseModel):
    password: str


class UserResponse(BaseModel):
    id: str
    username: str
    is_admin: bool
    is_disabled: bool
    created_at: str


class CreateUserResponse(BaseModel):
    id: str
    username: str
    is_admin: bool
    api_key: str


class UserListResponse(BaseModel):
    users: list[UserResponse]


@router.get("")
async def list_users(
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).order_by(User.created_at))
    users = result.scalars().all()
    return UserListResponse(
        users=[
            UserResponse(
                id=u.id,
                username=u.username,
                is_admin=u.is_admin,
                is_disabled=u.is_disabled,
                created_at=u.created_at.isoformat() if u.created_at else "",
            )
            for u in users
        ]
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    req: CreateUserRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.username == req.username))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    raw_api_key, key_hash = generate_api_key()
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        gitv_api_key=key_hash,
        is_admin=False,
    )
    db.add(user)
    await db.flush()

    user_settings = UserSettings(user_id=user.id)
    db.add(user_settings)
    await db.commit()
    await db.refresh(user)

    logger.info("User created: %s by admin: %s", user.username, admin.username)
    return CreateUserResponse(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        api_key=raw_api_key,
    )


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    req: UpdateUserRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if req.username is not None:
        existing = await db.execute(
            select(User).where(User.username == req.username, User.id != user_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
        user.username = req.username

    if req.is_disabled is not None:
        if user.is_admin and req.is_disabled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot disable admin user",
            )
        user.is_disabled = req.is_disabled

    await db.commit()
    await db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        is_disabled=user.is_disabled,
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


@router.post("/{user_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    user_id: str,
    req: ResetPasswordRequest,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.password_hash = hash_password(req.password)
    await db.commit()
    logger.info("Password reset for user: %s by admin: %s", user.username, admin.username)
    return {"status": "ok"}


@router.post("/{user_id}/regenerate-api-key", status_code=status.HTTP_200_OK)
async def regenerate_api_key(
    user_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    raw_api_key, key_hash = generate_api_key()
    user.gitv_api_key = key_hash
    await db.commit()
    logger.info("API key regenerated for user: %s by admin: %s", user.username, admin.username)
    return {"api_key": raw_api_key}


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete admin user",
        )

    await _cascade_delete_user_data(db, user_id)

    await db.delete(user)
    await db.commit()
    logger.info("User deleted: %s by admin: %s", user.username, admin.username)


async def _cascade_delete_user_data(db: AsyncSession, user_id: str) -> None:
    """Explicitly delete all user-owned data to avoid orphaned rows.

    SQLAlchemy ORM cascade handles related models with relationships,
    but several tables reference users.id without an ORM relationship.
    This ensures complete cleanup regardless of FK enforcement state.
    """
    tables_with_user_id = [
        VerificationLog,
        VerificationRule,
        Memory,
        ConversationSummary,
        ConversationHash,
        ChatData,
        ForbiddenWord,
        Cantrip,
        Lorebook,
        UserSettings,
        Endpoint,
    ]

    for model in tables_with_user_id:
        await db.execute(
            delete(model).where(model.user_id == user_id)
        )
