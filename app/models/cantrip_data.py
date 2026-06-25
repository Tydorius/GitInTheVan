from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CantripData(Base):
    __tablename__ = "cantrip_data"
    __table_args__ = (
        UniqueConstraint("user_id", "cantrip_id", "key", name="uq_cantrip_data_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cantrip_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cantrips.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column("key", String(256), nullable=False, quote=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
