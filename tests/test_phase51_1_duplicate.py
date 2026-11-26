"""Phase 0.51.1 tests: Duplicate endpoints (Rule 23 owner-scoped) & Cantrip Tester user_data_prefixes scoping."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.endpoint import Endpoint
from app.models.user_data import UserData
from tests.conftest import TestSessionLocal


async def _create_second_user_headers(client) -> dict[str, str]:
    """Create a non-admin second user via the currently-authenticated admin client and return their auth headers."""
    resp = await client.post(
        "/api/users",
        json={"username": "user_b", "password": "userbpass123"},
    )
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/auth/login",
        json={"username": "user_b", "password": "userbpass123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


class TestDuplicateEndpoint:
    @pytest.mark.asyncio
    async def test_duplicate_endpoint_preserves_all_fields_and_real_api_key(self, admin_client):
        client, _, _ = admin_client
        secret_key = "sk-super-secret-real-provider-key-9999"
        create_resp = await client.post(
            "/api/endpoints",
            json={
                "name": "Primary OpenRouter",
                "base_url": "https://openrouter.ai/api",
                "api_key": secret_key,
                "api_base_path": "/v1",
                "provider": "openrouter",
                "default_model": "anthropic/claude-3.5-sonnet",
                "bypass_method": "headers",
                "enabled": True,
                "role_tag": "driver",
                "priority": 3,
                "custom_tag": "fast-tier",
                "parameters": [
                    {"name": "temperature", "value": 0.7, "enabled": True},
                    {"name": "top_p", "value": 0.9, "enabled": False},
                ],
                "models": [
                    {
                        "name": "anthropic/claude-3.5-sonnet",
                        "description": "Main model",
                        "parameters": [{"name": "max_tokens", "value": 4096, "enabled": True}],
                    },
                    {
                        "name": "openai/gpt-4o",
                        "description": "Fallback model",
                        "parameters": [{"name": "temperature", "value": 0.4, "enabled": True}],
                    },
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        orig = create_resp.json()

        dup_resp = await client.post(f"/api/endpoints/{orig['id']}/duplicate")
        assert dup_resp.status_code == 201, dup_resp.text
        copy = dup_resp.json()

        assert copy["id"] != orig["id"]
        assert copy["name"] == "Primary OpenRouter (Copy)"
        assert copy["api_key_set"] is True
        assert copy["base_url"] == orig["base_url"]
        assert copy["api_base_path"] == orig["api_base_path"]
        assert copy["provider"] == orig["provider"]
        assert copy["default_model"] == orig["default_model"]
        assert copy["bypass_method"] == orig["bypass_method"]
        assert copy["enabled"] == orig["enabled"]
        assert copy["role_tag"] == orig["role_tag"]
        assert copy["priority"] == orig["priority"]
        assert copy["custom_tag"] == orig["custom_tag"]
        assert copy["parameters"] == orig["parameters"]

        assert len(copy["models"]) == 2
        assert [m["name"] for m in copy["models"]] == [m["name"] for m in orig["models"]]
        assert [m["parameters"] for m in copy["models"]] == [m["parameters"] for m in orig["models"]]
        assert {m["id"] for m in copy["models"]}.isdisjoint({m["id"] for m in orig["models"]})

        # Verify real unmasked api_key is stored in DB and revealed cleanly
        async with TestSessionLocal() as db:
            row = (await db.execute(select(Endpoint).where(Endpoint.id == copy["id"]))).scalar_one()
            assert row.api_key == secret_key

        reveal_resp = await client.post(f"/api/endpoints/{copy['id']}/reveal")
        assert reveal_resp.status_code == 200
        assert reveal_resp.json()["api_key"] == secret_key

        # Editing the copy does not mutate the original
        upd_resp = await client.put(
            f"/api/endpoints/{copy['id']}",
            json={
                "name": "Renamed Copy",
                "base_url": "https://example.invalid",
                "api_key": "sk-changed-copy-key",
                "custom_tag": "mutated-tag",
            },
        )
        assert upd_resp.status_code == 200

        async with TestSessionLocal() as db:
            orig_row = (await db.execute(select(Endpoint).where(Endpoint.id == orig["id"]))).scalar_one()
            assert orig_row.name == "Primary OpenRouter"
            assert orig_row.base_url == "https://openrouter.ai/api"
            assert orig_row.api_key == secret_key
            assert orig_row.custom_tag == "fast-tier"


class TestDuplicateCantrip:
    @pytest.mark.asyncio
    async def test_duplicate_cantrip_preserves_fields_and_deactivates(self, admin_client):
        client, _, _ = admin_client
        create_resp = await client.post(
            "/api/cantrips",
            json={
                "name": "RPG Tracker",
                "description": "Tracks HP and mana",
                "llm_instructions": "Track stats cleanly.",
                "code": 'context.character.scenario += " [HP:100]";',
                "hook_type": "pre",
                "run_pre_driver": True,
                "run_driver_callable": True,
                "run_pre_navigator": True,
                "run_post_navigator": True,
                "is_public": True,
                "is_active": True,
                "execution_order": 25,
                "timeout_ms": 3500,
                "tag": "rpg_core",
                "budget_weight": 2.5,
                "reserved_tokens": 150,
                "lorebook_reads": ["World Lore", "Bestiary"],
                "response_tags": ["hp_delta", "loot"],
                "user_data_prefixes": ["rpg.", "quest."],
                "card_edits_standing": True,
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        orig = create_resp.json()

        dup_resp = await client.post(f"/api/cantrips/{orig['id']}/duplicate")
        assert dup_resp.status_code == 201, dup_resp.text
        copy = dup_resp.json()

        assert copy["id"] != orig["id"]
        assert copy["name"] == "RPG Tracker (Copy)"
        assert copy["is_active"] is False
        assert copy["is_public"] is False

        for field in (
            "description",
            "llm_instructions",
            "code",
            "hook_type",
            "run_pre_driver",
            "run_driver_callable",
            "run_pre_navigator",
            "run_post_navigator",
            "execution_order",
            "timeout_ms",
            "tag",
            "budget_weight",
            "reserved_tokens",
            "lorebook_reads",
            "response_tags",
            "user_data_prefixes",
            "card_edits_standing",
        ):
            assert copy[field] == orig[field], f"Mismatch on field {field}"


class TestDuplicateLorebook:
    @pytest.mark.asyncio
    async def test_duplicate_lorebook_deep_copies_entries(self, admin_client):
        client, _, _ = admin_client
        create_resp = await client.post(
            "/api/lorebooks",
            json={
                "name": "Eldoria Compendium",
                "description": "Kingdom lore",
                "llm_instructions": "Use sparingly.",
                "is_public": True,
                "is_active": True,
                "tag": "eldoria",
                "run_pre_driver": True,
                "run_driver_callable": True,
                "run_pre_navigator": False,
                "run_post_navigator": True,
                "inject_after_cantrips": True,
                "scan_depth": 6,
                "word_boundary_keys": True,
                "budget_weight": 1.8,
                "reserved_tokens": 250,
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        orig_lb = create_resp.json()

        entry_payloads = [
            {
                "name": "Silver Citadel",
                "keys": ["citadel", "silver"],
                "secondary_keys": ["north"],
                "content": "A gleaming fortress in the northern peaks.",
                "content_summary": "Northern fortress.",
                "content_bullets": "- Gleaming fortress",
                "position": "before_last_message",
                "insertion_order": 5,
                "is_constant": True,
                "is_selective": True,
                "is_disabled": False,
                "character_limit": 500,
            },
            {
                "name": "Guild of Scribes",
                "keys": ["scribe", "guild"],
                "secondary_keys": [],
                "content": "Keepers of ancient maps and scrolls.",
                "content_summary": "Map keepers.",
                "content_bullets": "- Ancient scrolls",
                "position": "system_start",
                "insertion_order": 15,
                "is_constant": False,
                "is_selective": False,
                "is_disabled": True,
                "character_limit": 0,
            },
        ]
        orig_entry_ids = []
        for payload in entry_payloads:
            er = await client.post(f"/api/lorebooks/{orig_lb['id']}/entries", json=payload)
            assert er.status_code == 201, er.text
            orig_entry_ids.append(er.json()["id"])

        dup_resp = await client.post(f"/api/lorebooks/{orig_lb['id']}/duplicate")
        assert dup_resp.status_code == 201, dup_resp.text
        copy = dup_resp.json()

        assert copy["id"] != orig_lb["id"]
        assert copy["name"] == "Eldoria Compendium (Copy)"
        assert copy["is_active"] is False
        assert copy["is_public"] is False

        for field in (
            "description",
            "llm_instructions",
            "tag",
            "run_pre_driver",
            "run_driver_callable",
            "run_pre_navigator",
            "run_post_navigator",
            "inject_after_cantrips",
            "scan_depth",
            "word_boundary_keys",
            "budget_weight",
            "reserved_tokens",
        ):
            assert copy[field] == orig_lb[field], f"Mismatch on field {field}"

        assert len(copy["entries"]) == 2
        copy_entry_ids = [e["id"] for e in copy["entries"]]
        assert set(copy_entry_ids).isdisjoint(set(orig_entry_ids))
        assert [e["name"] for e in copy["entries"]] == ["Silver Citadel", "Guild of Scribes"]
        assert copy["entries"][0]["keys"] == ["citadel", "silver"]
        assert copy["entries"][0]["secondary_keys"] == ["north"]
        assert copy["entries"][0]["is_constant"] is True
        assert copy["entries"][1]["is_disabled"] is True


class TestDuplicateMap:
    @pytest.mark.asyncio
    async def test_duplicate_map_deep_copies_stages_and_resources(self, admin_client):
        client, _, _ = admin_client

        ep_resp = await client.post(
            "/api/endpoints",
            json={"name": "Map EP", "base_url": "https://api.example.com", "api_key": "sk-map"},
        )
        assert ep_resp.status_code == 201
        ep_id = ep_resp.json()["id"]

        lb_resp = await client.post("/api/lorebooks", json={"name": "Map LB"})
        assert lb_resp.status_code == 201
        lb_id = lb_resp.json()["id"]

        c_resp = await client.post("/api/cantrips", json={"name": "Map Cantrip", "code": ""})
        assert c_resp.status_code == 201
        c_id = c_resp.json()["id"]

        create_resp = await client.post(
            "/api/maps",
            json={
                "name": "Story Director",
                "description": "Two-stage narrative pipeline",
                "tag": "director",
                "is_public": True,
                "is_active": True,
                "version": "2.3",
                "author": "Michael",
                "global_llm_instructions": "Maintain consistent voice.",
                "final_verification_enabled": True,
                "stages": [
                    {
                        "name": "Planner",
                        "description": "Outline beat",
                        "system_instructions": "Plan the next scene.",
                        "endpoint_id": ep_id,
                        "endpoint_tag": "fast",
                        "model_override": "gpt-4o-mini",
                        "driver_callable_turns": 2,
                        "verification_enabled": True,
                        "verification_endpoint_id": ep_id,
                        "verification_model": "gpt-4o",
                        "verification_max_retries": 3,
                        "verification_instructions": "Check continuity.",
                        "output_mode": "capture",
                        "base_prompt_mode": "blank",
                        "condition_json": json.dumps({"path": "world.day", "op": ">=", "value": 1}),
                        "history_window": 4,
                        "parameters": [{"name": "temperature", "value": 0.5, "enabled": True}],
                        "verification_parameters": [{"name": "temperature", "value": 0.1, "enabled": True}],
                        "resources": [
                            {"resource_type": "lorebook", "resource_id": lb_id, "position": "pre_driver", "sticky": True},
                            {"resource_type": "cantrip", "resource_id": c_id, "position": "post_navigator", "sticky": False},
                        ],
                    },
                    {
                        "name": "Prose",
                        "description": "Write final reply",
                        "system_instructions": "Write vivid prose.",
                        "output_mode": "persist",
                        "base_prompt_mode": "full",
                        "resources": [],
                    },
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        orig = create_resp.json()

        dup_resp = await client.post(f"/api/maps/{orig['id']}/duplicate")
        assert dup_resp.status_code == 201, dup_resp.text
        copy = dup_resp.json()

        assert copy["id"] != orig["id"]
        assert copy["name"] == "Story Director (Copy)"
        assert copy["is_active"] is False
        assert copy["is_public"] is False

        for field in (
            "description",
            "tag",
            "version",
            "author",
            "global_llm_instructions",
            "final_verification_enabled",
        ):
            assert copy[field] == orig[field], f"Mismatch on field {field}"

        assert len(copy["stages"]) == 2
        assert {s["id"] for s in copy["stages"]}.isdisjoint({s["id"] for s in orig["stages"]})
        assert [s["stage_order"] for s in copy["stages"]] == [1, 2]
        assert [s["name"] for s in copy["stages"]] == ["Planner", "Prose"]

        orig_s1, copy_s1 = orig["stages"][0], copy["stages"][0]
        for field in (
            "description",
            "system_instructions",
            "endpoint_id",
            "endpoint_tag",
            "model_override",
            "driver_callable_turns",
            "verification_enabled",
            "verification_endpoint_id",
            "verification_model",
            "verification_max_retries",
            "verification_instructions",
            "output_mode",
            "base_prompt_mode",
            "condition_json",
            "history_window",
            "parameters",
            "verification_parameters",
        ):
            assert copy_s1[field] == orig_s1[field], f"Mismatch on stage field {field}"

        assert len(copy_s1["resources"]) == 2
        assert {r["id"] for r in copy_s1["resources"]}.isdisjoint({r["id"] for r in orig_s1["resources"]})
        assert [
            (r["resource_type"], r["resource_id"], r["position"], r["sticky"])
            for r in copy_s1["resources"]
        ] == [
            (r["resource_type"], r["resource_id"], r["position"], r["sticky"])
            for r in orig_s1["resources"]
        ]


class TestNameTruncation:
    @pytest.mark.asyncio
    async def test_128_char_names_truncate_safely_across_all_resources(self, admin_client):
        client, _, _ = admin_client
        long_name = "A" * 128

        ep = await client.post("/api/endpoints", json={"name": long_name, "base_url": "https://ep.test"})
        assert ep.status_code == 201
        ep_dup = await client.post(f"/api/endpoints/{ep.json()['id']}/duplicate")
        assert ep_dup.status_code == 201
        assert len(ep_dup.json()["name"]) == 128
        assert ep_dup.json()["name"].endswith(" (Copy)")

        c = await client.post("/api/cantrips", json={"name": long_name, "code": ""})
        assert c.status_code == 201
        c_dup = await client.post(f"/api/cantrips/{c.json()['id']}/duplicate")
        assert c_dup.status_code == 201
        assert len(c_dup.json()["name"]) == 128
        assert c_dup.json()["name"].endswith(" (Copy)")

        lb = await client.post("/api/lorebooks", json={"name": long_name})
        assert lb.status_code == 201
        lb_dup = await client.post(f"/api/lorebooks/{lb.json()['id']}/duplicate")
        assert lb_dup.status_code == 201
        assert len(lb_dup.json()["name"]) == 128
        assert lb_dup.json()["name"].endswith(" (Copy)")

        mp = await client.post("/api/maps", json={"name": long_name})
        assert mp.status_code == 201
        mp_dup = await client.post(f"/api/maps/{mp.json()['id']}/duplicate")
        assert mp_dup.status_code == 201
        assert len(mp_dup.json()["name"]) == 128
        assert mp_dup.json()["name"].endswith(" (Copy)")


class TestRule23CrossUserDuplicate404:
    @pytest.mark.asyncio
    async def test_foreign_user_gets_404_on_all_four_duplicate_routes(self, admin_client):
        client, _, _ = admin_client

        # User A creates all four resources (including public ones to verify Rule 23 strict ownership)
        ep_id = (
            await client.post("/api/endpoints", json={"name": "UserA EP", "base_url": "https://a.test"})
        ).json()["id"]
        cantrip_id = (
            await client.post("/api/cantrips", json={"name": "UserA Cantrip", "code": "", "is_public": True})
        ).json()["id"]
        lorebook_id = (
            await client.post("/api/lorebooks", json={"name": "UserA LB", "is_public": True})
        ).json()["id"]
        map_id = (
            await client.post("/api/maps", json={"name": "UserA Map", "is_public": True})
        ).json()["id"]

        user_b_headers = await _create_second_user_headers(client)

        for url in (
            f"/api/endpoints/{ep_id}/duplicate",
            f"/api/cantrips/{cantrip_id}/duplicate",
            f"/api/lorebooks/{lorebook_id}/duplicate",
            f"/api/maps/{map_id}/duplicate",
        ):
            resp = await client.post(url, headers=user_b_headers)
            assert resp.status_code == 404, f"Expected 404 for foreign duplicate on {url}, got {resp.status_code}"

        # Non-existent IDs also return 404
        for url in (
            "/api/endpoints/nonexistent-id/duplicate",
            "/api/cantrips/nonexistent-id/duplicate",
            "/api/lorebooks/nonexistent-id/duplicate",
            "/api/maps/nonexistent-id/duplicate",
        ):
            resp = await client.post(url)
            assert resp.status_code == 404, f"Expected 404 for missing id on {url}, got {resp.status_code}"


class TestCantripTesterUserDataPrefixScoping:
    @pytest.mark.asyncio
    async def test_stored_cantrip_tester_scopes_explicit_user_data(self, admin_client):
        client, _, _ = admin_client
        code = (
            'const hp = context.user_data.get("rpg.hp");\n'
            'const secret = context.user_data.get("secret.token");\n'
            'const keys = context.user_data.keys().sort();\n'
            'context.chat_data.set("hp", hp);\n'
            'context.chat_data.set("secret", secret ?? "HIDDEN");\n'
            'context.chat_data.set("keys", keys);\n'
        )
        created = await client.post(
            "/api/cantrips",
            json={
                "name": "Scoped RPG Cantrip",
                "code": code,
                "user_data_prefixes": ["rpg."],
            },
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        resp = await client.post(
            f"/api/cantrips/{cid}/test",
            json={
                "user_data": {
                    "rpg.hp": 85,
                    "rpg.mp": 40,
                    "secret.token": "do-not-leak",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["chat_data"]["hp"] == 85
        assert body["chat_data"]["secret"] == "HIDDEN"
        assert body["chat_data"]["keys"] == ["rpg.hp", "rpg.mp"]

    @pytest.mark.asyncio
    async def test_stored_cantrip_tester_scopes_persisted_db_user_data(self, admin_client):
        client, _, _ = admin_client
        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        user_id = me.json()["id"]

        async with TestSessionLocal() as db:
            db.add(UserData(user_id=user_id, key="rpg.level", value_json=json.dumps(7)))
            db.add(UserData(user_id=user_id, key="secret.token", value_json=json.dumps("top-secret")))
            await db.commit()

        code = (
            'context.chat_data.set("keys", context.user_data.keys().sort());\n'
            'context.chat_data.set("level", context.user_data.get("rpg.level"));\n'
            'context.chat_data.set("secret", context.user_data.get("secret.token") ?? "HIDDEN");\n'
        )
        created = await client.post(
            "/api/cantrips",
            json={
                "name": "Scoped DB Reader",
                "code": code,
                "user_data_prefixes": ["rpg."],
            },
        )
        assert created.status_code == 201
        cid = created.json()["id"]

        # Omit user_data in request so tester loads from DB and applies cantrip.user_data_prefixes
        resp = await client.post(f"/api/cantrips/{cid}/test", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["chat_data"]["level"] == 7
        assert body["chat_data"]["secret"] == "HIDDEN"
        assert body["chat_data"]["keys"] == ["rpg.level"]

    @pytest.mark.asyncio
    async def test_standalone_cantrip_tester_respects_user_data_prefixes(self, admin_client):
        client, _, _ = admin_client
        code = (
            'context.chat_data.set("keys", context.user_data.keys().sort());\n'
            'context.chat_data.set("secret", context.user_data.get("secret.token") ?? "HIDDEN");\n'
        )
        resp = await client.post(
            "/api/cantrips/test",
            json={
                "code": code,
                "user_data": {"rpg.gold": 120, "secret.token": "nope"},
                "user_data_prefixes": ["rpg."],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["chat_data"]["keys"] == ["rpg.gold"]
        assert body["chat_data"]["secret"] == "HIDDEN"
