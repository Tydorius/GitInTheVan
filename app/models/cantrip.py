from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class Cantrip(Base):
    __tablename__ = "cantrips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    code: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hook_type: Mapped[str] = mapped_column(String(32), default="pre", nullable=False)
    run_pre_driver: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    run_driver_callable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_pre_navigator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    run_post_navigator: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tag: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    execution_order: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    budget_weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Phase 20: JSON list of lorebook names whose entries are seeded read-only
    # into this cantrip's sandbox context (context.lorebook). The column is
    # nullable on MySQL (no literal TEXT defaults there); readers treat NULL
    # as "none configured" -- see parse reads helper in dynamic_lorebook.py.
    lorebook_reads: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Package 3 (0.39.0): JSON list of registered response-tag names. GitV
    # extracts these tags from the model's final reply, strips them before
    # the client, and delivers the extractions to post-Navigator cantrip
    # context the same turn. Same MySQL NULL-reads-as-empty convention.
    response_tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # 0.45.0: JSON list of user_data key prefixes this cantrip can access.
    # Same MySQL NULL-reads-as-empty convention.
    user_data_prefixes: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # 0.50.0 (N13): when True, card-field appends from this cantrip stay in
    # messages[0] (the cached system prompt) even under cache_friendly_mode.
    card_edits_standing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship(back_populates="cantrips")
