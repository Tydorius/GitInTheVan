"""Persistence for assistant conversations.

The conversation row is the only state a turn writes to. Everything the loop
does -- an assistant message, a tool result, a pending confirmation, the
per-turn tool counter -- is committed here before its SSE event is emitted, so
a reload or an aborted stream lands on something consistent.

Sessions are short and opened per call rather than held for the life of a
stream: a `StreamingResponse` can stay open for minutes, and a yield-dependency
session would hold a pooled connection for all of it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.database import async_session
from app.models.assistant_conversation import AssistantConversation

logger = logging.getLogger(__name__)

JSON_MEDIA = "application/json"
MARKDOWN_MEDIA = "text/markdown; charset=utf-8"

# Cap on one tool result inside a Markdown export. Exports are for reading;
# an untruncated debug capture would drown the transcript.
EXPORT_TOOL_LIMIT = 4096

TITLE_LIMIT = 80


def loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("Assistant conversation blob was not valid JSON; using the default")
        return fallback


def to_dict(row: AssistantConversation, *, include_messages: bool = True) -> dict[str, Any]:
    """Serialize a row for the API."""
    out: dict[str, Any] = {
        "id": row.id,
        "title": row.title,
        "saved": row.saved,
        "yolo": row.yolo,
        "last_route": row.last_route,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "llm_calls": row.llm_calls,
        "tool_calls": row.tool_calls,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }
    if include_messages:
        out["messages"] = loads(row.messages_json, [])
        out["compaction"] = loads(row.compaction_json, {})
        out["pending"] = loads(row.pending_json, None)
        out["session_allows"] = loads(row.session_allows_json, [])
    return out


async def create(user_id: str, max_unsaved: int) -> tuple[AssistantConversation, str | None]:
    """Create a conversation, rotating the oldest unsaved one when at the cap.

    Rotation rather than refusal, matching how debug runs behave: a limit the
    user can hit while working is a wall, and a wall in an assistant is a bug
    report. Saved conversations are pinned and never rotated.
    """
    rotated_title: str | None = None
    async with async_session() as db:
        cap = max(1, int(max_unsaved or 1))
        count_result = await db.execute(
            select(func.count())
            .select_from(AssistantConversation)
            .where(
                AssistantConversation.user_id == user_id,
                AssistantConversation.saved.is_(False),
            )
        )
        unsaved = int(count_result.scalar() or 0)

        while unsaved >= cap:
            oldest_result = await db.execute(
                select(AssistantConversation)
                .where(
                    AssistantConversation.user_id == user_id,
                    AssistantConversation.saved.is_(False),
                )
                .order_by(AssistantConversation.created_at, AssistantConversation.id)
                .limit(1)
            )
            oldest = oldest_result.scalar_one_or_none()
            if oldest is None:
                break
            rotated_title = oldest.title or "(untitled)"
            await db.delete(oldest)
            await db.flush()
            unsaved -= 1

        row = AssistantConversation(user_id=user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row, rotated_title


async def get(user_id: str, conversation_id: str) -> AssistantConversation | None:
    """One conversation, scoped to its owner. Another user's id returns None."""
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()


async def list_conversations(user_id: str) -> list[dict[str, Any]]:
    """Every conversation for a user, newest first, without message bodies."""
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation)
            .where(AssistantConversation.user_id == user_id)
            .order_by(AssistantConversation.updated_at.desc())
        )
        rows = result.scalars().all()
        return [to_dict(row, include_messages=False) for row in rows]


async def delete(user_id: str, conversation_id: str) -> bool:
    """Delete a conversation. False when it does not exist for this user."""
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True


async def set_saved(user_id: str, conversation_id: str, saved: bool) -> AssistantConversation | None:
    """Pin or unpin a conversation against rotation."""
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.saved = bool(saved)
        row.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
        return row


