"""Request-side macro expansion for ``{{user}}`` and ``{{char}}`` tokens.

Expands standard roleplay macros in outgoing ``messages`` right before upstream
forwarding (after lorebooks, cantrips, and skills have injected). Never mutates
stored cantrips, ``chat_data``, ``user_data``, or upstream responses.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.cantrip_context import parse_character_sections
from app.services.debug import debug_capture

_MACRO_RE = re.compile(r"\{\{(user|char)\}\}", re.IGNORECASE)
_JSLOREBOOK_RE = re.compile(r"<jslorebook\b[^>]*>.*?</jslorebook>", re.DOTALL | re.IGNORECASE)
_USER_PERSONA_RE = re.compile(r"<UserPersona>(?P<body>.*?)</UserPersona>", re.DOTALL | re.IGNORECASE)
_USER_LINE_RE = re.compile(r"^\s*User\s*:\s*([^\n<>{}]{1,80})", re.MULTILINE | re.IGNORECASE)
# ``Name:`` is only trusted inside <UserPersona>: card-style prompts use a
# bare ``Name:`` line for the *character*, so resolving the user from it
# would silently swap the two names.
_USER_PERSONA_LINE_RE = re.compile(r"^\s*(?:User|Name)\s*:\s*([^\n<>{}]{1,80})", re.MULTILINE | re.IGNORECASE)
# Never match bare ``Persona:`` as a character name — non-JanitorAI cards
# commonly write trait lines like ``Persona: shy, observant``.
_CHAR_LINE_RE = re.compile(r"^\s*Character(?:\s+Name)?\s*:\s*([^\n<>{}]{1,80})", re.MULTILINE | re.IGNORECASE)


def _first_system_text(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
    return ""


def resolve_macro_names(
    messages: list[dict[str, Any]],
    request_headers: dict[str, str] | None = None,
    body_json: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve ``(user_name, char_name)`` for request-side macro expansion.

    Precedence:
    1. Explicit headers (``X-User-Name``, ``X-Char-Name`` / ``X-Character-Name``).
    2. Character name from JanitorAI ``<{NAME}'s Persona>`` sections via
       ``cantrip_context.parse_character_sections``, or a ``Character:`` /
       ``Character Name:`` line in the system prompt.
    3. User name from ``<UserPersona>`` (``User:`` / ``Name:`` inside it) or
       a ``User:`` line in the system prompt.

    Both ``user_name`` and ``char_name`` stay ``""`` when genuinely unknown so
    ``{{user}}`` and ``{{char}}`` are left intact rather than rewritten to a
    placeholder like ``"User"``.
    """
    headers_lower = {
        k.lower(): v
        for k, v in (request_headers or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }

    user_name = headers_lower.get("x-user-name", "").strip()
    char_name = (
        headers_lower.get("x-char-name")
        or headers_lower.get("x-character-name")
        or ""
    ).strip()

    system_text = _first_system_text(messages)

    if not char_name:
        sections = parse_character_sections(messages)
        if sections and sections.character_name:
            char_name = sections.character_name.strip()
        elif system_text:
            char_match = _CHAR_LINE_RE.search(system_text)
            if char_match:
                char_name = char_match.group(1).strip()

    if not user_name and system_text:
        up_match = _USER_PERSONA_RE.search(system_text)
        if up_match:
            user_match = _USER_PERSONA_LINE_RE.search(up_match.group("body"))
        else:
            user_match = _USER_LINE_RE.search(system_text)
        if user_match:
            user_name = user_match.group(1).strip()

    return user_name, char_name


def _expand_plain(text: str, user_name: str, char_name: str) -> tuple[str, int]:
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        token = match.group(1).lower()
        if token == "user" and user_name:
            count += 1
            return user_name
        if token == "char" and char_name:
            count += 1
            return char_name
        return match.group(0)

    return _MACRO_RE.sub(_replace, text), count


def expand_macros(
    text: str,
    *,
    user_name: str = "",
    char_name: str = "",
) -> tuple[str, int]:
    """Expand ``{{user}}`` and ``{{char}}`` outside ``<jslorebook>`` blocks.

    Returns ``(expanded_text, replacement_count)``.
    """
    if not text or (not user_name and not char_name):
        return text, 0

    if "<jslorebook" not in text.lower():
        return _expand_plain(text, user_name, char_name)

    parts: list[str] = []
    last_end = 0
    total = 0
    for block in _JSLOREBOOK_RE.finditer(text):
        if block.start() > last_end:
            expanded, n = _expand_plain(text[last_end:block.start()], user_name, char_name)
            parts.append(expanded)
            total += n
        parts.append(block.group(0))
        last_end = block.end()

    if last_end < len(text):
        expanded, n = _expand_plain(text[last_end:], user_name, char_name)
        parts.append(expanded)
        total += n

    return "".join(parts), total


def expand_macros_in_messages(
    body_json: dict[str, Any],
    request_headers: dict[str, str] | None = None,
) -> int:
    """Expand ``{{user}}`` and ``{{char}}`` across ``body_json['messages']``.

    Handles both string and multimodal list-of-parts ``content``. Emits a
    ``macro_expansion`` debug capture when at least one replacement occurs.
    """
    messages = body_json.get("messages")
    if not isinstance(messages, list) or not messages:
        return 0

    user_name, char_name = resolve_macro_names(
        messages, request_headers=request_headers, body_json=body_json
    )
    if not user_name and not char_name:
        return 0

    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new_content, n = expand_macros(content, user_name=user_name, char_name=char_name)
            if n > 0:
                msg["content"] = new_content
                total += n
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    new_text, n = expand_macros(part["text"], user_name=user_name, char_name=char_name)
                    if n > 0:
                        part["text"] = new_text
                        total += n

    if total > 0:
        debug_capture(
            body_json,
            "macro_expansion",
            "Macro Expansion",
            detail=f"Expanded {total} macro(s) (user={user_name!r}, char={char_name!r})",
            metadata={"count": total, "user_name": user_name, "char_name": char_name},
        )

    return total
