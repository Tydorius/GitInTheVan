from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="settings")
    default_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[default_endpoint_id])
    verification_endpoint: Mapped[Endpoint | None] = relationship(foreign_keys=[verification_endpoint_id])
