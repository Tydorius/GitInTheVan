from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.api_key import ApiKey
    from app.models.cantrip import Cantrip
    from app.models.endpoint import Endpoint
    from app.models.lorebook import Lorebook
    from app.models.user_settings import UserSettings
    from app.models.verification import VerificationRule


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    gitv_api_key: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    endpoints: Mapped[list[Endpoint]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    settings: Mapped[UserSettings] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    lorebooks: Mapped[list[Lorebook]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    cantrips: Mapped[list[Cantrip]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    verification_rules: Mapped[list[VerificationRule]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
