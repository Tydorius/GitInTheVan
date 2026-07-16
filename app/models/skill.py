from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.endpoint import Endpoint


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="skill", server_default="skill")
    budget_weight: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )

    endpoints: Mapped[list[EndpointSkill]] = relationship(back_populates="skill", cascade="all, delete-orphan")


class EndpointSkill(Base):
    __tablename__ = "endpoint_skills"
    __table_args__ = (UniqueConstraint("endpoint_id", "skill_id", name="uq_endpoint_skill"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    skill: Mapped[Skill] = relationship(back_populates="endpoints")
    endpoint: Mapped[Endpoint] = relationship()
