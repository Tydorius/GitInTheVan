"""Registry invariants (Phase 26b).

These are the properties every other assistant module assumes: a tool has a
group that exists, a risk that matches what it does, and a doc the model can
act on. A registry edit that breaks one of them breaks the boundary tests, the
prompt and the schema at once, so they are checked here first.
"""

from __future__ import annotations

import pytest

from app.services.assistant.registry import (
    GROUPS,
    HARD_DENY_PREFIXES,
    HARD_DENY_ROUTES,
    NAV_PAGES,
    TOOLS,
    Group,
    Risk,
    tools_by_group,
    visible_tools,
)


class _Admin:
    def __init__(self, packs=False, admin_reads=False):
        self.assistant_packs_enabled = packs
        self.assistant_admin_reads_enabled = admin_reads


class _Ctx:
    def __init__(self, admin, is_admin=False):
        self.admin = admin
        self.is_admin = is_admin


class TestRegistryShape:
    def test_every_tool_has_a_known_group(self):
        for tool in TOOLS.values():
            assert tool.group in GROUPS, f"{tool.name} names group {tool.group!r}"

    def test_every_tool_has_summary_and_doc(self):
        for tool in TOOLS.values():
            assert tool.summary.strip(), f"{tool.name} has no summary"
            lines = [line for line in tool.doc.strip().splitlines() if line.strip()]
            assert 2 <= len(lines) <= 6, f"{tool.name} doc has {len(lines)} lines"

    def test_tool_names_are_unique(self):
        # TOOLS is built by name, so a duplicate would silently vanish.
        from app.services.assistant.registry import _TOOL_LIST

        assert len(_TOOL_LIST) == len(TOOLS)

    def test_every_group_category_is_known(self):
        for group in GROUPS.values():
            assert group.category in ("content", "configuration", "admin")

    def test_spec_categories(self):
        content = {
            "cantrips", "lorebooks", "skills", "tags", "verification",
            "memories", "maps", "debug", "selfcheck", "docs", "navigation",
        }
        configuration = {"endpoints", "settings", "packs", "diagnostics"}
        assert {k for k, g in GROUPS.items() if g.category == "content"} == content
        assert {k for k, g in GROUPS.items() if g.category == "configuration"} == configuration
        assert {k for k, g in GROUPS.items() if g.category == "admin"} == {"admin_reads"}

    def test_packs_defaults_to_deny_and_is_admin_gated(self):
        assert GROUPS["packs"].default_mode == "deny"
        assert GROUPS["packs"].admin_flag == "assistant_packs_enabled"

    def test_admin_reads_is_admin_gated(self):
        assert GROUPS["admin_reads"].admin_flag == "assistant_admin_reads_enabled"

    def test_every_other_group_defaults_to_normal(self):
        for key, group in GROUPS.items():
            if key == "packs":
                continue
            assert group.default_mode == "normal", key

    def test_selfcheck_and_admin_reads_are_populated(self):
        grouped = tools_by_group(list(TOOLS.values()))
        assert {t.name for t in grouped["selfcheck"]} == {
            "list_self_checks",
            "dry_run_activation",
            "dry_run_lorebook",
            "explain_parameters",
            "lint_configuration",
            "report_routing",
            "run_sandbox_smoke",
            "probe_endpoint",
            "probe_judge",
            "probe_summarizer",
            "probe_map",
        }
        assert {t.name for t in grouped["admin_reads"]} == {
            "schema_report",
            "read_server_logs",
            "install_health",
        }
        assert all(t.kind == "local" for t in grouped["selfcheck"])
        assert all(t.kind == "local" and t.admin_only for t in grouped["admin_reads"])


