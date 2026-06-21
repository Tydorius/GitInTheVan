from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class VerificationRule(Base):
    __tablename__ = "verification_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    execution_order: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    resubmission_strategy: Mapped[str] = mapped_column(
        String(32), default="add_instructions", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship(back_populates="verification_rules")


class VerificationLog(Base):
    __tablename__ = "verification_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("verification_rules.id", ondelete="SET NULL"), nullable=True
    )
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    response_snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    violation_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    violation_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    retries_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
