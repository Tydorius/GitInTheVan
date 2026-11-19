"""Response tags (Package 3, item 3) and registered strip patterns (item 22).

The `_extract_response_memories` template generalized: a cantrip registers
tag names (``Cantrip.response_tags``); GitV extracts those tags from the
model's final reply, strips them before the client, and seeds the
extractions into the post-Navigator cantrip context the same turn — the
one-turn wait that forced the World Ledger's hidden-link format is gone.

Ownership drives both extraction and stripping: a tag nobody registered is
not ours to touch, so an arbitrary `<deed>` in a reply passes through
untouched. The registry is every tag name the user's cantrips declare,
regardless of activation — registration is data about the cantrip, not a
pipeline participant (a disabled ledger still wants its tags out of the
visible reply; per-user name uniqueness keeps ownership unambiguous).

The user's strip patterns (item 22) run at the same point with no capture
and no delivery — pure cleanup of the final reply. Request-side stripping
(the designated home for the JanitorAI client-note fix once real examples
arrive) is deliberately not built; the service is shaped so that variant
is an additive direction flag, not a rewrite.

Both extractors operate on string content only, matching the memstore
precident's behavior on content-parts lists (the same latent gap; nothing
regresses).
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import select

from app.database import async_session
from app.models.cantrip import Cantrip
from app.services.debug import debug_capture_response

logger = logging.getLogger(__name__)

# Names that would collide with tags GitV itself parses. A response tag is
# extracted from replies; these names belong to request-side machinery, but
# refusing them avoids a script ever seeing its own plumbing in
# context.response_tags after a client echoes a tag back.
RESERVED_TAG_NAMES = frozenset({
    "memstore", "jslorebook", "verify", "map", "lexicon", "cantrip",
})

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

MAX_STRIP_PATTERNS = 20
MAX_STRIP_PATTERN_LEN = 256


def normalize_tag_name(raw: str) -> str | None:
    """Lowercase/trim a tag name; None when it is not a legal name."""
    name = str(raw or "").strip().lower()
    if not _NAME_RE.match(name) or name in RESERVED_TAG_NAMES:
        return None
    return name


def parse_tag_names(raw: str | None) -> list[str]:
    """JSON list of tag names; NULL (MySQL TEXT) and junk degrade to []."""
    try:
        result = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(result, list):
        return []
    names: list[str] = []
    for item in result:
        if not isinstance(item, str):
            continue
        name = normalize_tag_name(item)
        if name and name not in names:
            names.append(name)
    return names


def _paired_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"<{name}>\s*([\s\S]*?)\s*</{name}>", re.IGNORECASE)


def _selfclosing_re(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"<{name}\s+value=[\"']([^\"']*)[\"']\s*/>", re.IGNORECASE
    )


def extract_tags(content: str, names: list[str]) -> dict[str, list[str]]:
    """Pull every registered tag's payloads out of ``content``.

    Both grammars mirror memstore: paired ``<name>…</name>`` blocks and
    self-closing ``<name value="…"/>`` forms, matched case-insensitively.
    """
    found: dict[str, list[str]] = {}
    if not isinstance(content, str) or not content:
        return found
    for name in names:
        payloads: list[str] = []
        for m in _paired_re(name).finditer(content):
            text = m.group(1).strip()
            if text:
                payloads.append(text)
        for m in _selfclosing_re(name).finditer(content):
            text = m.group(1).strip()
            if text:
                payloads.append(text)
        if payloads:
            found[name] = payloads
    return found


def strip_tags(content: str, names: list[str]) -> str:
    """Remove registered tags and tidy the blank space they leave behind."""
    if not isinstance(content, str) or not content:
        return content
    stripped = content
    for name in names:
        stripped = _paired_re(name).sub("", stripped)
        stripped = _selfclosing_re(name).sub("", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def parse_strip_patterns(raw: str | None) -> list[str]:
    """Stored JSON -> the raw pattern list (settings GET). NULL degrades to
    []."""
    try:
        items = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(i) for i in items if isinstance(i, str)] if isinstance(items, list) else []


def validate_strip_patterns(patterns: list[str]) -> list[str]:
    """Write-path validation (422 fodder): each pattern must compile and
    fit the caps. Raises ValueError with a user-readable reason. Patterns
    are deliberately NOT run through sanitize_and_log -- they are code-like
    (regex), and control-char stripping could corrupt a legitimate pattern
    (the same exemption cantrip code gets under rule 18)."""
    if len(patterns) > MAX_STRIP_PATTERNS:
        raise ValueError(f"at most {MAX_STRIP_PATTERNS} strip patterns")
    cleaned: list[str] = []
    for raw in patterns:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        if len(pattern) > MAX_STRIP_PATTERN_LEN:
            raise ValueError(
                f"strip pattern longer than {MAX_STRIP_PATTERN_LEN} characters"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"strip pattern does not compile: {pattern!r} ({exc})") from exc
        if pattern not in cleaned:
            cleaned.append(pattern)
    return cleaned


def compile_strip_patterns(raw: str | None) -> list[re.Pattern[str]]:
    """The user's item-22 patterns; each compiles independently and a bad
    one is skipped (the setting is validated on write, so this only guards
    against state written by older/other means). Never raises."""
    try:
        items = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    patterns: list[re.Pattern[str]] = []
    for item in items[:MAX_STRIP_PATTERNS]:
        if not isinstance(item, str) or not item or len(item) > MAX_STRIP_PATTERN_LEN:
            continue
        try:
            patterns.append(re.compile(item))
        except re.error:
            continue
    return patterns


def apply_strip_patterns(content: str, patterns: list[re.Pattern[str]]) -> str:
    if not isinstance(content, str) or not content or not patterns:
        return content
    stripped = content
    for pattern in patterns:
        stripped = pattern.sub("", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


async def registered_tag_names(user_id: str) -> list[str]:
    """Union of every tag name the user's cantrips register."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Cantrip.response_tags).where(Cantrip.user_id == user_id)
            )
        ).scalars().all()
    names: list[str] = []
    for row in rows:
        for name in parse_tag_names(row):
            if name not in names:
                names.append(name)
    return names


