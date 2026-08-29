from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class DebugSandbox(Base):
    """A forked copy of one request, re-runnable from inside GitInTheVan.

    Made from a debug run: the request that produced it is copied, along with a
    fork of the conversation state it read (memories, chat data, summary). The
    fork gets its own ``sandbox_chat_id``, so firing it repeatedly accumulates
    state in the copy and never touches the conversation it came from.

    A sandbox run is initiated inside GitInTheVan and its response is returned to
    the Debug UI. There is no downstream client: the chat service the original
    request came from is not involved and receives nothing.
    """

    __tablename__ = "debug_sandboxes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # The run this was forked from, kept so the sandbox can be reset back to it.
    source_exchange_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    # The real conversation, recorded for display only. Never written to.
    source_chat_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # This sandbox's own conversation id. All forked state is keyed to it.
    sandbox_chat_id: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)

    messages: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship()
