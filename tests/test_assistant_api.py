"""The `/api/assistant` router (Phase 26b).

Conversation CRUD, config, the permission grid, export and fork. Two
properties matter beyond the CRUD: the assistant's own configuration is not
reachable through `/api/settings` (so `update_settings` cannot widen it), and
every conversation route is scoped to its owner.
"""

from __future__ import annotations

import json

import pytest

MOCK_URL = "https://api-upstream.test"


async def _endpoint(client, name="EP", base_url=MOCK_URL, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    resp = await client.post(
        "/api/endpoints",
        json={"name": name, "base_url": base_url, "api_key": "sk-api-test", "default_model": "m"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _second_user(client):
    created = await client.post(
        "/api/users", json={"username": "other", "password": "otherpass123", "is_admin": False}
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post(
        "/api/auth/login", json={"username": "other", "password": "otherpass123"}
    )
    assert login.status_code == 200
    return login.json()["access_token"]


class TestConfig:
    @pytest.mark.asyncio
    async def test_defaults(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/assistant/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["endpoint_id"] is None
        assert body["model"] == ""
        assert body["enabled"] is True
        assert body["context_tokens"] == 64000

    @pytest.mark.asyncio
    async def test_put_and_read_back(self, admin_client):
        client, _, _ = admin_client
        endpoint_id = await _endpoint(client)
        resp = await client.put(
            "/api/assistant/config",
            json={
                "endpoint_id": endpoint_id,
                "model": "chosen-model",
                "context_tokens": 32000,
                "parameters": [
                    {"name": "temperature", "type": "float", "value": 0.2, "description": ""}
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["endpoint_id"] == endpoint_id
        assert body["model"] == "chosen-model"
        assert body["context_tokens"] == 32000
        assert body["parameters"][0]["name"] == "temperature"
        assert body["endpoint_name"] == "EP"

    @pytest.mark.asyncio
    async def test_role_tag_is_reported(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/endpoints",
            json={
                "name": "Tagged",
                "base_url": MOCK_URL,
                "api_key": "sk-x",
                "role_tag": "tool_use",
            },
        )
        await client.put("/api/assistant/config", json={"endpoint_id": resp.json()["id"]})
        body = (await client.get("/api/assistant/config")).json()
        assert body["endpoint_role_tag"] == "tool_use"

    @pytest.mark.asyncio
    async def test_another_users_endpoint_is_rejected(self, admin_client):
        client, _, _ = admin_client
        other_token = await _second_user(client)
        theirs = await _endpoint(client, name="Theirs", token=other_token)

        resp = await client.put("/api/assistant/config", json={"endpoint_id": theirs})
        assert resp.status_code == 400
        assert (await client.get("/api/assistant/config")).json()["endpoint_id"] is None

    @pytest.mark.asyncio
    async def test_unknown_endpoint_is_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/assistant/config", json={"endpoint_id": "no-such-id"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_settings_route_carries_no_assistant_fields(self, admin_client):
        """`update_settings` must not be able to see the assistant's config."""
        client, _, _ = admin_client
        body = (await client.get("/api/settings")).json()
        assert not [key for key in body if key.startswith("assistant")]

        endpoint_id = await _endpoint(client)
        resp = await client.put(
            "/api/settings", json={"assistant_endpoint_id": endpoint_id, "default_model": "m"}
        )
        assert resp.status_code == 200
        assert (await client.get("/api/assistant/config")).json()["endpoint_id"] is None


class TestPermissions:
    @pytest.mark.asyncio
    async def test_grid_shape(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/assistant/permissions")
        assert resp.status_code == 200
        groups = {g["key"]: g for g in resp.json()["groups"]}

        assert set(groups) >= {"cantrips", "packs", "selfcheck", "admin_reads"}
        cantrips = groups["cantrips"]
        assert cantrips["category"] == "content"
        assert cantrips["mode"] == "normal"
        assert cantrips["disabled_by_admin"] is False
        by_name = {t["name"]: t for t in cantrips["tools"]}
        assert by_name["list_cantrips"]["effective"] == "allow"
        assert by_name["delete_cantrip"]["effective"] == "ask"
        assert by_name["delete_cantrip"]["mode"] == "inherit"

    @pytest.mark.asyncio
    async def test_admin_gated_groups_report_disabled(self, admin_client):
        client, _, _ = admin_client
        groups = {g["key"]: g for g in (await client.get("/api/assistant/permissions")).json()["groups"]}
        assert groups["packs"]["disabled_by_admin"] is True
        assert groups["packs"]["default_mode"] == "deny"
        assert groups["admin_reads"]["disabled_by_admin"] is True

    @pytest.mark.asyncio
    async def test_empty_groups_still_appear(self, admin_client):
        # admin_reads ships tools from 26d on, but they stay invisible -- and so
        # the group stays empty here -- until assistant_admin_reads_enabled is
        # turned on, which this admin client has not done.
        client, _, _ = admin_client
        groups = {g["key"]: g for g in (await client.get("/api/assistant/permissions")).json()["groups"]}
        assert groups["admin_reads"]["tools"] == []

    @pytest.mark.asyncio
    async def test_put_updates_effective_modes(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put(
            "/api/assistant/permissions",
            json={"groups": {"maps": "deny"}, "tools": {"delete_cantrip": "always_allow"}},
        )
        assert resp.status_code == 200
        groups = {g["key"]: g for g in resp.json()["groups"]}
        assert groups["maps"]["mode"] == "deny"
        assert all(t["effective"] == "deny" for t in groups["maps"]["tools"])
        cantrips = {t["name"]: t for t in groups["cantrips"]["tools"]}
        assert cantrips["delete_cantrip"]["effective"] == "allow"

        # And it survives a re-read.
        reread = {g["key"]: g for g in (await client.get("/api/assistant/permissions")).json()["groups"]}
        assert reread["maps"]["mode"] == "deny"

    @pytest.mark.asyncio
    async def test_unknown_group_is_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/assistant/permissions", json={"groups": {"nope": "deny"}})
        assert resp.status_code == 422
        assert "unknown group" in json.dumps(resp.json())

    @pytest.mark.asyncio
    async def test_unknown_tool_is_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/assistant/permissions", json={"tools": {"nope": "deny"}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_mode_is_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/assistant/permissions", json={"groups": {"maps": "banana"}})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_a_rejected_put_changes_nothing(self, admin_client):
        client, _, _ = admin_client
        await client.put("/api/assistant/permissions", json={"groups": {"maps": "deny"}})
        await client.put("/api/assistant/permissions", json={"groups": {"nope": "deny"}})
        groups = {g["key"]: g for g in (await client.get("/api/assistant/permissions")).json()["groups"]}
        assert groups["maps"]["mode"] == "deny"

    @pytest.mark.asyncio
    async def test_permissions_are_per_user(self, admin_client):
        client, _, _ = admin_client
        other_token = await _second_user(client)
        await client.put("/api/assistant/permissions", json={"groups": {"maps": "deny"}})

        theirs = await client.get(
            "/api/assistant/permissions", headers={"Authorization": f"Bearer {other_token}"}
        )
        groups = {g["key"]: g for g in theirs.json()["groups"]}
        assert groups["maps"]["mode"] == "normal"


class TestCatalog:
    @pytest.mark.asyncio
    async def test_catalog_lists_visible_tools_with_schemas(self, admin_client):
        client, _, _ = admin_client
        body = (await client.get("/api/assistant/catalog")).json()
        names = {t["name"] for t in body["tools"]}
        assert "list_cantrips" in names
        assert "link_repo" not in names  # packs gated off by default

        entry = next(t for t in body["tools"] if t["name"] == "get_cantrip")
        assert entry["schema"]["function"]["parameters"]["properties"]["cantrip_id"]


class TestConversations:
    @pytest.mark.asyncio
    async def test_create_list_patch_delete(self, admin_client):
        client, _, _ = admin_client
        created = await client.post("/api/assistant/conversations")
        assert created.status_code == 201
        conversation_id = created.json()["id"]
        assert created.json()["rotated_title"] is None

        listed = (await client.get("/api/assistant/conversations")).json()["conversations"]
        assert [c["id"] for c in listed] == [conversation_id]
        assert "messages" not in listed[0]

        patched = await client.patch(
            f"/api/assistant/conversations/{conversation_id}",
            json={"title": "Renamed", "yolo": True},
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "Renamed"
        assert patched.json()["yolo"] is True

        deleted = await client.delete(f"/api/assistant/conversations/{conversation_id}")
        assert deleted.status_code == 204
        assert (await client.get(f"/api/assistant/conversations/{conversation_id}")).status_code == 404

    @pytest.mark.asyncio
    async def test_save_and_unsave(self, admin_client):
        client, _, _ = admin_client
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]

        saved = await client.post(f"/api/assistant/conversations/{conversation_id}/save")
        assert saved.status_code == 200
        assert saved.json()["saved"] is True

        unsaved = await client.delete(f"/api/assistant/conversations/{conversation_id}/save")
        assert unsaved.status_code == 200
        assert unsaved.json()["saved"] is False

    @pytest.mark.asyncio
    async def test_rotation_notice_names_the_evicted_conversation(self, admin_client):
        client, _, _ = admin_client
        from app.services.admin import get_admin_settings, update_admin_settings

        original = (await get_admin_settings()).max_assistant_conversations
        try:
            await update_admin_settings({"max_assistant_conversations": 2})
            first = (await client.post("/api/assistant/conversations")).json()["id"]
            await client.patch(f"/api/assistant/conversations/{first}", json={"title": "Oldest"})
            await client.post("/api/assistant/conversations")

            third = await client.post("/api/assistant/conversations")
            assert third.json()["rotated_title"] == "Oldest"
            remaining = (await client.get("/api/assistant/conversations")).json()["conversations"]
            assert first not in [c["id"] for c in remaining]
        finally:
            await update_admin_settings({"max_assistant_conversations": original})

    @pytest.mark.asyncio
    async def test_cross_user_access_is_404_everywhere(self, admin_client):
        client, _, _ = admin_client
        mine = (await client.post("/api/assistant/conversations")).json()["id"]
        other_token = await _second_user(client)
        headers = {"Authorization": f"Bearer {other_token}"}

        assert (await client.get(f"/api/assistant/conversations/{mine}", headers=headers)).status_code == 404
        assert (await client.patch(f"/api/assistant/conversations/{mine}", json={"title": "x"}, headers=headers)).status_code == 404
        assert (await client.delete(f"/api/assistant/conversations/{mine}", headers=headers)).status_code == 404
        assert (await client.post(f"/api/assistant/conversations/{mine}/save", headers=headers)).status_code == 404
        assert (await client.get(f"/api/assistant/conversations/{mine}/export", headers=headers)).status_code == 404
        assert (await client.post(f"/api/assistant/conversations/{mine}/fork", json={"summary": "s"}, headers=headers)).status_code == 404

        # And it is still there for its owner.
        assert (await client.get(f"/api/assistant/conversations/{mine}")).status_code == 200

    @pytest.mark.asyncio
    async def test_listing_is_scoped_to_the_caller(self, admin_client):
        client, _, _ = admin_client
        mine = (await client.post("/api/assistant/conversations")).json()["id"]
        other_token = await _second_user(client)
        theirs = (
            await client.post(
                "/api/assistant/conversations", headers={"Authorization": f"Bearer {other_token}"}
            )
        ).json()["id"]

        listed = (await client.get("/api/assistant/conversations")).json()["conversations"]
        ids = [c["id"] for c in listed]
        assert mine in ids
        assert theirs not in ids


class TestExportAndFork:
    @pytest.mark.asyncio
    async def test_json_export(self, admin_client):
        client, _, _ = admin_client
        conversation_id = await _seeded(client)
        resp = await client.get(f"/api/assistant/conversations/{conversation_id}/export?format=json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "attachment" in resp.headers["content-disposition"]

        payload = json.loads(resp.text)
        assert payload["kind"] == "assistant_conversation"
        assert payload["conversation"]["messages"][0]["content"] == "hello there"

    @pytest.mark.asyncio
    async def test_markdown_export(self, admin_client):
        client, _, _ = admin_client
        conversation_id = await _seeded(client)
        resp = await client.get(
            f"/api/assistant/conversations/{conversation_id}/export?format=markdown"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        body = resp.text
        assert body.startswith("# ")
        assert "### user" in body
        assert "hello there" in body
        assert "```json" in body  # the tool result is fenced

    @pytest.mark.asyncio
    async def test_export_defaults_to_json(self, admin_client):
        client, _, _ = admin_client
        conversation_id = await _seeded(client)
        resp = await client.get(f"/api/assistant/conversations/{conversation_id}/export")
        assert json.loads(resp.text)["kind"] == "assistant_conversation"

    @pytest.mark.asyncio
    async def test_fork_seeds_a_new_conversation_and_keeps_the_original(self, admin_client):
        client, _, _ = admin_client
        conversation_id = await _seeded(client)
        await client.patch(f"/api/assistant/conversations/{conversation_id}", json={"title": "Original"})

        resp = await client.post(
            f"/api/assistant/conversations/{conversation_id}/fork",
            json={"summary": "- decided to keep Alpha"},
        )
        assert resp.status_code == 201
        forked = resp.json()
        assert forked["id"] != conversation_id
        assert forked["title"] == "Original (continued)"
        assert forked["messages"][0]["role"] == "system"
        assert "decided to keep Alpha" in forked["messages"][0]["content"]

        assert (await client.get(f"/api/assistant/conversations/{conversation_id}")).status_code == 200


class TestSSERoutes:
    @pytest.mark.asyncio
    async def test_message_is_rejected_when_the_admin_disables_the_assistant(self, admin_client):
        client, _, _ = admin_client
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]
        from app.services.admin import get_admin_settings, update_admin_settings

        original = (await get_admin_settings()).assistant_enabled
        try:
            await update_admin_settings({"assistant_enabled": False})
            resp = await client.post(
                f"/api/assistant/conversations/{conversation_id}/message", json={"content": "hi"}
            )
            assert resp.status_code == 403
            resume = await client.post(
                f"/api/assistant/conversations/{conversation_id}/resume", json={"call_id": "c1"}
            )
            assert resume.status_code == 403
        finally:
            await update_admin_settings({"assistant_enabled": original})

    @pytest.mark.asyncio
    async def test_oversized_message_is_413(self, admin_client):
        client, _, _ = admin_client
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]
        resp = await client.post(
            f"/api/assistant/conversations/{conversation_id}/message",
            json={"content": "x" * (64 * 1024 + 1)},
        )
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_stream_content_type_and_frame_shape(self, admin_client):
        client, _, _ = admin_client
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]
        resp = await client.post(
            f"/api/assistant/conversations/{conversation_id}/message", json={"content": "hi"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        blocks = [b for b in resp.text.split("\n\n") if b.strip()]
        assert blocks
        for block in blocks:
            lines = block.splitlines()
            assert lines[0].startswith("event: ")
            assert lines[1].startswith("data: ")
            json.loads(lines[1][len("data: "):])

        # No endpoint is configured, so this is the frame the pane must see.
        assert '"code": "no_endpoint"' in resp.text

    @pytest.mark.asyncio
    async def test_resume_without_a_pending_call_is_reported(self, admin_client):
        client, _, _ = admin_client
        await _endpoint(client)
        endpoint_id = (await client.get("/api/endpoints")).json()["endpoints"][0]["id"]
        await client.put("/api/assistant/config", json={"endpoint_id": endpoint_id, "model": "m"})
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]

        resp = await client.post(
            f"/api/assistant/conversations/{conversation_id}/resume",
            json={"call_id": "c1", "decision": "approve"},
        )
        assert resp.status_code == 200
        assert '"code": "no_pending"' in resp.text


async def _seeded(client) -> str:
    """A conversation with a transcript, written through the store."""
    from app.services.assistant import store

    me = (await client.get("/api/auth/me")).json()["id"]
    row, _ = await store.create(me, 20)
    await store.save_state(
        row.id,
        messages=[
            {"role": "user", "content": "hello there"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "list_cantrips", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "list_cantrips",
                "content": json.dumps({"cantrips": []}),
            },
            {"role": "assistant", "content": "You have none."},
        ],
        title="Seeded",
    )
    return row.id
