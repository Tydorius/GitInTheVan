from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationTurnCounter(Base):
    """Monotonic per-conversation request counter (0.35.0).

    `context.chat.message_count` is len(messages) of the client request, so
    it stops growing the moment the client trims history — useless as a turn
    index for stateful cantrips. This table counts client requests per
    conversation and only ever goes up. Keyed by the Phase-29 conversation
    identity (client header wins, internal rolling-hash id fallback), the
    same identity lexicon bindings and chat_data scoping use.
    """

    __tablename__ = "conversation_turn_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", name="uq_conversation_turn_counter"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Package 3 (item 2 slice, 0.39.0): "{message_count}:{sha256(trailing
    # user text)}" of the previous live request for this conversation. Equal
    # fingerprint on the next request means the client resent the same shape
    # -- the is_regenerate heuristic. Sandbox/replay reads never write here.
    # MySQL TEXT: nullable, NULL reads as empty.
    last_request_fingerprint: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC), nullable=False,
    )
