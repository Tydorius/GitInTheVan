from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

JSLOREBOOK_PATTERN = re.compile(
    r"<jslorebook>(.*?)</jslorebook>",
    re.DOTALL | re.IGNORECASE,
)

JSLOREBOOK_STRIP_PATTERN = re.compile(
    r"<jslorebook>.*?</jslorebook>",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class EmbeddedScript:
    code: str
    position: str


def _desanitize_content(raw: str) -> str:
    """Decode HTML entities and common escaping from upstream platforms."""
    decoded = html.unescape(raw)
    decoded = decoded.replace("&amp;", "&")
    decoded = decoded.replace("&lt;", "<").replace("&gt;", ">")
    decoded = decoded.replace("&quot;", '"').replace("&#39;", "'")
    decoded = decoded.replace("\\n", "\n").replace("\\t", "\t")
    return decoded.strip()


def extract_jslorebook_scripts(text: str) -> list[EmbeddedScript]:
    """Extract JavaScript code blocks from <jslorebook> tags.

    Handles HTML-escaped content from upstream platforms (JanitorAI,
    SillyTavern, etc). Returns list of EmbeddedScript objects.
    """
    if not text:
        return []

    scripts: list[EmbeddedScript] = []

    for match in JSLOREBOOK_PATTERN.finditer(text):
        raw_content = match.group(1)
        code = _desanitize_content(raw_content)

        if not code.strip():
            continue

        position = _detect_position(code)
        scripts.append(EmbeddedScript(code=code, position=position))

    return scripts


def strip_jslorebook_tags(text: str) -> str:
    """Remove <jslorebook> blocks from text."""
    if not text:
        return text
    cleaned = JSLOREBOOK_STRIP_PATTERN.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _detect_position(code: str) -> str:
    """Infer the pipeline position from code comments."""
    lower = code.lower()
    if "<post-llm>" in lower or "post-llm" in lower:
        return "post_navigator"
    if "<post-user>" in lower or "post-user" in lower:
        return "pre_navigator"
    return "pre_driver"


def extract_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[EmbeddedScript], list[dict[str, Any]]]:
    """Scan all messages for <jslorebook> tags.

    Returns (scripts, cleaned_messages) where cleaned_messages have
    the jslorebook blocks removed.
    """
    all_scripts: list[EmbeddedScript] = []

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            scripts = extract_jslorebook_scripts(content)
            all_scripts.extend(scripts)

    cleaned_messages = []
    for msg in messages:
        new_msg = msg.copy()
        content = msg.get("content", "")
        if isinstance(content, str):
            new_msg["content"] = strip_jslorebook_tags(content)
        cleaned_messages.append(new_msg)

    return all_scripts, cleaned_messages
