from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.lorebook import Lorebook


class LorebookEntry(Base):
    __tablename__ = "lorebook_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    lorebook_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lorebooks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    keys: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    secondary_keys: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_bullets: Mapped[str] = mapped_column(Text, nullable=False, default="")
    position: Mapped[str] = mapped_column(
        String(32), nullable=False, default="before_last_message"
    )
    insertion_order: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_constant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_selective: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    character_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lorebook: Mapped[Lorebook] = relationship(back_populates="entries")
