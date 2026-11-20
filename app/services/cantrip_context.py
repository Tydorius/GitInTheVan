from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.messages import extract_text

logger = logging.getLogger(__name__)

_extract_text = extract_text


# ============================================================================
# JanitorAI card sections
#
# JanitorAI flattens a character card into tagged sections at the tail of the
# system message (verified against June 2026 wire captures):
#
#     {preset}\n
#     <{NAME}'s Persona>{personality}</{NAME}'s Persona>\n
#     <Scenario>{scenario}</Scenario>\n
#     <UserPersona>{user persona}</UserPersona>\n
#     <example_dialogs>{example dialogs}</example_dialogs>\n
#
# {NAME} is the chat-renamed character name (commas and apostrophes allowed).
# An empty field omits the whole section, markers included. A legacy variant
# wrapped each body in inner markers (<Start Personality> ... <End
# Personality>); those are stripped on extraction and never re-emitted.
# <UserPersona> is the *user's* persona, not card data, and is never modified.
# ============================================================================

_PERSONA_RE = re.compile(
    r"<(?P<name>[^\n<>]+)'s Persona>(?P<body>.*?)</(?P=name)'s Persona>",
    re.DOTALL,
)

_INNER_MARKERS = {
    "personality": ("<Start Personality>", "<End Personality>"),
    "scenario": ("<Start Scenario>", "<End Scenario>"),
    "example_dialogs": ("<Start Example Dialog>", "<End Example Dialog>"),
}

# Fixed open/close markers for the non-persona sections, in wire order.
_FIXED_SECTIONS: list[tuple[str, str, str]] = [
    ("scenario", "<Scenario>", "</Scenario>"),
    ("example_dialogs", "<example_dialogs>", "</example_dialogs>"),
]


@dataclass
class CharacterSection:
    """One card section located inside the system message content."""

    value: str
    start: int  # offset of the opening marker
    end: int  # offset just past the closing marker
    body_start: int  # offset just past the opening marker
    body_end: int  # offset of the closing marker


@dataclass
class CharacterSections:
    character_name: str
    personality: CharacterSection | None = None
    scenario: CharacterSection | None = None
    example_dialogs: CharacterSection | None = None

    def any_present(self) -> bool:
        return any((self.personality, self.scenario, self.example_dialogs))


def _strip_inner_markers(field: str, body: str) -> str:
    open_marker, close_marker = _INNER_MARKERS[field]
    if body.startswith(open_marker) and body.endswith(close_marker):
        body = body[len(open_marker):-len(close_marker)]
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
    return body


def _first_system_text(messages: list[dict[str, Any]]) -> str | None:
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            return content if isinstance(content, str) else None
    return None


def parse_character_sections(
    messages: list[dict[str, Any]],
) -> CharacterSections | None:
    """Locate JanitorAI card sections in the first system message.

    Returns None when no section markers are present (non-JanitorAI clients),
    so callers can fall back to inject-block behavior. Section order follows
    the wire format: persona, then scenario, then example dialogs, each
    searched only after the previous section's close so a marker mentioned in
    the preset cannot be matched ahead of the real section.
    """
    content = _first_system_text(messages)
    if content is None:
        return None

    sections = CharacterSections(character_name="")
    search_from = 0

    persona = _PERSONA_RE.search(content)
    if persona:
        sections.character_name = persona.group("name")
        sections.personality = CharacterSection(
            value=_strip_inner_markers("personality", persona.group("body")),
            start=persona.start(),
            end=persona.end(),
            body_start=persona.start("body"),
            body_end=persona.end("body"),
        )
        search_from = persona.end()

    for field, open_marker, close_marker in _FIXED_SECTIONS:
        idx = content.find(open_marker, search_from)
        if idx == -1:
            continue
        close_idx = content.find(close_marker, idx + len(open_marker))
        if close_idx == -1:
            continue
        body_start = idx + len(open_marker)
        section = CharacterSection(
            value=_strip_inner_markers(field, content[body_start:close_idx]),
            start=idx,
            end=close_idx + len(close_marker),
            body_start=body_start,
            body_end=close_idx,
        )
        setattr(sections, field, section)
        search_from = section.end

    if not sections.any_present():
        return None
    return sections


