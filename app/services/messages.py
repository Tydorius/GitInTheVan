from __future__ import annotations

from collections.abc import Callable
from typing import Any


def extract_text(content: Any) -> str:
    """Extract plain text from string, multimodal content-parts list, or other values."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
        return " ".join(parts)
    return str(content) if content else ""


def inject_into_system_message(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    """Append ``block`` to the first system message, or prepend a new system message.

    Multimodal-safe: when the existing system message uses a content-parts list,
    ``block`` is appended as a text part rather than concatenated as a string.
    Returns a new list without mutating the input message dicts.
    """
    if not block:
        return messages

    result = [msg.copy() for msg in messages]
    for i, msg in enumerate(result):
        if msg.get("role") == "system":
            existing = msg.get("content")
            if isinstance(existing, list):
                result[i] = {
                    **msg,
                    "content": [*existing, {"type": "text", "text": f"\n\n{block}"}],
                }
            else:
                new_content = f"{existing}\n\n{block}" if existing else block
                result[i] = {**msg, "content": new_content}
            return result

    result.insert(0, {"role": "system", "content": block})
    return result


def strip_from_messages(
    messages: list[dict[str, Any]], strip_fn: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Apply ``strip_fn`` to message text, including content-parts lists.

    Parts without a string ``text`` field (e.g. images) pass through untouched.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        new_msg = msg.copy()
        content = msg.get("content", "")
        if isinstance(content, str):
            new_msg["content"] = strip_fn(content)
        elif isinstance(content, list):
            new_parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    new_parts.append({**item, "text": strip_fn(item["text"])})
                else:
                    new_parts.append(item)
            new_msg["content"] = new_parts
        result.append(new_msg)
    return result