async def process_response_capture(
    response_data: dict, user_id: str, body_json: dict
) -> dict:
    """The four-path hook: extract registered tags from the final reply,
    strip them plus the user's strip patterns, and seed the extractions for
    the post-Navigator cantrip context. Runs after the verification loop
    (rejected drafts are never seen) and before the post-Navigator pass.

    Never raises — a failure here degrades to an untouched reply, exactly
    like ``_extract_response_memories``.
    """
    try:
        choices = response_data.get("choices", []) if isinstance(response_data, dict) else []
        if not choices:
            return response_data
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not isinstance(content, str) or not content:
            return response_data

        names = await registered_tag_names(user_id)
        found = extract_tags(content, names)

        from app.models.user_settings import UserSettings
        async with async_session() as db:
            settings = (
                await db.execute(
                    select(UserSettings.response_strip_patterns).where(
                        UserSettings.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
        patterns = compile_strip_patterns(settings or "[]")

        if not found and not patterns:
            return response_data

        stripped = strip_tags(content, names) if found else content
        stripped = apply_strip_patterns(stripped, patterns)

        if found:
            body_json["_gitv_response_tags"] = found
            from app.services.metrics import record_prompt_object
            for name, payloads in found.items():
                record_prompt_object(
                    body_json, "response_tag", name,
                    0, max(1, sum(len(p) for p in payloads) // 4),
                )

        if stripped != content:
            message["content"] = stripped

        if body_json.get("_gitv_debug") is not None:
            debug_capture_response(
                body_json, "response_tags", "Response Tags",
                content_before=content[:500] if found or patterns else "",
                content_after=stripped[:500] if found or patterns else "",
                detail=(
                    f"{sum(len(v) for v in found.values())} extraction(s) "
                    f"across {len(found)} tag(s)"
                    + (f", {len(patterns)} strip pattern(s) applied" if patterns else "")
                    if found or patterns
                    else "no registered tags matched"
                ),
                metadata={
                    "tags": {
                        name: {"count": len(v), "chars": sum(len(p) for p in v)}
                        for name, v in found.items()
                    },
                    "strip_patterns": len(patterns),
                    "stripped_chars": len(content) - len(stripped),
                },
            )
        return response_data
    except Exception:
        logger.exception("Response-tag capture failed; reply forwarded untouched")
        return response_data