def build_context(
    messages: list[dict[str, Any]],
    conversation_id: str = "",
    user_name: str = "User",
    character_name: str = "",
    character_description: str = "",
    first_message: str = "",
    alternate_greetings: list[str] | None = None,
    request: dict[str, Any] | None = None,
    turn_index: int = 0,
) -> dict[str, Any]:
    last_messages = []
    for msg in messages:
        text = _extract_text(msg.get("content", ""))
        # Package 3 (item 2): roles ride along now -- swipe-aware scripts
        # branch on them. Additive; scripts reading only .message (the
        # documented JanitorAI shape) are unaffected.
        last_messages.append({"message": text, "role": msg.get("role", "")})

    last_message = last_messages[-1]["message"] if last_messages else ""
    message_count = len(messages)
    message_created_at = datetime.now(UTC).isoformat()

    # Seed the mutable card fields with the request's real section values so
    # scripts see (and replace or extend) what JanitorAI would have seeded,
    # instead of operating on empty strings.
    sections = parse_character_sections(messages)
    if sections is not None:
        if not character_name:
            character_name = sections.character_name
        personality = sections.personality.value if sections.personality else ""
        scenario = sections.scenario.value if sections.scenario else ""
        example_dialogs = (
            sections.example_dialogs.value if sections.example_dialogs else ""
        )
    else:
        personality = scenario = example_dialogs = ""

    return {
        "chat": {
            "last_message": last_message,
            "last_messages": last_messages,
            "message_count": message_count,
            # Monotonic per-conversation request counter (0.35.0).
            # message_count stalls once the client trims history; turn_index
            # only ever goes up. 0 when no counter could be resolved.
            "turn_index": turn_index,
            "conversation_id": conversation_id,
            "user_name": user_name,
            "message_created_at": message_created_at,
        },
        # Read-only request facts (Maps QoL item 5). Normalized so a context
        # built without params still has every key -- scripts branch on
        # map_stage_name without existence checks.
        "request": {
            "model": (request or {}).get("model", ""),
            "stream": bool((request or {}).get("stream", False)),
            "is_map": bool((request or {}).get("is_map", False)),
            "map_name": (request or {}).get("map_name", ""),
            "map_stage_index": (request or {}).get("map_stage_index", -1),
            "map_stage_name": (request or {}).get("map_stage_name", ""),
            "map_stage_endpoint": (request or {}).get("map_stage_endpoint", ""),
            # Package 3 (item 2): per-request id + regenerate heuristic.
            "id": (request or {}).get("id", ""),
            "is_regenerate": bool((request or {}).get("is_regenerate", False)),
        },
        "character": {
            "name": character_name,
            "personality": personality,
            "scenario": scenario,
            "example_dialogs": example_dialogs,
            "description": character_description,
            "first_message": first_message,
            "alternate_greetings": alternate_greetings or [],
        },
    }


def extract_context_params(body: dict[str, Any], request_headers: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}

    conversation_id = request_headers.get("x-conversation-id", "")
    if not conversation_id:
        conversation_id = request_headers.get("x-chat-id", "")
    if not conversation_id:
        # Clients that send no conversation header still have an internal
        # rolling-hash chat id. Without this fallback, post-driver cantrips
        # (including map stage capture) silently dropped their chat_data
        # writes: the store save is keyed on this value and skipped when
        # empty, while pre-driver cantrips persisted fine because their
        # caller passes the internal id explicitly.
        conversation_id = body.get("_gitv_chat_id", "")
    params["conversation_id"] = conversation_id

    user_name = request_headers.get("x-user-name", "User")
    params["user_name"] = user_name

    # Read-only request facts (Maps QoL item 5): which model the client asked
    # for, whether this is a stream, and -- for cantrips running inside a map
    # stage -- which map and stage. Retires the guess-a-stage-by-timer hacks;
    # a stage cantrip can branch on context.request.map_stage_name instead.
    stage_info = body.get("_gitv_stage_info") or {}
    params["request"] = {
        "model": body.get("model", ""),
        "stream": bool(body.get("stream", False)),
        "is_map": bool(stage_info),
        "map_name": stage_info.get("map", ""),
        "map_stage_index": stage_info.get("index", -1),
        "map_stage_name": stage_info.get("name", ""),
        "map_stage_endpoint": stage_info.get("endpoint", ""),
        # Package 3 (item 2): per-request id (stable across every cantrip
        # position and the debug run) and the regenerate heuristic.
        "id": body.get("_gitv_request_id", ""),
        "is_regenerate": bool(body.get("_gitv_is_regenerate", False)),
    }

    # The monotonic turn index, stamped by the proxy before the first cantrip
    # pass. 0 when no counter could be resolved (replays, unauthenticated).
    params["turn_index"] = int(body.get("_gitv_turn_index", 0) or 0)

    return params


def _section_text(field: str, character_name: str, value: str) -> str:
    if field == "personality":
        return f"<{character_name}'s Persona>{value}</{character_name}'s Persona>"
    if field == "scenario":
        return f"<Scenario>{value}</Scenario>"
    return f"<example_dialogs>{value}</example_dialogs>"


# Canonical section order in the wire format. An absent section is inserted
# immediately before the first follower still present, or appended at the end.
_SECTION_FOLLOWERS = {
    "personality": ("<Scenario>", "<UserPersona>", "<example_dialogs>"),
    "scenario": ("<UserPersona>", "<example_dialogs>"),
    "example_dialogs": (),
}


def _insert_anchor(content: str, field: str) -> int:
    for marker in _SECTION_FOLLOWERS[field]:
        idx = content.find(marker)
        if idx != -1:
            return idx
    return len(content)


