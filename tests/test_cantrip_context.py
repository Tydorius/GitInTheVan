"""JanitorAI card-section parsing, seeding, and splice application.

Pure-Python coverage (no Deno) so it runs in every environment. The flattened
system-message formats below mirror JanitorAI captures from June 2026: the
current format and the legacy variant with inner Start/End markers.
"""

from app.services.cantrip_context import (
    apply_cantrip_result_to_messages,
    build_context,
    parse_character_sections,
)

PRESET = "You are a roleplay engine.\nWrite well."

PERSONA_VALUE = "Zuri is calm, sharp-witted, and fiercely loyal."
SCENARIO_VALUE = "A shared apartment at the tail end of autumn."
DIALOGS_VALUE = "User: Hi!\nZuri, Lucy: Welcome home."

USER_PERSONA = "<UserPersona>A heavy-set late-30s man with a full beard.</UserPersona>"

SYSTEM_FULL = (
    PRESET + "\n"
    f"<Zuri, Lucy's Persona>{PERSONA_VALUE}</Zuri, Lucy's Persona>\n"
    f"<Scenario>{SCENARIO_VALUE}</Scenario>\n"
    f"{USER_PERSONA}\n"
    f"<example_dialogs>{DIALOGS_VALUE}</example_dialogs>\n"
)

# A card with an empty example_dialogs: JanitorAI omits the whole section.
SYSTEM_NO_DIALOGS = (
    PRESET + "\n"
    f"<Zuri, Lucy's Persona>{PERSONA_VALUE}</Zuri, Lucy's Persona>\n"
    f"<Scenario>{SCENARIO_VALUE}</Scenario>\n"
    f"{USER_PERSONA}\n"
)

# Legacy format (June 11 captures): inner markers around each body.
SYSTEM_LEGACY = (
    PRESET + "\n"
    f"<Aria's Persona><Start Personality>\n{PERSONA_VALUE}\n<End Personality></Aria's Persona>\n"
    f"<Scenario><Start Scenario>\n{SCENARIO_VALUE}\n<End Scenario></Scenario>\n"
    "<UserPersona>A heavy-set late-30s man.</UserPersona>\n"
    f"<example_dialogs><Start Example Dialog>\n{DIALOGS_VALUE}\n<End Example Dialog></example_dialogs>\n"
)


def _messages(system: str):
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "."},
        {"role": "assistant", "content": "Welcome home."},
        {"role": "user", "content": "Tydorius: I enter the kitchen."},
    ]


# ============================================================================
# Parsing
# ============================================================================

class TestCharacterSectionParsing:
    def test_parses_all_three_sections(self):
        sections = parse_character_sections(_messages(SYSTEM_FULL))
        assert sections is not None
        assert sections.character_name == "Zuri, Lucy"
        assert sections.personality.value == PERSONA_VALUE
        assert sections.scenario.value == SCENARIO_VALUE
        assert sections.example_dialogs.value == DIALOGS_VALUE

    def test_parses_name_with_apostrophe(self):
        system = PRESET + "\n<O'Brien's Persona>Gruff and terse.</O'Brien's Persona>\n"
        sections = parse_character_sections(_messages(system))
        assert sections is not None
        assert sections.character_name == "O'Brien"
        assert sections.personality.value == "Gruff and terse."

    def test_legacy_inner_markers_stripped(self):
        sections = parse_character_sections(_messages(SYSTEM_LEGACY))
        assert sections is not None
        assert sections.character_name == "Aria"
        assert sections.personality.value == PERSONA_VALUE
        assert sections.scenario.value == SCENARIO_VALUE
        assert sections.example_dialogs.value == DIALOGS_VALUE
        assert "<Start Personality>" not in sections.personality.value
        assert "<End Scenario>" not in sections.scenario.value

    def test_missing_section_is_absent_not_empty(self):
        sections = parse_character_sections(_messages(SYSTEM_NO_DIALOGS))
        assert sections is not None
        assert sections.example_dialogs is None
        assert sections.scenario is not None
        assert sections.scenario.value == SCENARIO_VALUE

    def test_no_markers_returns_none(self):
        assert parse_character_sections(_messages("Plain system prompt.")) is None

    def test_no_system_message_returns_none(self):
        assert parse_character_sections([{"role": "user", "content": "Hi"}]) is None

    def test_empty_message_list_returns_none(self):
        assert parse_character_sections([]) is None

    def test_multipart_system_content_returns_none(self):
        messages = [{"role": "system", "content": [{"type": "text", "text": SYSTEM_FULL}]}]
        assert parse_character_sections(messages) is None


