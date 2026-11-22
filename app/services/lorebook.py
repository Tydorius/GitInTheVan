from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.messages import extract_text, inject_into_system_message

# Package 3 (item 4) added in_chat_system: its own system message placed
# after the last user message (end of the list) -- the in-chat directive
# position. 0.46.0 (G1) added user_tail: appended to the newest user message
# (multimodal-safe; falls back to appending a user message if none exists).
# Unknown positions still fall through inject_entries' catch-all.
INJECTION_POSITIONS = ("system_start", "before_last_message", "in_chat_system", "user_tail")


@dataclass
class MatchedEntry:
    content: str
    position: str
    insertion_order: int
    name: str
    # Provenance, so a debug trace can name which lorebook an injected entry
    # came from and link back to it. Defaulted because callers that build entry
    # dicts by hand (imports, tests) do not supply them.
    lorebook_id: str = ""
    lorebook_name: str = ""
    entry_id: str = ""


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
    word_boundary: bool = False,
) -> list[MatchedEntry]:
    conversation_text = _extract_conversation_text(messages)
    candidates: list[MatchedEntry] = []

    for entry in entries:
        if entry.get("is_disabled"):
            continue

        if entry.get("is_constant"):
            candidates.append(_to_matched_entry(entry))
            continue

        primary_keys = parse_json_list(entry.get("keys", "[]"))
        if not primary_keys:
            continue

        primary_matched = any(
            _keyword_matches(key, conversation_text, word_boundary)
            for key in primary_keys
        )

        if not primary_matched:
            continue

        if entry.get("is_selective"):
            secondary_keys = parse_json_list(entry.get("secondary_keys", "[]"))
            if secondary_keys:
                secondary_matched = any(
                    _keyword_matches(key, conversation_text, word_boundary)
                    for key in secondary_keys
                )
                if not secondary_matched:
                    continue

        candidates.append(_to_matched_entry(entry))

    candidates.sort(key=lambda m: m.insertion_order)

    if total_budget <= 0:
        return candidates

    matched: list[MatchedEntry] = []
    used_chars = 0
    for item in candidates:
        matched.append(item)
        used_chars += len(item.content)
        if used_chars >= total_budget:
            break

    return matched


def match_book_entries(
    messages: list[dict],
    entries: list[dict],
    scan_depth: int = 0,
    word_boundary: bool = False,
    total_budget: int = 0,
) -> list[MatchedEntry]:
    """Match one book's entries, honoring that book's scan depth, its
    word-boundary flag and its budget share.

    scan_depth <= 0 scans the whole message list (previous behavior); N > 0
    matches keys against only the newest N messages, so a keyword mentioned
    in ancient history no longer triggers the entry and long chats stop
    paying a full-history regex scan per key.

    word_boundary (0.42.0, B7.1) is the book's opt-in to whole-word keyword
    matching ("Rook" stops firing on "rookie"). Substring matching -- the
    original behavior -- stays the default.

    total_budget (0.42.0, B9b) caps this book's matched content, in
    characters -- the caller converts the token share with the canonical
    len//4 estimator. Zero (the previous behavior) means uncapped.
    """
    window = messages[-scan_depth:] if scan_depth > 0 else messages
    return match_entries(window, entries, total_budget=total_budget, word_boundary=word_boundary)


def inject_entries(
    messages: list[dict],
    matched: list[MatchedEntry],
) -> list[dict]:
    if not matched:
        return messages

    result = [msg.copy() for msg in messages]

    system_entries = [m for m in matched if m.position == "system_start"]
    message_entries = [m for m in matched if m.position == "before_last_message"]
    user_tail_entries = [m for m in matched if m.position == "user_tail"]
    in_chat_entries = [m for m in matched if m.position == "in_chat_system"]
    other_entries = [
        m for m in matched
        if m.position not in INJECTION_POSITIONS
    ]

    if system_entries:
        combined = "\n\n".join(m.content for m in system_entries)
        result = inject_into_system_message(result, combined)

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

    if user_tail_entries:
        combined = "\n\n".join(m.content for m in user_tail_entries)
        last_user_idx = None
        for i in range(len(result) - 1, -1, -1):
            if result[i].get("role") == "user":
                last_user_idx = i
                break

        if last_user_idx is not None:
            target = result[last_user_idx]
            content = target.get("content")
            if isinstance(content, list):
                parts = list(content)
                parts.append({"type": "text", "text": "\n\n" + combined})
                target["content"] = parts
            else:
                existing = content if isinstance(content, str) else ""
                target["content"] = f"{existing}\n\n{combined}" if existing else combined
        else:
            result.append({"role": "user", "content": combined})

    if in_chat_entries:
        # Each in_chat_system entry is its own message at the very end --
        # the classic end-of-prompt directive slot. Not combined: scripts
        # inject distinct directives and merging them muddies attribution.
        for m in sorted(in_chat_entries, key=lambda e: e.insertion_order):
            result.append({"role": "system", "content": m.content})

    if other_entries:
        combined = "\n\n".join(m.content for m in other_entries)
        result.insert(len(result) - 1, {"role": "system", "content": combined})

    return result


