from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class Map(Base):
    __tablename__ = "maps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    author: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    global_llm_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship()
    stages: Mapped[list[MapStage]] = relationship(
        back_populates="map_obj", cascade="all, delete-orphan", order_by="MapStage.stage_order"
    )


class MapStage(Base):
    __tablename__ = "map_stages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    map_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("maps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stage_order: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    system_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    endpoint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="SET NULL"), nullable=True
    )
    model_override: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    driver_callable_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    verification_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_endpoint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="SET NULL"), nullable=True
    )
    verification_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    verification_max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    verification_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")

    output_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="persist")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    map_obj: Mapped[Map] = relationship(back_populates="stages")
    resources: Mapped[list[MapStageResource]] = relationship(
        back_populates="stage", cascade="all, delete-orphan"
    )


class MapStageResource(Base):
    __tablename__ = "map_stage_resources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    map_stage_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("map_stages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position: Mapped[str] = mapped_column(String(32), nullable=False, default="pre_driver")
    sticky: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    stage: Mapped[MapStage] = relationship(back_populates="resources")
