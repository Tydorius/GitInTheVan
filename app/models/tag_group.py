from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class TagGroup(Base):
    __tablename__ = "tag_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship()
    members: Mapped[list[TagGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class TagGroupMember(Base):
    __tablename__ = "tag_group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tag_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    member_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    group: Mapped[TagGroup] = relationship(back_populates="members")