def _keyword_matches(keyword: str, text: str, word_boundary: bool = False) -> bool:
    keyword_lower = keyword.lower()
    text_lower = text.lower()
    if word_boundary:
        # 0.42.0 (B7.1): opt-in whole-word matching. Substring was the only
        # behavior before and stays the default; the word-boundary branch
        # used to be dead code (a boundary match implies the substring
        # above had already matched).
        pattern = r"(?<!\w)" + re.escape(keyword_lower) + r"(?!\w)"
        return bool(re.search(pattern, text_lower))
    return keyword_lower in text_lower


def _extract_conversation_text(messages: list[dict]) -> str:
    parts = [extract_text(msg.get("content", "")) for msg in messages]
    return " ".join(p for p in parts if p)


def book_entries_to_dicts(
    entries: Any,
    *,
    book: Any = None,
    lorebook_id: str = "",
    lorebook_name: str = "",
) -> list[dict[str, Any]]:
    """Convert ORM lorebook entries into matcher-ready dicts."""
    lb_id = getattr(book, "id", lorebook_id) if book is not None else lorebook_id
    lb_name = getattr(book, "name", lorebook_name) if book is not None else lorebook_name
    return [
        {
            "lorebook_id": lb_id or getattr(entry, "lorebook_id", ""),
            "lorebook_name": lb_name or getattr(entry, "lorebook_name", ""),
            "entry_id": entry.id,
            "name": entry.name,
            "keys": entry.keys,
            "secondary_keys": entry.secondary_keys,
            "content": entry.content,
            "position": entry.position,
            "insertion_order": entry.insertion_order,
            "is_constant": entry.is_constant,
            "is_selective": entry.is_selective,
            "is_disabled": entry.is_disabled,
        }
        for entry in entries
    ]


def _to_matched_entry(entry: dict) -> MatchedEntry:
    return MatchedEntry(
        content=entry.get("content", ""),
        position=entry.get("position", "before_last_message"),
        insertion_order=entry.get("insertion_order", 10),
        name=entry.get("name", ""),
        lorebook_id=entry.get("lorebook_id", ""),
        lorebook_name=entry.get("lorebook_name", ""),
        entry_id=entry.get("entry_id", ""),
    )


async def load_active_lorebooks(
    db,
    user_id: str,
    tags: list | None = None,
    request_headers: dict[str, str] | None = None,
    internal_chat_id: str = "",
    direct_member_ids: set[str] | None = None,
) -> list:
    """The activated lorebook rows for one request: every candidate through
    the Activation Hierarchy (rule 19) plus the conversation bindings (the
    fifth source, union-only) plus direct tag-group membership (the seventh
    source: ids of members of activated opted-in groups, OR-ed in). Shared
    by the default injection pass (proxy._apply_lorebook_injection) and the
    manual-order orchestrator (pipeline_order.py) so both orderings
    activate through ONE code path.

    ``db`` is the caller's session; entries are eager-loaded.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook
    from app.models.conversation_lorebook_binding import ConversationLorebookBinding
    from app.models.lorebook import Lorebook
    from app.services.lexicon import resolve_action_identity
    from app.services.tagging import should_activate_resource

    # The tags parameter may be None (meaning "no tags"); normalize so the
    # unconditional should_activate_resource call below never iterates None
    # (the guard-shaped bug rule 19 exists to prevent).
    tags = tags or []

    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.user_id == user_id)
        .options(selectinload(Lorebook.entries))
    )
    lorebooks = result.scalars().all()

    # Fifth activation source (Phase 29a): the conversation binding. A union
    # source -- it only ever adds. A bound lorebook joins the candidates
    # regardless of its tag or Active flag, so a lexicon lorebook (created
    # inactive, untagged) injects in its own chat and in no other. No prompt
    # tag can switch it off.
    conversation_id, _ = resolve_action_identity(request_headers or {}, internal_chat_id)
    bound_lorebook_ids: set = set()
    if conversation_id:
        lex_rows = await db.execute(
            select(ConversationLorebookBinding.lorebook_id).where(
                ConversationLorebookBinding.user_id == user_id,
                ConversationLorebookBinding.conversation_id == conversation_id,
            )
        )
        bound_lorebook_ids = set(lex_rows.scalars().all())
        # Phase 20: script-created books bind through the companion table
        # (multi-book per conversation). Same union, same semantics -- the ""
        # sentinel rows never match a real conversation id, so unbound tagged
        # books stay tag-activated only (rule 19 source 3).
        dyn_rows = await db.execute(
            select(ConversationDynamicLorebook.lorebook_id).where(
                ConversationDynamicLorebook.user_id == user_id,
                ConversationDynamicLorebook.conversation_id == conversation_id,
            )
        )
        bound_lorebook_ids |= set(dyn_rows.scalars().all())

    active_lorebooks = []
    for lb in lorebooks:
        # Bound lorebooks join unconditionally (fifth source, union-only).
        # Direct group membership likewise (seventh source, union-only).
        # Every other candidate goes through should_activate_resource with no
        # caller-side pre-check: an `if lb.tag and tags:` shape here would
        # duplicate the hierarchy and only happen to agree with it.
        if (
            lb.id in bound_lorebook_ids
            or lb.id in (direct_member_ids or ())
            or should_activate_resource(
                lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, user_id, tags
            )
        ):
            active_lorebooks.append(lb)
    return active_lorebooks
