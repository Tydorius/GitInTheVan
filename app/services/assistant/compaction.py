"""Keeping a conversation under its context budget without ever refusing one.

Two steps, cheapest first. Tool results older than the last few messages are
elided in the *outbound* copy only -- they are the bulk of an assistant
conversation and dropping them is free. If that is still not enough, everything
older is folded into a rolling summary through the user's own endpoint.

The stored transcript is never rewritten by the outbound path, so the pane can
still show what happened; `trim_stored` separately shrinks tool bodies that are
already behind the summary point, which is what stops storage growing without
bound.

Nothing here may raise on the hot path. A failed summary call logs, leaves
`compaction_json` untouched and lets the turn proceed with elision only -- but
the loop tells the user it happened rather than silently sending a shorter
context.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.budget import estimate_messages_tokens

logger = logging.getLogger(__name__)

DEFAULT_KEEP_LAST = 8

# Compact when the estimate crosses this share of the working context. Leaves
# room for the system prompt, the tool schemas and the model's own reply.
COMPACT_THRESHOLD = 0.8

# What a stored tool body is trimmed to once it is behind the summary point.
STORED_TOOL_LIMIT = 2048

SUMMARY_PREFIX = (
    "Earlier in this conversation (summarised, not verbatim):\n\n"
)

SUMMARY_SYSTEM_PROMPT = (
    "You are compacting a configuration-support conversation so it can continue "
    "within a smaller context. Produce a bulleted summary that preserves, in "
    "this order of priority: decisions the user made; the ids and names of every "
    "object touched or discussed; tasks still open; and the user's stated goals. "
    "Drop pleasantries, tool mechanics and anything already superseded. Do not "
    "invent detail that is not in the transcript. Keep it under 600 words."
)


@dataclass
class Outbound:
    """What to send upstream this turn, and whether a summary is now due."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    needs_summary: bool = False
    fold_through: int = 0


def _elided_note(message: dict[str, Any]) -> str:
    name = message.get("name") or "tool"
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, default=str)
    kb = max(1, round(len(content) / 1024)) if content else 0
    return f"[tool result elided: {name}, {kb} KB]"


def elide_tool_results(
    messages: list[dict[str, Any]], keep_last: int = DEFAULT_KEEP_LAST
) -> list[dict[str, Any]]:
    """Replace old tool-result bodies with a one-line placeholder.

    Returns a new list; the input is not mutated, because the stored transcript
    and the outbound copy are the same objects until this point.
    """
    if not messages:
        return []
    cutoff = max(0, len(messages) - max(0, keep_last))
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index < cutoff and message.get("role") == "tool":
            trimmed = dict(message)
            trimmed["content"] = _elided_note(message)
            out.append(trimmed)
        else:
            out.append(message)
    return out


def plan(
    messages: list[dict[str, Any]],
    compaction: dict[str, Any] | None,
    budget_tokens: int,
    keep_last: int = DEFAULT_KEEP_LAST,
) -> Outbound:
    """Decide what goes upstream this turn.

    `messages` is the stored transcript without the system prompt. The returned
    `messages` already carry any existing summary; `needs_summary` says whether
    elision alone was not enough.
    """
    base = outbound_with_summary(messages, compaction, keep_last)
    budget = max(1, int(budget_tokens or 0))
    if estimate_messages_tokens(base) <= budget * COMPACT_THRESHOLD:
        return Outbound(messages=base, needs_summary=False, fold_through=0)

    fold_through = max(0, len(messages) - max(0, keep_last))
    return Outbound(messages=base, needs_summary=fold_through > 0, fold_through=fold_through)


def outbound_with_summary(
    messages: list[dict[str, Any]],
    compaction: dict[str, Any] | None,
    keep_last: int = DEFAULT_KEEP_LAST,
) -> list[dict[str, Any]]:
    """The outbound list: [summary note] + the tail, with old results elided."""
    summary = str((compaction or {}).get("summary") or "")
    through = int((compaction or {}).get("through_index") or 0)

    if not summary:
        return elide_tool_results(messages, keep_last)

    through = max(0, min(through, len(messages)))
    tail = elide_tool_results(messages[through:], keep_last)
    return [{"role": "system", "content": SUMMARY_PREFIX + summary}, *tail]


def apply(
    compaction: dict[str, Any] | None, summary: str, through_index: int
) -> dict[str, Any]:
    """Roll the summary forward. Returns the new compaction block."""
    out = dict(compaction or {})
    out["summary"] = summary
    out["through_index"] = int(through_index)
    out["revisions"] = int(out.get("revisions") or 0) + 1
    return out


def trim_stored(
    messages: list[dict[str, Any]], through_index: int
) -> list[dict[str, Any]]:
    """Shrink stored tool bodies behind the summary point.

    They have been summarised and the pane shows old results collapsed, so the
    full body is no longer worth its storage. Text messages are left alone --
    they are what the user reads back.
    """
    limit = max(0, min(int(through_index or 0), len(messages)))
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if (
            index < limit
            and message.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > STORED_TOOL_LIMIT
        ):
            trimmed = dict(message)
            trimmed["content"] = (
                content[:STORED_TOOL_LIMIT]
                + "\n[trimmed: this result is behind the compaction point]"
            )
            out.append(trimmed)
        else:
            out.append(message)
    return out


def _fold_transcript(messages: list[dict[str, Any]]) -> str:
    """Render the messages being folded as plain text for the summariser."""
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "?")
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        calls = message.get("tool_calls")
        if calls:
            names = ", ".join(
                str((c.get("function") or {}).get("name") or "?") for c in calls
            )
            content = f"{content or ''}\n(called: {names})".strip()
        if message.get("name"):
            role = f"{role}:{message['name']}"
        lines.append(f"{role}: {content[:4000]}")
    return "\n\n".join(lines)


async def summarize(
    candidate: Any,
    model: str,
    user_layer: list[Any],
    messages_to_fold: list[dict[str, Any]],
    prior_summary: str = "",
    *,
    timeout: int = 120,
) -> str | None:
    """Fold old messages into a rolling summary. None means it did not work.

    One upstream call, no tools. The caller counts its usage and, when this
    returns None, must tell the user the context was only elided.
    """
    from app.services.assistant.llm import call_chat

    if not messages_to_fold:
        return prior_summary or None

    user_content = _fold_transcript(messages_to_fold)
    if prior_summary:
        user_content = (
            "Summary of everything before this point:\n"
            f"{prior_summary}\n\n"
            "New messages to fold into it:\n"
            f"{user_content}"
        )

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        data, status, _ = await call_chat(
            candidate,
            model,
            messages,
            tools=None,
            user_layer=user_layer,
            timeout=timeout,
        )
    except Exception:
        logger.exception("Assistant compaction summary call raised")
        return None

    if status != 200 or not isinstance(data, dict):
        logger.warning("Assistant compaction summary returned status %s", status)
        return None

    try:
        content = data["choices"][0]["message"].get("content") or ""
    except Exception:
        logger.warning("Assistant compaction summary response had no content")
        return None

    content = content.strip()
    return content or None
