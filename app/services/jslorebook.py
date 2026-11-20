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
    """Decode HTML entities from upstream platforms and restore escaped newlines/tabs
    only outside JavaScript string literals so string escapes like ``"a\\nb"`` stay valid.
    Tracks ``//`` line comments and ``/* ... */`` block comments so apostrophes inside
    comments (e.g. ``// don't``) do not flip into single-quote string mode.
    """
    decoded = html.unescape(raw)
    out: list[str] = []
    in_quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(decoded)
    while i < n:
        ch = decoded[i]
        if in_line_comment:
            if ch == "\\" and i + 1 < n and decoded[i + 1] in ("n", "t"):
                nl = "\n" if decoded[i + 1] == "n" else "\t"
                out.append(nl)
                if nl == "\n":
                    in_line_comment = False
                i += 2
                continue
            out.append(ch)
            if ch in ("\n", "\r"):
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and i + 1 < n and decoded[i + 1] == "/":
                out.append("*/")
                in_block_comment = False
                i += 2
                continue
            if ch == "\\" and i + 1 < n and decoded[i + 1] in ("n", "t"):
                out.append("\n" if decoded[i + 1] == "n" else "\t")
                i += 2
                continue
            out.append(ch)
            i += 1
            continue
        if in_quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(decoded[i + 1])
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch == "/" and i + 1 < n and decoded[i + 1] == "/":
            in_line_comment = True
            out.append("//")
            i += 2
            continue
        if ch == "/" and i + 1 < n and decoded[i + 1] == "*":
            in_block_comment = True
            out.append("/*")
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n and decoded[i + 1] in ("n", "t"):
            out.append("\n" if decoded[i + 1] == "n" else "\t")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


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
