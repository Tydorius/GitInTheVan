"""Phase 49C: AI Assistant pane parity with 0.44.0–0.49.0 features."""

from __future__ import annotations

import json

import pytest

from app.main import app
from app.services.admin import get_admin_settings
from app.services.assistant.context import ToolContext
from app.services.assistant.executor import execute
from app.services.assistant.registry import TOOLS
from app.services.assistant.schema import tool_schema


async def _ctx(client, token, *, is_admin=True):
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    admin = await get_admin_settings()
    return ToolContext(
        user_id=me.json()["id"],
        is_admin=is_admin,
        bearer_token=token,
        app=app,
        conversation_id="conv-phase49",
        admin=admin,
        secrets=(),
    )


def _ok(outcome):
    assert outcome.ok, outcome.result
    return outcome.result


def _props(tool_name: str) -> dict:
    return tool_schema(TOOLS[tool_name])["function"]["parameters"]["properties"]


def _stage_props(tool_name: str) -> dict:
    stages = _props(tool_name)["stages"]
    if "items" in stages:
        return stages["items"]["properties"]
    for branch in stages.get("anyOf", []):
        if isinstance(branch, dict) and "items" in branch:
            return branch["items"]["properties"]
    raise AssertionError(f"Could not locate stage properties in {tool_name}: {stages}")


class TestToolSchemaParity:
    def test_verification_rule_schemas_expose_regex_pattern(self):
        for name in ("create_verification_rule", "update_verification_rule"):
            assert "regex_pattern" in _props(name), name

    def test_map_schemas_expose_final_verification_and_stage_history_window(self):
        for name in ("create_map", "update_map"):
            assert "final_verification_enabled" in _props(name), name
            stage_props = _stage_props(name)
            assert "history_window" in stage_props, name
            assert stage_props["history_window"].get("minimum") == 0

    def test_cantrip_schemas_expose_reserved_tokens_and_user_data_prefixes(self):
        for name in ("create_cantrip", "update_cantrip"):
            props = _props(name)
            assert "reserved_tokens" in props, name
            assert "user_data_prefixes" in props, name

    def test_lorebook_and_entry_schemas_expose_reserved_tokens_and_user_tail(self):
        for name in ("create_lorebook", "update_lorebook"):
            assert "reserved_tokens" in _props(name), name
        for name in ("add_lorebook_entry", "update_lorebook_entry"):
            pos = _props(name)["position"]
            assert "user_tail" in pos.get("enum", []), f"{name} position schema missing user_tail: {pos}"

    def test_skill_schemas_expose_reserved_tokens_and_budget_weight(self):
        for name in ("create_skill", "update_skill"):
            props = _props(name)
            assert "reserved_tokens" in props, name
            assert "budget_weight" in props, name

    def test_settings_schemas_expose_caching_block_size_and_timezone(self):
        props = _props("update_settings")
        for field in (
            "summarization_block_size",
            "cache_friendly_mode",
            "auto_cache_control",
            "timezone",
        ):
            assert field in props, f"update_settings missing {field}"
        assert "summarization_block_size" in _props("update_summarization_settings")

    def test_debug_tool_docs_mention_cached_tokens_and_cache_analysis(self):
        assert "cached_tokens" in TOOLS["list_debug_runs"].doc
        assert "cached_tokens" in TOOLS["get_debug_run"].doc
        assert "cache_analysis" in TOOLS["get_debug_run"].doc


class TestAdminReadsParity:
    @pytest.mark.asyncio
    async def test_install_health_surfaces_deno_pool_and_log_timezone(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token, is_admin=True)
        outcome = _ok(await execute(ctx, TOOLS["install_health"], {}))
        by_name = {r["name"]: r for r in outcome}

        assert "deno worker pool" in by_name
        pool_row = by_name["deno worker pool"]
        assert pool_row["passed"] is True
        pool_detail = json.loads(pool_row["detail"])
        assert "deno_pool_min_idle" in pool_detail
        assert "deno_pool_max_idle" in pool_detail
        assert "deno_pool_idle_ttl_seconds" in pool_detail
        assert "live" in pool_detail
        assert "idle_count" in pool_detail["live"]

        assert "log timezone" in by_name
        tz_row = by_name["log timezone"]
        assert tz_row["passed"] is True
        tz_detail = json.loads(tz_row["detail"])
        assert "log_timezone" in tz_detail