# ============================================================================
# build_context seeding
# ============================================================================

class TestBuildContextSeeding:
    def test_seeds_card_fields_from_system_message(self):
        ctx = build_context(_messages(SYSTEM_FULL))
        assert ctx["character"]["personality"] == PERSONA_VALUE
        assert ctx["character"]["scenario"] == SCENARIO_VALUE
        assert ctx["character"]["example_dialogs"] == DIALOGS_VALUE
        assert ctx["character"]["name"] == "Zuri, Lucy"

    def test_seeds_only_present_sections(self):
        ctx = build_context(_messages(SYSTEM_NO_DIALOGS))
        assert ctx["character"]["personality"] == PERSONA_VALUE
        assert ctx["character"]["example_dialogs"] == ""

    def test_explicit_character_name_wins(self):
        ctx = build_context(_messages(SYSTEM_FULL), character_name="Override")
        assert ctx["character"]["name"] == "Override"
        assert ctx["character"]["personality"] == PERSONA_VALUE

    def test_no_markers_leaves_fields_empty(self):
        ctx = build_context(_messages("Plain system prompt."))
        assert ctx["character"]["personality"] == ""
        assert ctx["character"]["scenario"] == ""
        assert ctx["character"]["example_dialogs"] == ""
        assert ctx["character"]["name"] == ""


# ============================================================================
# Splice application (JanitorAI replace semantics)
# ============================================================================

class TestCantripSplice:
    def test_replace_personality_in_place(self):
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), "REPLACED", SCENARIO_VALUE, DIALOGS_VALUE
        )
        assert len(result) == 4  # no messages added or removed
        system = result[0]["content"]
        assert "<Zuri, Lucy's Persona>REPLACED</Zuri, Lucy's Persona>" in system
        assert PERSONA_VALUE not in system  # the original is gone, not kept alongside
        assert "[Personality]" not in system
        assert SCENARIO_VALUE in system  # untouched fields survive unchanged
        assert DIALOGS_VALUE in system

    def test_append_extends_section_without_duplication(self):
        extended = PERSONA_VALUE + " Also nocturnal."
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), extended, SCENARIO_VALUE, DIALOGS_VALUE
        )
        system = result[0]["content"]
        assert system.count(PERSONA_VALUE) == 1
        assert "Also nocturnal." in system

    def test_clear_removes_section_entirely(self):
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), PERSONA_VALUE, "", DIALOGS_VALUE
        )
        system = result[0]["content"]
        assert "<Scenario>" not in system
        assert "</Scenario>" not in system
        assert SCENARIO_VALUE not in system
        assert USER_PERSONA in system  # neighbouring sections intact

    def test_set_absent_field_inserts_canonical_section(self):
        new_dialogs = "User: Yo.\nZuri, Lucy: Hey."
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_NO_DIALOGS), PERSONA_VALUE, SCENARIO_VALUE, new_dialogs
        )
        system = result[0]["content"]
        assert f"<example_dialogs>{new_dialogs}</example_dialogs>" in system
        # canonical position: after the user-persona section
        assert system.index(USER_PERSONA) < system.index("<example_dialogs>")

    def test_splice_is_idempotent(self):
        once = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), "NEW PERSONA", SCENARIO_VALUE, DIALOGS_VALUE
        )
        twice = apply_cantrip_result_to_messages(
            once, "NEW PERSONA", SCENARIO_VALUE, DIALOGS_VALUE
        )
        assert twice == once

    def test_userpersona_never_touched(self):
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), "X", "Y", "Z"
        )
        assert USER_PERSONA in result[0]["content"]

    def test_unchanged_values_leave_prompt_byte_identical(self):
        result = apply_cantrip_result_to_messages(
            _messages(SYSTEM_FULL), PERSONA_VALUE, SCENARIO_VALUE, DIALOGS_VALUE
        )
        assert result[0]["content"] == SYSTEM_FULL

    def test_no_markers_keeps_inject_fallback(self):
        messages = [
            {"role": "system", "content": "Plain system."},
            {"role": "user", "content": "Hello"},
        ]
        result = apply_cantrip_result_to_messages(messages, "", "Extra lore", "")
        assert len(result) == 3
        assert result[-2]["role"] == "system"
        assert "Extra lore" in result[-2]["content"]
