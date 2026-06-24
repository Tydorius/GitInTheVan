from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.endpoint import Endpoint
    from app.models.user import User


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    default_endpoint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="SET NULL"), nullable=True
    )
    default_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    verification_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_endpoint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="SET NULL"), nullable=True
    )
    verification_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    preserve_thinking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    gitv_status: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    simulated_streaming_speed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summarization_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    summarization_endpoint_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("endpoints.id", ondelete="SET NULL"), nullable=True
    )
    summarization_model: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    summarization_token_threshold: Mapped[int] = mapped_column(Integer, default=8000, nullable=False)
    summarization_keep_recent: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    summarization_prompt: Mapped[str] = mapped_column(
        Text, nullable=False,
        default="Summarize the following roleplay conversation excerpt. Preserve key facts, "
        "character development, important plot points, established relationships, locations, "
        "items, and any commitments or promises made. Write in concise bullet points. "
        "Do not add new information. Output only the summary.",
    )
    forbidden_words_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    forbidden_words_case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    driver_callable_turns: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    bypass_method: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    prefill_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_budget_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    context_window_override: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    debug_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_map_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="settings")
    default_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[default_endpoint_id])
    verification_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[verification_endpoint_id])
    summarization_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[summarization_endpoint_id])
