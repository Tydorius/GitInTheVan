from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class AssistantConversation(Base):
    """One Assistant Pane conversation (Phase 26).

    The only state a turn writes to: every assistant message, tool call and
    pending confirmation is committed here before its SSE event is emitted, so
    a reload or an aborted stream lands on consistent state. `saved`
    conversations are pinned and exempt from the rotation that otherwise
    evicts the oldest unsaved conversation past `max_assistant_conversations`
    -- the same model `DebugExchange.saved` uses for debug runs.
    """

    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")

    # OpenAI-format message list, including `tool_calls` / `tool` roles.
    messages_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    # Rolling summary + the message index it covers, once compaction has run.
    compaction_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The turn's in-flight confirmation state (awaiting_confirmation /
    # awaiting_client), including tool_calls_used so a reload cannot reset the
    # per-turn cap.
    pending_json: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Tool names the user chose "Allow for session" for, in this conversation.
    session_allows_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    # Management UI route the conversation was started from, for page context.
    last_route: Mapped[str] = mapped_column(String(512), nullable=False, default="", server_default="")

    yolo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"),
        onupdate=lambda: datetime.now(UTC),
    )

    user: Mapped[User] = relationship()
