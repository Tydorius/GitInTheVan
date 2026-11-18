from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CantripRequest(Base):
    """A cantrip draft queued from chat (Phase 29b).

    `<CANTRIP:description>` writes a pending row; the Assistant pane lists
    them, seeds a conversation from request + context, and marks the draft
    installed or dismissed. With auto-install doubly gated on, the pipeline
    drafts and installs in-request and the row records the outcome.
    """

    __tablename__ = "cantrip_requests"
    __table_args__ = (
        Index("ix_cantrip_requests_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    request_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Capped transcript snapshot taken at queue time -- the same view of the
    # conversation the lexicon stage would see, so the pane (or the in-request
    # drafter) can work without re-reading the chat.
    context_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # pending | started | installed | dismissed | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Auto-install outcome: the notice text shown to the player and, on
    # refusal, the findings. Queue-only rows keep it empty.
    outcome_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
        onupdate=lambda: datetime.now(UTC),
    )
