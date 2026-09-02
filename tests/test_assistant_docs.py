"""The documentation tools (Phase 26b).

The guide is the single source of truth, split at import on the same `<a id>`
anchors the in-app help links use. These tests are what stops a renamed anchor
silently turning a group's help pointer into an empty string -- the kind of
drift that broke twelve help links before rule 21 existed.
"""

from __future__ import annotations

import pytest

from app.services.assistant import docs
from app.services.assistant.registry import GROUPS, NAV_PAGES, TOOLS


class TestGuideLoading:
    def test_the_guide_loaded_at_all(self):
        # Vacuity guard: every anchor assertion below passes on an empty dict.
        assert len(docs.GUIDE) >= 10

    def test_concept_documents_loaded(self):
        assert "activation-hierarchy" in docs.CONCEPTS
        assert "parameter-layers" in docs.CONCEPTS
        assert all(text.strip() for text in docs.CONCEPTS.values())

    def test_every_group_help_anchor_resolves_to_a_section(self):
        for key, group in GROUPS.items():
            section = docs.guide_section(group.help_anchor)
            assert section.strip(), f"group '{key}' points at empty anchor '{group.help_anchor}'"

    def test_every_nav_page_with_an_anchor_resolves(self):
        for page, anchor in docs.PAGE_ANCHORS.items():
            assert page in NAV_PAGES, page
            assert docs.guide_section(anchor).strip(), anchor

    def test_sections_do_not_bleed_into_each_other(self):
        assert "<a id=" not in docs.guide_section("cantrips")

    def test_a_long_section_is_capped(self):
        assert len(docs.guide_section("cantrips")) <= docs.MAX_SECTION_CHARS + 100


class TestDescribePage:
    def test_known_page(self):
        text = docs.describe_page("/cantrips")
        assert "cantrip" in text.lower()
        assert "Related concept documents" in text

    def test_related_concepts_are_named(self):
        assert "cantrip-context-api" in docs.describe_page("/cantrips")

    def test_unknown_page_lists_what_is_known(self):
        text = docs.describe_page("/nowhere")
        assert "No documentation for page" in text
        assert "/cantrips" in text

    def test_empty_page_defaults_to_the_dashboard(self):
        assert "No documentation" not in docs.describe_page("")


class TestSearchDocs:
    def test_finds_the_cantrip_context_material(self):
        results = docs.search_docs("cantrip context api")
        assert results
        anchors = {r["anchor"] for r in results}
        assert anchors & {"cantrips", "cantrip-context-api"}

    def test_excerpts_are_bounded(self):
        for result in docs.search_docs("cantrip", limit=5):
            assert len(result["excerpt"]) <= docs.MAX_EXCERPT_CHARS

    def test_results_carry_a_source_and_anchor(self):
        for result in docs.search_docs("activation hierarchy"):
            assert result["source"] in ("user-guide", "concept")
            assert result["anchor"]

    def test_limit_is_honoured(self):
        assert len(docs.search_docs("the", limit=2)) <= 2

    def test_a_query_of_only_stopwords_returns_nothing(self):
        assert docs.search_docs("a of") == []

    def test_a_nonsense_query_returns_nothing(self):
        assert docs.search_docs("zzqqxxjjvv") == []

    def test_ranking_prefers_the_denser_paragraph(self):
        results = docs.search_docs("verification judge rule", limit=3)
        assert results
        assert any("verif" in r["anchor"] for r in results)

    def test_a_bad_limit_does_not_raise(self):
        assert isinstance(docs.search_docs("cantrip", limit="lots"), list)


class TestDescribeTool:
    def test_known_tool(self):
        out = docs.describe_tool("delete_cantrip")
        assert out["risk"] == "destructive"
        assert out["group"]["label"] == "Cantrips"
        assert "cantrip_id" in out["schema"]["properties"]
        assert out["doc"]

    def test_unknown_tool_is_an_error_result(self):
        out = docs.describe_tool("obliterate")
        assert out["error"]["status"] == 404

    def test_every_registered_tool_can_describe_itself(self):
        for name in TOOLS:
            out = docs.describe_tool(name)
            assert "error" not in out, name
            assert out["summary"], name


class TestHandlers:
    @pytest.mark.asyncio
    async def test_handlers_are_wired_to_the_registry(self, admin_client):
        client, token, _ = admin_client
        from app.main import app
        from app.services.admin import get_admin_settings
        from app.services.assistant.context import ToolContext
        from app.services.assistant.executor import execute

        me = (await client.get("/api/auth/me")).json()["id"]
        ctx = ToolContext(
            user_id=me,
            is_admin=True,
            bearer_token=token,
            app=app,
            conversation_id="docs",
            admin=await get_admin_settings(),
        )

        described = await execute(ctx, TOOLS["describe_tool"], {"name": "list_maps"})
        assert described.ok
        assert described.result["summary"]

        page = await execute(ctx, TOOLS["describe_page"], {"page": "/maps"})
        assert page.ok
        assert page.result["documentation"]

        searched = await execute(ctx, TOOLS["search_docs"], {"query": "map stage"})
        assert searched.ok
        assert searched.result["results"]

    @pytest.mark.asyncio
    async def test_a_local_tool_makes_no_http_call(self, admin_client, httpx_mock):
        """`httpx_mock` fails the test if a registered response goes unused, so
        registering none and passing proves the docs tools stay in-process."""
        client, token, _ = admin_client
        from app.main import app
        from app.services.admin import get_admin_settings
        from app.services.assistant.context import ToolContext
        from app.services.assistant.executor import execute

        me = (await client.get("/api/auth/me")).json()["id"]
        ctx = ToolContext(
            user_id=me,
            is_admin=True,
            bearer_token=token,
            app=app,
            conversation_id="docs",
            admin=await get_admin_settings(),
        )
        outcome = await execute(ctx, TOOLS["search_docs"], {"query": "lorebook"})
        assert outcome.ok


def test_a_missing_guide_degrades_rather_than_raising(tmp_path, monkeypatch):
    """A degraded install loses documentation, not the assistant."""
    monkeypatch.setattr(docs, "_GUIDE_PATH", tmp_path / "absent.md")
    assert docs._load_guide() == {}


def test_a_guide_with_no_anchors_degrades(tmp_path, monkeypatch):
    path = tmp_path / "user-guide.md"
    path.write_text("# No anchors here\n", encoding="utf-8")
    monkeypatch.setattr(docs, "_GUIDE_PATH", path)
    assert docs._load_guide() == {}
