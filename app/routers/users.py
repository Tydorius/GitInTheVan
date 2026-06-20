import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.auth import generate_api_key, hash_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
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
                api_key=u.gitv_api_key,
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
    return UserResponse(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        api_key=raw_api_key,
    )
