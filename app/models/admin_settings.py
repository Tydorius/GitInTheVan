from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    max_driver_callable_turns: Mapped[int] = mapped_column(Integer, default=2, server_default="2", nullable=False)
    max_verification_retries: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    rate_limit_proxy_per_min: Mapped[int] = mapped_column(Integer, default=60, server_default="60", nullable=False)
    rate_limit_api_per_min: Mapped[int] = mapped_column(Integer, default=120, server_default="120", nullable=False)
    max_map_stages: Mapped[int] = mapped_column(Integer, default=3, server_default="3", nullable=False)
    max_memory_size_mb: Mapped[int] = mapped_column(Integer, default=50, server_default="50", nullable=False)
    max_script_size_kb: Mapped[int] = mapped_column(Integer, default=50, server_default="50", nullable=False)
    max_rule_size_kb: Mapped[int] = mapped_column(Integer, default=25, server_default="25", nullable=False)
    max_lorebook_size_kb: Mapped[int] = mapped_column(Integer, default=500, server_default="500", nullable=False)
    url_blocklist: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    runtime_log_level: Mapped[str] = mapped_column(String(16), default="", server_default="", nullable=False)
    site_banner: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    site_banner_level: Mapped[str] = mapped_column(String(16), default="info", server_default="info", nullable=False)
    backup_schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    backup_schedule_days: Mapped[str] = mapped_column(String(32), default="", server_default="", nullable=False)
    backup_schedule_time: Mapped[str] = mapped_column(String(8), default="03:00", server_default="03:00", nullable=False)
    backup_retention_count: Mapped[int] = mapped_column(Integer, default=7, server_default="7", nullable=False)
    backup_dir: Mapped[str] = mapped_column(String(512), default="", server_default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
