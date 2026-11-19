from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.lorebook import Lorebook


class ConversationDynamicLorebook(Base):
    """Phase 20: lorebooks created by scripts via ``context.lorebook``.

    The lexicon binding table allows one book per conversation; the original
    Phase 20 API is multi-book (``create({name})``/``get(name)``), so
    script-created books keep their own binding-shaped table and union into
    the fifth activation source alongside ConversationLorebookBinding.

    A row with ``conversation_id=""`` is not a binding at all: it records a
    script-created *cross-conversation* book (created with a tag, activated
    per rule 19 source 3). The empty sentinel never matches a real
    conversation in the injection-side lookup; it exists so a later request
    can re-find the book by (user, name) without colliding with the user's
    static lorebooks.
    """

    __tablename__ = "conversation_dynamic_lorebooks"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", "name", name="uq_conversation_dynamic_lorebook"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The conversation id as resolved at creation time: client header when the
    # platform sends one, else the internal rolling-hash id. "" marks an
    # unbound (cross-conversation, tagged) script-created book.
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    lorebook_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lorebooks.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lorebook: Mapped[Lorebook] = relationship()
