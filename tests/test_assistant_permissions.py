"""The permission table (Phase 26b).

The whole point of this module is that Deny is absolute and yolo is narrow, so
every row of the table is exercised, including the ones that only matter when
two settings disagree.
"""

from __future__ import annotations

import pytest

from app.services.assistant.permissions import (
    effective,
    effective_for,
    group_mode,
    parse_prefs,
    tool_mode,
    validate_prefs,
)
from app.services.assistant.registry import GROUPS, TOOLS, Risk, Tool

READ = TOOLS["list_cantrips"]            # content group, read
WRITE = TOOLS["create_cantrip"]          # content group, write
DESTRUCTIVE = TOOLS["delete_cantrip"]    # content group, destructive
COST = TOOLS["run_debug_sandbox"]        # content group, external cost
CONFIG_WRITE = TOOLS["update_endpoint"]  # configuration group, write
CONFIG_READ = TOOLS["list_endpoints"]    # configuration group, read
PACKS_READ = TOOLS["list_repos"]         # packs group (default deny)


def _prefs(groups=None, tools=None):
    return {"groups": dict(groups or {}), "tools": dict(tools or {})}


def _effective(tool: Tool, prefs, *, session=None, yolo=False):
    return effective(
        tool, GROUPS[tool.group], prefs, session_allows=set(session or ()), yolo=yolo
    )


class TestNormal:
    @pytest.mark.parametrize(
        "tool,expected",
        [
            (READ, "allow"),
            (CONFIG_READ, "allow"),
            (WRITE, "ask"),
            (DESTRUCTIVE, "ask"),
            (COST, "ask"),
            (CONFIG_WRITE, "ask"),
        ],
    )
    def test_normal_by_risk(self, tool, expected):
        assert _effective(tool, _prefs()) == expected

    def test_session_allow_collapses_the_ask(self):
        assert _effective(WRITE, _prefs(), session={"create_cantrip"}) == "allow"

    def test_session_allow_is_per_tool(self):
        assert _effective(DESTRUCTIVE, _prefs(), session={"create_cantrip"}) == "ask"


class TestYolo:
    def test_yolo_allows_content_writes(self):
        assert _effective(WRITE, _prefs(), yolo=True) == "allow"

    def test_yolo_allows_content_destructive(self):
        assert _effective(DESTRUCTIVE, _prefs(), yolo=True) == "allow"

    def test_yolo_is_ignored_for_configuration_groups(self):
        assert _effective(CONFIG_WRITE, _prefs(), yolo=True) == "ask"

    def test_always_ask_still_asks_under_yolo(self):
        prefs = _prefs(tools={"create_cantrip": "always_ask"})
        assert _effective(WRITE, prefs, yolo=True) == "ask"

    def test_group_always_ask_still_asks_under_yolo(self):
        prefs = _prefs(groups={"cantrips": "always_ask"})
        assert _effective(READ, prefs, yolo=True) == "ask"


class TestDeny:
    def test_tool_deny(self):
        assert _effective(READ, _prefs(tools={"list_cantrips": "deny"})) == "deny"

    def test_group_deny(self):
        assert _effective(READ, _prefs(groups={"cantrips": "deny"})) == "deny"

    def test_deny_beats_yolo(self):
        prefs = _prefs(groups={"cantrips": "deny"})
        assert _effective(WRITE, prefs, yolo=True) == "deny"

    def test_deny_beats_always_allow_on_the_child(self):
        prefs = _prefs(groups={"cantrips": "deny"}, tools={"create_cantrip": "always_allow"})
        assert _effective(WRITE, prefs) == "deny"

    def test_deny_beats_session_allow(self):
        prefs = _prefs(tools={"create_cantrip": "deny"})
        assert _effective(WRITE, prefs, session={"create_cantrip"}) == "deny"

    def test_unset_packs_group_resolves_to_deny(self):
        assert _effective(PACKS_READ, _prefs()) == "deny"

    def test_packs_can_be_opened_explicitly(self):
        assert _effective(PACKS_READ, _prefs(groups={"packs": "normal"})) == "allow"


