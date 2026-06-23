from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    max_driver_callable_turns: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    max_verification_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    rate_limit_proxy_per_min: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    rate_limit_api_per_min: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    max_map_stages: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    runtime_log_level: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
