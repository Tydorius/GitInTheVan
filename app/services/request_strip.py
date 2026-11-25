"""Request strip rules (0.52.0, N1).

User-configurable regexes applied to request message text. JanitorAI appends
its banned-words "SYSTEM NOTE" to the newest user message only, so the message
comes back without it on the next request and the conversation hash no longer
matches -- every turn resolves as a brand-new chat. Other platforms inject
similar per-request metadata, and some inject junk the user never wants the
model to see at all. One rule shape covers both:

- scope ``hash``    -- normalize conversation identity only. The forwarded
  request keeps the text: JanitorAI's note exists so the model avoids those
  words, and removing it would defeat the feature.
- scope ``forward`` -- strip from the outbound request body as well. Forward
  implies hash: text marked as junk should never be hashed either, and the
  implication keeps both hash sides symmetric for stored client history.

Symmetry is what makes hash rules work. The same compiled rule set is applied
inside ``compute_hash`` by ``resolve_conversation`` and ``record_post_hash``,
so normalization runs on both the recorded and the resolved side no matter
whether the client resent the text. An empty rule list is a no-op: the hash
is computed exactly as before 0.52.0.

Storage mirrors ``response_strip_patterns`` (rule objects instead of plain
strings, because of the scope) with the same caps and the same never-raises
read path.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.response_tags import (
    MAX_STRIP_PATTERN_LEN,
    MAX_STRIP_PATTERNS,
    apply_strip_patterns,
)

SCOPES = ("hash", "forward")

_EXAMPLE_JANITOR = {
    "pattern": (
        r"\n?SYSTEM NOTE: Do not include the following words/phrases "
        r"in your output under any circumstances:.*"
    ),
    "scope": "hash",
}


def janitor_example_rule() -> dict[str, str]:
    """The shipped example rule (Settings > Conversation Matching): JanitorAI's
    banned-words note as observed 2026-11-25 -- appended after one newline to
    the newest user message, no wrapper, variable word list to end of line."""
    return dict(_EXAMPLE_JANITOR)


def _normalize_rule(raw: Any) -> dict[str, str] | None:
    """One stored/API rule -> {"pattern", "scope"}; None when malformed."""
    if not isinstance(raw, dict):
        return None
    pattern = str(raw.get("pattern") or "").strip()
    scope = str(raw.get("scope") or "hash").strip().lower()
    if not pattern or scope not in SCOPES:
        return None
    return {"pattern": pattern, "scope": scope}


def parse_request_strip_rules(raw: str | None) -> list[dict[str, str]]:
    """Stored JSON -> rule list (settings GET). NULL (MySQL TEXT) and junk
    degrade to []."""
    try:
        items = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    rules: list[dict[str, str]] = []
    for item in items:
        rule = _normalize_rule(item)
        if rule and rule not in rules:
            rules.append(rule)
    return rules


def validate_request_strip_rules(rules: list[Any]) -> list[dict[str, str]]:
    """Write-path validation (422 fodder): each rule must be well-formed,
    compile, and fit the same caps as the response strip patterns. Raises
    ValueError with a user-readable reason. Rules are deliberately NOT run
    through sanitize_and_log -- they are code-like (regex), and control-char
    stripping could corrupt a legitimate pattern (the same exemption cantrip
    code and the response strip patterns get under rule 18)."""
    if len(rules) > MAX_STRIP_PATTERNS:
        raise ValueError(f"at most {MAX_STRIP_PATTERNS} strip rules")
    cleaned: list[dict[str, str]] = []
    for raw in rules:
        rule = _normalize_rule(raw)
        if rule is None:
            raise ValueError(
                "each strip rule needs a non-empty pattern and a scope of "
                "'hash' or 'forward'"
            )
        if len(rule["pattern"]) > MAX_STRIP_PATTERN_LEN:
            raise ValueError(
                f"strip rule longer than {MAX_STRIP_PATTERN_LEN} characters"
            )
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise ValueError(
                f"strip rule does not compile: {rule['pattern']!r} ({exc})"
            ) from exc
        if rule not in cleaned:
            cleaned.append(rule)
    return cleaned


def compile_request_strip_rules(raw: str | None) -> dict[str, list[re.Pattern[str]]]:
    """Stored JSON -> {"hash": [...], "forward": [...]} compiled patterns.

    The hash list carries hash-scope rules AND forward-scope rules (forward
    implies hash) -- it is the one list every hash site must use. A rule that
    no longer compiles is skipped; the setting is validated on write, so this
    only guards against state written by older or other means. Never raises.
    """
    hash_patterns: list[re.Pattern[str]] = []
    forward_patterns: list[re.Pattern[str]] = []
    for rule in parse_request_strip_rules(raw):
        try:
            pattern = re.compile(rule["pattern"])
        except re.error:
            continue
        if rule["scope"] == "forward":
            forward_patterns.append(pattern)
            hash_patterns.append(pattern)
        else:
            hash_patterns.append(pattern)
    return {"hash": hash_patterns, "forward": forward_patterns}


def normalize_for_hash(text: str, patterns: list[re.Pattern[str]] | None) -> str:
    """Apply the hash-scope rules to one message's extracted text.

    The same normalization as the response-side strip patterns (substitute,
    collapse the blank space left behind, trim edges) so a rule author meets
    one behavior. An empty pattern list returns the text unchanged -- the
    pre-0.52.0 hash exactly.
    """
    if not patterns:
        return text
    return apply_strip_patterns(text, patterns)


def strip_forward_messages(
    messages: list[dict[str, Any]] | None,
    patterns: list[re.Pattern[str]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Strip forward-scope rules from the outbound message list.

    Returns (new_messages, changed_indexes). String content is stripped
    whole; content-parts lists are stripped per text part. Never mutates the
    input dicts -- the same contract as inject_into_system_message and
    apply_history_ops, so ``_gitv_messages_for_hash`` (captured earlier) is
    untouched by construction.
    """
    if not patterns or not isinstance(messages, list):
        return messages or [], []
    new_messages: list[dict[str, Any]] = []
    changed: list[int] = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            new_content = apply_strip_patterns(content, patterns)
            if new_content != content:
                changed.append(index)
            new_msg = dict(msg)
            new_msg["content"] = new_content
            new_messages.append(new_msg)
        elif isinstance(content, list):
            new_content = []
            part_changed = False
            for part in content:
                if (
                    isinstance(part, dict)
                    and isinstance(part.get("text"), str)
                    and part["text"]
                ):
                    new_text = apply_strip_patterns(part["text"], patterns)
                    if new_text != part["text"]:
                        part_changed = True
                        new_part = dict(part)
                        new_part["text"] = new_text
                        part = new_part
                new_content.append(part)
            if part_changed:
                changed.append(index)
                new_msg = dict(msg)
                new_msg["content"] = new_content
                new_messages.append(new_msg)
            else:
                new_messages.append(msg)
        else:
            new_messages.append(msg)
    return new_messages, changed