def _splice_sections(
    result: list[dict[str, Any]],
    sections: CharacterSections,
    finals: dict[str, str],
) -> dict[str, str]:
    """Rewrite the card's own sections in the first system message.

    JanitorAI replace semantics: a changed final value replaces the section
    body in place, an empty final removes the section entirely (JanitorAI
    omits empty sections), and a value for a section the card did not have
    inserts one at its canonical position. Returns the fields that could not
    be spliced; the caller falls back to block injection for those.
    """
    system_idx = next(
        (i for i, m in enumerate(result) if m.get("role") == "system"), None
    )
    if system_idx is None:
        return finals

    content = result[system_idx]["content"]
    existing = {
        "personality": sections.personality,
        "scenario": sections.scenario,
        "example_dialogs": sections.example_dialogs,
    }

    edits: list[tuple[int, int, str]] = []
    inserts: list[str] = []
    deferred: dict[str, str] = {}

    for field, section in existing.items():
        final = finals[field]
        if section is None:
            if final:
                # Without the persona marker there is no character name to
                # build the persona wrapper from; defer to block injection.
                if field == "personality" and not sections.character_name:
                    deferred[field] = final
                else:
                    inserts.append(field)
            continue
        if final == section.value:
            continue
        if not final:
            remove_end = section.end
            if content[remove_end:remove_end + 1] == "\n":
                remove_end += 1
            edits.append((section.start, remove_end, ""))
        else:
            edits.append((section.body_start, section.body_end, final))

    # Apply last-to-first so earlier spans stay valid.
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        content = content[:start] + replacement + content[end:]

    # Inserts re-anchor on the updated content after each insertion.
    for field in inserts:
        anchor = _insert_anchor(content, field)
        text = _section_text(field, sections.character_name, finals[field])
        prefix = "" if anchor == 0 or content[anchor - 1] == "\n" else "\n"
        content = content[:anchor] + prefix + text + "\n" + content[anchor:]

    if edits or inserts:
        result[system_idx] = {**result[system_idx], "content": content}
        logger.debug(
            "Card sections spliced in place: %d edit(s), %d insert(s)",
            len(edits), len(inserts),
        )
    return deferred


def _inject_blocks(result: list[dict[str, Any]], finals: dict[str, str]) -> None:
    """Insert cantrip output as additional system messages.

    Fallback for requests without JanitorAI card sections (non-JanitorAI
    clients) and for fields the splice could not handle.
    """
    additions: list[tuple[str, str]] = []
    if finals.get("scenario"):
        additions.append(("scenario", finals["scenario"]))
    if finals.get("personality"):
        additions.append(("personality", finals["personality"]))
    if finals.get("example_dialogs"):
        additions.append(("example_dialogs", finals["example_dialogs"]))

    if not additions:
        return

    personality_parts = []
    scenario_parts = []
    dialog_parts = []

    for label, text in additions:
        if label == "personality":
            personality_parts.append(text)
        elif label == "scenario":
            scenario_parts.append(text)
        elif label == "example_dialogs":
            dialog_parts.append(text)

    system_idx = None
    last_user_idx = None
    for i, msg in enumerate(result):
        if msg.get("role") == "system" and system_idx is None:
            system_idx = i
        if msg.get("role") == "user":
            last_user_idx = i

    if personality_parts:
        combined = "\n".join(personality_parts)
        block = f"[Personality]\n{combined}\n[/Personality]"
        if system_idx is not None:
            insert_at = system_idx + 1
            result.insert(insert_at, {"role": "system", "content": block})
        else:
            insert_at = 0
            result.insert(0, {"role": "system", "content": block})
        if last_user_idx is not None and insert_at <= last_user_idx:
            last_user_idx += 1

    if dialog_parts:
        combined = "\n".join(dialog_parts)
        block = f"[Example Dialogs]\n{combined}\n[/Example Dialogs]"
        insert_idx = last_user_idx if last_user_idx is not None else len(result)
        result.insert(insert_idx, {"role": "system", "content": block})

    if scenario_parts:
        combined = "\n".join(scenario_parts)
        block = f"[Scenario]\n{combined}\n[/Scenario]"
        insert_idx = last_user_idx if last_user_idx is not None else len(result)
        result.insert(insert_idx, {"role": "system", "content": block})


def apply_cantrip_result_to_messages(
    messages: list[dict[str, Any]],
    result_personality: str,
    result_scenario: str,
    result_example_dialogs: str,
) -> list[dict[str, Any]]:
    finals = {
        "personality": result_personality,
        "scenario": result_scenario,
        "example_dialogs": result_example_dialogs,
    }
    result = [msg.copy() for msg in messages]

    sections = parse_character_sections(result)
    if sections is not None:
        deferred = _splice_sections(result, sections, finals)
        if deferred:
            _inject_blocks(result, deferred)
        return result

    _inject_blocks(result, finals)
    return result