class TestSelfCheckParity:
    @pytest.mark.asyncio
    async def test_lint_does_not_flag_regex_only_rules_without_judge_endpoint(self, admin_client):
        client, token, _ = admin_client

        # Enable verification with NO judge endpoint configured.
        resp = await client.put(
            "/api/verification/settings",
            json={"verification_enabled": True, "verification_endpoint_id": None},
        )
        assert resp.status_code == 200, resp.text

        # Create an active regex-only verification rule.
        created = await client.post(
            "/api/verification/rules",
            json={
                "name": "No-Banana-Regex",
                "prompt": "Must not mention banana.",
                "regex_pattern": r"\bBANANA\b",
                "is_active": True,
            },
        )
        assert created.status_code == 201, created.text

        ctx = await _ctx(client, token)
        results = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        verif = [r for r in results if r["name"] == "verification configuration"]
        assert verif, f"Expected verification configuration check in {results}"
        assert verif[0]["passed"] is True
        assert "regex_pattern" in verif[0]["message"]

    @pytest.mark.asyncio
    async def test_lint_flags_llm_rule_without_judge_endpoint(self, admin_client):
        client, token, _ = admin_client

        resp = await client.put(
            "/api/verification/settings",
            json={"verification_enabled": True, "verification_endpoint_id": None},
        )
        assert resp.status_code == 200, resp.text

        created = await client.post(
            "/api/verification/rules",
            json={
                "name": "LLM-Only-Rule",
                "prompt": "Check tone carefully.",
                "regex_pattern": "",
                "is_active": True,
            },
        )
        assert created.status_code == 201, created.text

        ctx = await _ctx(client, token)
        results = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        verif = [r for r in results if r["name"] == "verification configuration"]
        assert verif
        assert verif[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_probe_judge_works_for_regex_only_rule_without_endpoint(self, admin_client):
        client, token, _ = admin_client

        created = await client.post(
            "/api/verification/rules",
            json={
                "name": "Regex-Probe-Rule",
                "prompt": "Must not contain BANANA.",
                "regex_pattern": r"BANANA",
                "is_active": True,
            },
        )
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]

        ctx = await _ctx(client, token)
        results = _ok(await execute(ctx, TOOLS["probe_judge"], {"rule_id": rule_id}))
        by_name = {r["name"]: r for r in results}
        assert by_name["violating probe"]["passed"] is True
        assert by_name["clean probe"]["passed"] is True

    @pytest.mark.asyncio
    async def test_lint_surfaces_non_failing_prompt_caching_tip(self, admin_client):
        client, token, _ = admin_client

        lb = await client.post(
            "/api/lorebooks",
            json={"name": "World", "is_active": True},
        )
        assert lb.status_code == 201, lb.text
        lb_id = lb.json()["id"]

        entry = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "Castle",
                "keys": ["castle"],
                "content": "A cold stone fortress.",
                "position": "before_last_message",
            },
        )
        assert entry.status_code == 201, entry.text

        ctx = await _ctx(client, token)
        results = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        caching = [r for r in results if r["name"] == "prompt caching"]
        assert caching
        assert caching[0]["passed"] is True
        assert "cache_friendly_mode" in caching[0]["message"]
        assert "user_tail" in caching[0]["message"]


class TestDocsParity:
    @pytest.mark.asyncio
    async def test_search_docs_finds_new_044_to_049_topics(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)

        for query in (
            "cache_analysis",
            "thinking_level",
            "history_window",
            "summarization_block_size",
            "log_timezone",
        ):
            result = _ok(await execute(ctx, TOOLS["search_docs"], {"query": query, "limit": 3}))
            hits = result.get("hits") or result.get("results") or []
            assert hits, f"search_docs returned no hits for {query!r}: {result}"