class TestAlwaysAllow:
    def test_tool_always_allow_beats_group_normal(self):
        prefs = _prefs(tools={"delete_cantrip": "always_allow"})
        assert _effective(DESTRUCTIVE, prefs) == "allow"

    def test_group_always_allow_covers_its_tools(self):
        prefs = _prefs(groups={"cantrips": "always_allow"})
        assert _effective(DESTRUCTIVE, prefs) == "allow"

    def test_child_always_ask_overrides_group_always_allow(self):
        prefs = _prefs(groups={"cantrips": "always_allow"}, tools={"delete_cantrip": "always_ask"})
        assert _effective(DESTRUCTIVE, prefs) == "ask"

    def test_child_inherit_takes_the_group(self):
        prefs = _prefs(groups={"cantrips": "always_allow"}, tools={"delete_cantrip": "inherit"})
        assert _effective(DESTRUCTIVE, prefs) == "allow"


class TestDegradation:
    def test_unknown_tool_mode_is_normal(self, caplog):
        prefs = _prefs(tools={"delete_cantrip": "yolo_forever"})
        assert _effective(DESTRUCTIVE, prefs) == "ask"

    def test_unknown_group_mode_is_normal(self):
        prefs = _prefs(groups={"cantrips": "banana"})
        assert _effective(READ, prefs) == "allow"

    def test_non_string_mode_does_not_raise(self):
        prefs = {"groups": {"cantrips": 7}, "tools": {}}
        assert _effective(READ, prefs) == "allow"

    def test_missing_sections_do_not_raise(self):
        assert _effective(READ, {}) == "allow"

    def test_unknown_mode_never_widens_access(self):
        # The degradation must not turn a write into an allow.
        prefs = _prefs(tools={"create_cantrip": "definitely_allow"})
        assert _effective(WRITE, prefs) == "ask"


class TestParsePrefs:
    def test_empty(self):
        assert parse_prefs("") == {"groups": {}, "tools": {}}
        assert parse_prefs(None) == {"groups": {}, "tools": {}}

    def test_garbage(self):
        assert parse_prefs("{not json") == {"groups": {}, "tools": {}}
        assert parse_prefs("[1,2,3]") == {"groups": {}, "tools": {}}

    def test_partial(self):
        parsed = parse_prefs('{"groups": {"cantrips": "deny"}}')
        assert parsed == {"groups": {"cantrips": "deny"}, "tools": {}}

    def test_non_string_values_are_dropped(self):
        parsed = parse_prefs('{"groups": {"cantrips": 5, "maps": "deny"}}')
        assert parsed["groups"] == {"maps": "deny"}


class TestValidatePrefs:
    def test_valid(self):
        assert validate_prefs(_prefs(groups={"maps": "deny"}, tools={"get_map": "inherit"})) == []

    def test_unknown_group(self):
        errors = validate_prefs(_prefs(groups={"nope": "deny"}))
        assert any("unknown group 'nope'" in e for e in errors)

    def test_unknown_tool(self):
        errors = validate_prefs(_prefs(tools={"nope": "deny"}))
        assert any("unknown tool 'nope'" in e for e in errors)

    def test_invalid_mode(self):
        errors = validate_prefs(_prefs(groups={"maps": "banana"}))
        assert any("invalid mode" in e for e in errors)

    def test_group_may_not_inherit(self):
        errors = validate_prefs(_prefs(groups={"maps": "inherit"}))
        assert any("invalid mode" in e for e in errors)

    def test_unknown_section(self):
        errors = validate_prefs({"grups": {}})
        assert any("unknown section" in e for e in errors)


class TestHelpers:
    def test_group_mode_falls_back_to_default(self):
        assert group_mode(GROUPS["packs"], _prefs()) == "deny"
        assert group_mode(GROUPS["maps"], _prefs()) == "normal"

    def test_tool_mode_defaults_to_inherit(self):
        assert tool_mode(READ, _prefs()) == "inherit"

    def test_effective_for_looks_up_the_group(self):
        assert effective_for(READ, _prefs()) == "allow"
        assert effective_for(WRITE, _prefs(), yolo=True) == "allow"


def test_every_registered_tool_resolves_under_default_prefs():
    for tool in TOOLS.values():
        verdict = effective_for(tool, _prefs())
        assert verdict in ("deny", "ask", "allow"), tool.name
        if GROUPS[tool.group].default_mode == "normal" and tool.risk is Risk.READ:
            assert verdict == "allow", tool.name
