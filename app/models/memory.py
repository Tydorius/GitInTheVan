from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", "key", name="uq_memory_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    key: Mapped[str] = mapped_column("key", String(256), nullable=False, quote=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory_type: Mapped[str] = mapped_column(String(32), default="flag", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