async def patch(
    user_id: str,
    conversation_id: str,
    *,
    title: str | None = None,
    yolo: bool | None = None,
    last_route: str | None = None,
) -> AssistantConversation | None:
    """Update the user-editable fields. Only what is supplied changes."""
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation).where(
                AssistantConversation.id == conversation_id,
                AssistantConversation.user_id == user_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if title is not None:
            row.title = title[:TITLE_LIMIT]
        if yolo is not None:
            row.yolo = bool(yolo)
        if last_route is not None:
            row.last_route = last_route[:512]
        row.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
        return row


async def save_state(
    row_id: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    compaction: dict[str, Any] | None = None,
    pending: dict[str, Any] | None = None,
    session_allows: list[str] | set[str] | None = None,
    usage_delta: dict[str, int] | None = None,
    title: str | None = None,
    last_route: str | None = None,
) -> None:
    """Commit turn state. Called before every SSE event that reports progress.

    `pending` is written whenever the key is supplied, including as None, which
    is how a resolved confirmation is cleared. Usage is accumulated, not
    replaced, because a turn makes several calls.
    """
    async with async_session() as db:
        result = await db.execute(
            select(AssistantConversation).where(AssistantConversation.id == row_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            logger.warning("Assistant conversation %s vanished mid-turn", row_id)
            return

        if messages is not None:
            row.messages_json = json.dumps(messages, default=str)
        if compaction is not None:
            row.compaction_json = json.dumps(compaction, default=str)
        row.pending_json = json.dumps(pending, default=str) if pending else ""
        if session_allows is not None:
            row.session_allows_json = json.dumps(sorted(session_allows))
        if title is not None:
            row.title = title[:TITLE_LIMIT]
        if last_route is not None:
            row.last_route = last_route[:512]
        if usage_delta:
            row.prompt_tokens += int(usage_delta.get("prompt_tokens", 0) or 0)
            row.completion_tokens += int(usage_delta.get("completion_tokens", 0) or 0)
            row.llm_calls += int(usage_delta.get("llm_calls", 0) or 0)
            row.tool_calls += int(usage_delta.get("tool_calls", 0) or 0)
        row.updated_at = datetime.now(UTC)
        await db.commit()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _filename_stem(row: AssistantConversation) -> str:
    raw = (row.title or "assistant").strip().lower()
    safe = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-") or "assistant"
    return f"gitv-assistant-{safe[:48]}"


def _fenced(payload: Any, limit: int) -> str:
    try:
        text = json.dumps(payload, indent=2, default=str)
    except Exception:
        text = str(payload)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    return "```json\n" + text + "\n```"


def _markdown(row: AssistantConversation) -> str:
    messages = loads(row.messages_json, [])
    compaction = loads(row.compaction_json, {})

    lines: list[str] = [f"# {row.title or 'Assistant conversation'}", ""]
    lines.append(f"- Conversation: `{row.id}`")
    lines.append(f"- Created: {row.created_at.isoformat() if row.created_at else ''}")
    lines.append(f"- Updated: {row.updated_at.isoformat() if row.updated_at else ''}")
    lines.append(
        f"- Usage: {row.llm_calls} LLM calls, {row.tool_calls} tool calls, "
        f"{row.prompt_tokens} prompt / {row.completion_tokens} completion tokens"
    )
    lines.append("")

    summary = (compaction or {}).get("summary")
    if summary:
        lines.append("## Compacted context")
        lines.append("")
        lines.append(str(summary))
        lines.append("")

    lines.append("## Transcript")
    lines.append("")
    for message in messages:
        role = str(message.get("role") or "?")
        heading = role if not message.get("name") else f"{role} ({message['name']})"
        lines.append(f"### {heading}")
        lines.append("")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            if role == "tool":
                lines.append(_fenced(loads(content, content), EXPORT_TOOL_LIMIT))
            else:
                lines.append(content)
            lines.append("")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            lines.append(f"**Tool call:** `{function.get('name', '?')}`")
            lines.append("")
            lines.append(_fenced(loads(function.get("arguments"), function.get("arguments")), EXPORT_TOOL_LIMIT))
            lines.append("")

    return "\n".join(lines)


async def export(user_id: str, conversation_id: str, fmt: str) -> tuple[str, str, str] | None:
    """Render a conversation. Returns (content, filename, media_type)."""
    row = await get(user_id, conversation_id)
    if row is None:
        return None

    stem = _filename_stem(row)
    if (fmt or "json").lower() == "markdown":
        return _markdown(row), f"{stem}.md", MARKDOWN_MEDIA

    payload = {
        "export_version": 1,
        "kind": "assistant_conversation",
        "conversation": to_dict(row),
    }
    return json.dumps(payload, indent=2, default=str), f"{stem}.json", JSON_MEDIA


async def fork_from_summary(
    user_id: str, conversation_id: str, summary_text: str
) -> tuple[AssistantConversation, str | None] | None:
    """New conversation seeded with a summary of an existing one.

    The original is left in place. This is a convenience, not a requirement --
    automatic compaction already lets a conversation run indefinitely.
    """
    source = await get(user_id, conversation_id)
    if source is None:
        return None

    from app.services.admin import get_admin_settings

    admin = await get_admin_settings()
    row, rotated = await create(user_id, admin.max_assistant_conversations)

    seed = [
        {
            "role": "system",
            "content": (
                "Continuing from an earlier conversation. Summary of what has "
                "happened so far:\n\n" + (summary_text or "").strip()
            ),
        }
    ]
    title = (source.title or "Assistant conversation")[: TITLE_LIMIT - 12]
    await save_state(row.id, messages=seed, title=f"{title} (continued)")
    refreshed = await get(user_id, row.id)
    return (refreshed or row), rotated


# The spec names this `list`; the module-level alias keeps that call site while
# the definition above stays readable next to `list[...]` annotations.
list = list_conversations  # noqa: A001