class TestRiskRules:
    def test_get_routes_are_reads(self):
        for tool in TOOLS.values():
            if tool.kind == "http" and tool.method == "GET":
                assert tool.risk in (Risk.READ, Risk.EXTERNAL_COST), tool.name

    def test_deletes_are_destructive(self):
        for tool in TOOLS.values():
            if tool.kind == "http" and tool.method == "DELETE":
                # Unsaving and detaching remove a flag or a link, not content.
                if tool.name in ("unsave_debug_run", "detach_skill"):
                    continue
                assert tool.risk is Risk.DESTRUCTIVE, tool.name

    @pytest.mark.parametrize(
        "name",
        [
            "clear_debug_runs",
            "replay_debug_run",
            "reset_debug_sandbox",
            "delete_memory",
        ],
    )
    def test_named_destructive_tools(self, name):
        assert TOOLS[name].risk is Risk.DESTRUCTIVE

    @pytest.mark.parametrize(
        "name",
        [
            "test_verification",
            "run_diagnostics",
            "run_debug_sandbox",
            "list_endpoint_models",
        ],
    )
    def test_named_external_cost_tools(self, name):
        assert TOOLS[name].risk is Risk.EXTERNAL_COST

    @pytest.mark.parametrize("name", ["validate_cantrip", "test_cantrip", "test_stored_cantrip"])
    def test_cantrip_sandbox_tools_are_reads(self, name):
        # Sandboxed, bounded, persists nothing -- so it must not cost an ask.
        assert TOOLS[name].risk is Risk.READ


class TestHardBoundary:
    def test_no_tool_sits_on_a_hard_denied_prefix(self):
        for tool in TOOLS.values():
            if tool.kind != "http":
                continue
            assert not tool.path.startswith(HARD_DENY_PREFIXES), tool.name

    def test_no_tool_sits_on_a_hard_denied_route(self):
        for tool in TOOLS.values():
            if tool.kind != "http":
                continue
            assert (tool.method, tool.path) not in HARD_DENY_ROUTES, tool.name

    def test_local_repo_link_is_absent(self):
        paths = {t.path for t in TOOLS.values()}
        assert "/api/packs/repos/local" not in paths

    def test_endpoint_reads_carry_a_projection(self):
        assert TOOLS["list_endpoints"].project_result is not None
        assert TOOLS["get_endpoint"].project_result is not None

    def test_update_endpoint_strips_the_api_key(self):
        projected = TOOLS["update_endpoint"].project_args({"name": "x", "api_key": "sk-live"})
        assert "api_key" not in projected
        assert projected["name"] == "x"


class TestNavPages:
    def test_navigate_validates_against_nav_pages(self):
        assert TOOLS["navigate"].kind == "client"
        assert "/endpoints" in NAV_PAGES
        assert "/assistant-security" in NAV_PAGES

    def test_nav_pages_have_no_duplicates(self):
        assert len(set(NAV_PAGES)) == len(NAV_PAGES)


class TestVisibility:
    def test_packs_hidden_while_the_admin_flag_is_off(self):
        tools = visible_tools(_Ctx(_Admin(packs=False)))
        assert not [t for t in tools if t.group == "packs"]

    def test_packs_visible_once_the_admin_flag_is_on(self):
        tools = visible_tools(_Ctx(_Admin(packs=True)))
        packs = [t for t in tools if t.group == "packs"]
        assert packs, "packs group should appear when the admin enables it"
        # And enabling packs must not drag anything else in.
        assert {t.name for t in packs} < set(TOOLS)

    def test_admin_only_tool_needs_admin_and_the_flag(self):
        # Real admin_only tools exist from 26d on (test_assistant_admin_reads.py
        # covers those); this asserts the gate's shape in isolation from any one
        # tool's own behaviour.
        from dataclasses import replace

        probe = replace(TOOLS["list_cantrips"], name="probe_admin", admin_only=True)
        original = TOOLS.get("probe_admin")
        TOOLS["probe_admin"] = probe
        try:
            assert "probe_admin" not in {t.name for t in visible_tools(_Ctx(_Admin(), is_admin=True))}
            assert "probe_admin" not in {
                t.name for t in visible_tools(_Ctx(_Admin(admin_reads=True), is_admin=False))
            }
            assert "probe_admin" in {
                t.name for t in visible_tools(_Ctx(_Admin(admin_reads=True), is_admin=True))
            }
        finally:
            if original is None:
                TOOLS.pop("probe_admin", None)
            else:  # pragma: no cover - defensive
                TOOLS["probe_admin"] = original

    def test_tools_by_group_includes_empty_groups(self):
        grouped = tools_by_group(visible_tools(_Ctx(_Admin())))
        assert set(grouped) >= set(GROUPS)
        assert grouped["packs"] == []


def test_group_dataclass_defaults():
    group = Group("k", "Label", "/x", "anchor", "content")
    assert group.default_mode == "normal"
    assert group.admin_flag is None
