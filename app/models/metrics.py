from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PromptObjectMetric(Base):
    """One prompt-mutation measurement: what a pipeline stage added to (or
    removed from) the prompt of one request.

    One row per triggered object per request -- the same lorebook entry fires
    once per message that matches it, so (chat_id, object_type, object_id) is a
    lookup index, never a unique key. Token counts are estimates (chars/4 via
    ``budget.estimate_tokens``), not measurements.

    ``parent_id`` rolls an entry-level row up to its container: the lorebook id
    for ``lorebook_entry`` rows, the map id for stage objects. ``object_id`` is
    a fixed slug for types with no natural object (e.g. ``memory_injection``).

    ``context`` marks sandbox/replay runs so default aggregates can exclude
    them without losing the rows; ``is_retry`` marks rows produced inside a
    navigator-rejection retry cycle.
    """

    __tablename__ = "prompt_object_metrics"
    __table_args__ = (
        Index("ix_prompt_object_metrics_lookup", "chat_id", "object_type", "object_id"),
        Index("ix_prompt_object_metrics_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    object_type: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    object_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    parent_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")

    pre_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    post_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    difference: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    context: Mapped[str] = mapped_column(String(16), nullable=False, default="live", server_default="live")
    is_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"),
    )


class LlmCallMetric(Base):
    """One upstream LLM call: driver, verification judge, summarizer or map
    stage.

    Fed from the same ``record_llm_call`` hook the debug trace uses, so it
    records on every request, not only with debug mode on. ``tokens_source``
    says whether the token columns came from the upstream ``usage`` object
    ("usage", exact for that provider) or the chars/4 estimator ("estimate");
    an aggregate over mixed sources is a wrong aggregate, so the summary
    endpoint reports the split rather than blending it.

    ``endpoint_name`` is denormalised so cost history stays readable after the
    endpoint is deleted; ``rule_id`` is empty unless the call was a judge run
    of a specific verification rule.
    """

    __tablename__ = "llm_call_metrics"
    __table_args__ = (
        Index("ix_llm_call_metrics_user_created", "user_id", "created_at"),
        Index("ix_llm_call_metrics_chat", "chat_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")

    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    endpoint_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")
    endpoint_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")

    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    # 0 = first candidate; >0 = this call was a failover attempt. The only way
    # "how often does my primary endpoint fail over" is answerable.
    failover_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_source: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default="")

    rule_id: Mapped[str] = mapped_column(String(36), nullable=False, default="", server_default="")

    context: Mapped[str] = mapped_column(String(16), nullable=False, default="live", server_default="live")
    is_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC), server_default=text("CURRENT_TIMESTAMP"),
    )
