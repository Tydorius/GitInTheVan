from __future__ import annotations

import json
from typing import Any

from app.services.budget import estimate_messages_tokens

_KNOWN_SYSTEM_MUTATOR_STAGES = (
    "memory_injection",
    "lorebook",
    "cantrip",
    "summarization",
    "scenario_summarization",
)


def _parse_messages(raw: Any) -> list[dict[str, Any]] | None:
    """Parse a captured message list from either a JSON string or a list of dicts."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except Exception:
            return None
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict)]
    return None


def _extract_final_messages(pipeline: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract the final forwarded message list from a stored debug pipeline_data dict."""
    if not isinstance(pipeline, dict):
        return []

    stages = pipeline.get("stages")
    if isinstance(stages, list):
        for stage in reversed(stages):
            if not isinstance(stage, dict):
                continue
            msgs = _parse_messages(stage.get("messages_after"))
            if msgs is None:
                msgs = _parse_messages(stage.get("messages"))
            if msgs is not None:
                return msgs

    for key in ("final_messages", "modified_messages", "original_messages"):
        msgs = _parse_messages(pipeline.get(key))
        if msgs is not None:
            return msgs

    return []


def _normalize_message_text(msg: dict[str, Any]) -> str:
    """Normalize a message's content (string or multimodal/block list) into plain text."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _common_prefix_len(a: str, b: str) -> int:
    """Return the exact character offset where strings ``a`` and ``b`` first diverge."""
    limit = min(len(a), len(b))
    for idx in range(limit):
        if a[idx] != b[idx]:
            return idx
    return limit


def _message_differs_at(
    before_msgs: list[dict[str, Any]] | None,
    after_msgs: list[dict[str, Any]],
    index: int,
) -> bool:
    """Return True if the message at ``index`` changed between ``before_msgs`` and ``after_msgs``."""
    before_msg = before_msgs[index] if (before_msgs is not None and index < len(before_msgs)) else None
    after_msg = after_msgs[index] if index < len(after_msgs) else None

    if before_msg is None and after_msg is None:
        return False
    if (before_msg is None) != (after_msg is None):
        return True

    assert before_msg is not None and after_msg is not None
    if str(before_msg.get("role", "")) != str(after_msg.get("role", "")):
        return True
    return _normalize_message_text(before_msg) != _normalize_message_text(after_msg)


def _find_culprit_stage(current_pipeline: dict[str, Any], diff_index: int) -> str:
    """Identify the first pipeline stage that mutated ``messages[diff_index]``."""
    stages = current_pipeline.get("stages")
    if not isinstance(stages, list):
        return ""

    prev_stage_msgs = _parse_messages(current_pipeline.get("original_messages"))
    dropped_candidates: list[str] = []

    for stage in stages:
        if not isinstance(stage, dict):
            continue

        after_msgs = _parse_messages(stage.get("messages_after"))
        if after_msgs is None:
            after_msgs = _parse_messages(stage.get("messages"))

        if after_msgs is None:
            if stage.get("messages_dropped") and diff_index == 0:
                stage_name = str(stage.get("name", "")).lower()
                if any(tok in stage_name for tok in _KNOWN_SYSTEM_MUTATOR_STAGES):
                    dropped_candidates.append(str(stage.get("label") or stage.get("name") or ""))
            continue

        before_msgs = _parse_messages(stage.get("messages_before"))
        if before_msgs is None:
            before_msgs = prev_stage_msgs

        if before_msgs is not None and _message_differs_at(before_msgs, after_msgs, diff_index):
            return str(stage.get("label") or stage.get("name") or "")

        prev_stage_msgs = after_msgs

    if dropped_candidates:
        return dropped_candidates[0]

    return ""


def analyze_prompt_cache(
    current_pipeline: dict[str, Any],
    previous_pipeline: dict[str, Any] | None,
    previous_exchange_id: str = "",
) -> dict[str, Any]:
    """Compare the final forwarded messages of two consecutive turns in a chat.

    Detects where the shared prompt prefix first diverged, estimates cache-hot
    prefix stability, and attributes early prefix breaks to the first pipeline
    stage that mutated the diverging message.
    """
    prev_msgs = _extract_final_messages(previous_pipeline)
    if previous_pipeline is None or not prev_msgs:
        return {
            "available": False,
            "reason": "No previous turn in this chat to compare against.",
        }

    curr_msgs = _extract_final_messages(current_pipeline)
    if not curr_msgs:
        return {
            "available": False,
            "reason": "Current turn has no captured messages to analyze.",
        }

    shared_prefix_chars = 0
    first_diff_message_index: int | None = None
    first_diff_role = ""

    min_len = min(len(prev_msgs), len(curr_msgs))
    for i in range(min_len):
        prev_role = str(prev_msgs[i].get("role", ""))
        curr_role = str(curr_msgs[i].get("role", ""))
        prev_text = _normalize_message_text(prev_msgs[i])
        curr_text = _normalize_message_text(curr_msgs[i])

        if prev_role != curr_role:
            first_diff_message_index = i
            first_diff_role = curr_role or prev_role
            break

        if prev_text == curr_text:
            shared_prefix_chars += len(curr_text)
        else:
            partial = _common_prefix_len(prev_text, curr_text)
            shared_prefix_chars += partial
            first_diff_message_index = i
            first_diff_role = curr_role
            break

    if first_diff_message_index is None and len(curr_msgs) != len(prev_msgs):
        first_diff_message_index = min_len
        if min_len < len(curr_msgs):
            first_diff_role = str(curr_msgs[min_len].get("role", ""))
        else:
            first_diff_role = str(prev_msgs[min_len].get("role", ""))

    total_prompt_tokens = estimate_messages_tokens(curr_msgs)
    if first_diff_message_index is None:
        shared_prefix_tokens = total_prompt_tokens
        prefix_stability_pct = 100.0
    else:
        shared_prefix_tokens = shared_prefix_chars // 4
        prefix_stability_pct = round(
            min(100.0, (shared_prefix_tokens / max(1, total_prompt_tokens)) * 100.0),
            1,
        )

    is_tail_only = (
        first_diff_message_index is None
        or first_diff_message_index >= len(curr_msgs) - 1
        or first_diff_message_index >= len(prev_msgs)
    )

    culprit_stage = ""
    if not is_tail_only and first_diff_message_index is not None:
        culprit_stage = _find_culprit_stage(current_pipeline, first_diff_message_index)

    if is_tail_only:
        summary = (
            f"Prefix stayed {prefix_stability_pct:.1f}% stable "
            "(all previous turns cache-eligible)."
        )
    elif culprit_stage:
        summary = (
            f"Prefix broke at messages[{first_diff_message_index}] "
            f"(~{shared_prefix_tokens} tokens in, {prefix_stability_pct:.1f}% stable) "
            f"during stage '{culprit_stage}'. Enable Cache-Friendly Mode in Settings "
            "to move dynamic injections to the tail."
        )
    else:
        summary = (
            f"Prefix broke at messages[{first_diff_message_index}] "
            f"(~{shared_prefix_tokens} tokens in, {prefix_stability_pct:.1f}% stable) "
            "before pipeline stages ran (client prompt changed)."
        )

    return {
        "available": True,
        "previous_exchange_id": previous_exchange_id,
        "shared_prefix_tokens": shared_prefix_tokens,
        "total_prompt_tokens": total_prompt_tokens,
        "prefix_stability_pct": prefix_stability_pct,
        "first_diff_message_index": first_diff_message_index,
        "first_diff_role": first_diff_role,
        "culprit_stage": culprit_stage,
        "is_tail_only": is_tail_only,
        "summary": summary,
    }
