import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.auth import (
    create_access_token,
    generate_api_key,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SetupResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    api_key: str


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup_admin(
    req: SetupRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.is_admin.is_(True)))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Admin user already exists")

    raw_api_key, key_hash = generate_api_key()
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        gitv_api_key=key_hash,
        is_admin=True,
    )
    db.add(user)
    await db.flush()

    user_settings = UserSettings(user_id=user.id)
    db.add(user_settings)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(user.id, user.username, user.is_admin)
    logger.info("Admin user created: %s", user.username)
    return SetupResponse(access_token=token, api_key=raw_api_key)


@router.post("/login")
async def login(
    req: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(user.id, user.username, user.is_admin)
    return AuthResponse(access_token=token)
