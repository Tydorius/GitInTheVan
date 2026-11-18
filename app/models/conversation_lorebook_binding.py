from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.lorebook import Lorebook


class ConversationLorebookBinding(Base):
    """Fifth activation source (Phase 29a): binds one lorebook to one
    conversation. The lexicon compiler creates one binding per chat; its
    lorebook activates for that conversation regardless of tag or Active flag,
    adding to the activation union and never subtracting from anything.
    """

    __tablename__ = "conversation_lorebook_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", name="uq_conversation_lorebook_binding"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The conversation id as resolved at creation time: client header when the
    # platform sends one, else the internal rolling-hash id.
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    identity_source: Mapped[str] = mapped_column(String(16), nullable=False, default="rolling_hash")
    lorebook_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lorebooks.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lorebook: Mapped[Lorebook] = relationship()
