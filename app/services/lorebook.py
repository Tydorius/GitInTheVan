from __future__ import annotations

import json
import re
from dataclasses import dataclass

INJECTION_POSITIONS = ("system_start", "before_last_message")


@dataclass
class MatchedEntry:
    content: str
    position: str
    insertion_order: int
    name: str


def parse_json_list(raw: str) -> list[str]:
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(item).strip() for item in result if str(item).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def match_entries(
    messages: list[dict],
    entries: list[dict],
    total_budget: int = 0,
) -> list[MatchedEntry]:
    conversation_text = _extract_conversation_text(messages)
    matched: list[MatchedEntry] = []
    used_chars = 0

    for entry in entries:
        if entry.get("is_disabled"):
            continue

        if entry.get("is_constant"):
            matched.append(_to_matched_entry(entry))
            used_chars += len(matched[-1].content)
            continue

        primary_keys = parse_json_list(entry.get("keys", "[]"))
        if not primary_keys:
            continue

        primary_matched = any(
            _keyword_matches(key, conversation_text) for key in primary_keys
        )

        if not primary_matched:
            continue

        if entry.get("is_selective"):
            secondary_keys = parse_json_list(entry.get("secondary_keys", "[]"))
            if secondary_keys:
                secondary_matched = any(
                    _keyword_matches(key, conversation_text) for key in secondary_keys
                )
                if not secondary_matched:
                    continue

        matched.append(_to_matched_entry(entry))
        used_chars += len(matched[-1].content)

        if total_budget > 0 and used_chars >= total_budget:
            break

    matched.sort(key=lambda m: m.insertion_order)
    return matched


def inject_entries(
    messages: list[dict],
    matched: list[MatchedEntry],
) -> list[dict]:
    if not matched:
        return messages

    result = [msg.copy() for msg in messages]

    system_entries = [m for m in matched if m.position == "system_start"]
    message_entries = [m for m in matched if m.position == "before_last_message"]
    other_entries = [m for m in matched if m.position not in INJECTION_POSITIONS]

    if system_entries:
        combined = "\n\n".join(m.content for m in system_entries)
        existing_system = None
        for i, msg in enumerate(result):
            if msg.get("role") == "system":
                existing_system = i
                break

        if existing_system is not None:
            result[existing_system] = {
                **result[existing_system],
                "content": f"{result[existing_system]['content']}\n\n{combined}",
            }
        else:
            result.insert(0, {"role": "system", "content": combined})

    if message_entries and result:
        combined = "\n\n".join(m.content for m in message_entries)
        last_user_idx = None
        for i in range(len(result) - 1, -1, -1):
            if result[i].get("role") == "user":
                last_user_idx = i
                break

        if last_user_idx is not None:
            result.insert(last_user_idx, {"role": "system", "content": combined})
        else:
            result.insert(len(result) - 1, {"role": "system", "content": combined})

    if other_entries:
        combined = "\n\n".join(m.content for m in other_entries)
        result.insert(len(result) - 1, {"role": "system", "content": combined})

    return result


def _keyword_matches(keyword: str, text: str) -> bool:
    keyword_lower = keyword.lower()
    text_lower = text.lower()
    if keyword_lower in text_lower:
        return True
    pattern = r"\b" + re.escape(keyword_lower) + r"\b"
    return bool(re.search(pattern, text_lower))


def _extract_conversation_text(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        parts.append(text)
    return " ".join(parts)


def _to_matched_entry(entry: dict) -> MatchedEntry:
    return MatchedEntry(
        content=entry.get("content", ""),
        position=entry.get("position", "before_last_message"),
        insertion_order=entry.get("insertion_order", 10),
        name=entry.get("name", ""),
    )
