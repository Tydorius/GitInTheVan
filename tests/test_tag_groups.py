"""Tests for tag groups: API CRUD, group resolution, and pipeline integration."""

import json

import pytest

from app.services.group_resolver import resolve_group_tags

MOCK_ENDPOINT_URL = "http://mock-backend:9999"


@pytest.fixture(autouse=True)
def set_endpoint(monkeypatch):
    from app.config import Settings
    test_settings = Settings(
        default_endpoint_url=MOCK_ENDPOINT_URL,
        default_endpoint_api_key="sk-test-key",
        default_endpoint_model="test-model",
        default_endpoint_api_base_path="",
    )
    monkeypatch.setattr("app.services.proxy.settings", test_settings)


def _mock_response(content: str = "Hi there!", model: str = "test-model"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# ============================================================================
# API: CRUD
# ============================================================================

class TestTagGroupAPI:
    @pytest.mark.asyncio
    async def test_list_empty(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/tag-groups")
        assert resp.status_code == 200
        assert resp.json()["groups"] == []

    @pytest.mark.asyncio
    async def test_create_group(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/tag-groups", json={
            "name": "Test Group",
            "tag": "testgroup",
            "is_active": False,
            "members": [],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Group"
        assert data["tag"] == "testgroup"
        assert data["is_active"] is False
        assert data["members"] == []

    @pytest.mark.asyncio
    async def test_create_group_with_members(self, admin_client):
        client, _, _ = admin_client

        lb_resp = await client.post("/api/lorebooks", json={"name": "LB1"})
        lb_id = lb_resp.json()["id"]

        ct_resp = await client.post("/api/cantrips", json={
            "name": "CT1",
            "code": "console.log('hi')",
        })
        ct_id = ct_resp.json()["id"]

        resp = await client.post("/api/tag-groups", json={
            "name": "Group With Members",
            "tag": "gwm",
            "members": [
                {"member_type": "lorebook", "member_id": lb_id},
                {"member_type": "cantrip", "member_id": ct_id},
            ],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["members"]) == 2

    @pytest.mark.asyncio
    async def test_get_group(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/tag-groups", json={"name": "GetMe", "tag": "gm"})
        gid = create.json()["id"]

        resp = await client.get(f"/api/tag-groups/{gid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "GetMe"

    @pytest.mark.asyncio
    async def test_get_not_found(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/tag-groups/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_group(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/tag-groups", json={"name": "Old", "tag": "old"})
        gid = create.json()["id"]

        resp = await client.put(f"/api/tag-groups/{gid}", json={"name": "New", "is_active": True})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["is_active"] is True

    @pytest.mark.asyncio
    async def test_delete_group(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/tag-groups", json={"name": "DeleteMe"})
        gid = create.json()["id"]

        resp = await client.delete(f"/api/tag-groups/{gid}")
        assert resp.status_code == 204

        get_resp = await client.get(f"/api/tag-groups/{gid}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_members(self, admin_client):
        client, _, _ = admin_client
        lb_resp = await client.post("/api/lorebooks", json={"name": "LB1"})
        lb_id = lb_resp.json()["id"]

        create = await client.post("/api/tag-groups", json={"name": "MembersTest"})
        gid = create.json()["id"]

        resp = await client.put(f"/api/tag-groups/{gid}/members", json={
            "members": [{"member_type": "lorebook", "member_id": lb_id}]
        })
        assert resp.status_code == 200
        assert len(resp.json()["members"]) == 1
        assert resp.json()["members"][0]["member_type"] == "lorebook"


# ============================================================================
# Group Resolver: tag expansion
# ============================================================================

class TestGroupResolver:
    @pytest.mark.asyncio
    async def test_no_groups(self, admin_client):
        """No groups -> tags unchanged."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        async with TestSessionLocal() as db:
            expanded, activated = await resolve_group_tags(db, user_id, [
                {"type": "lore", "name": "foo", "owner": None, "raw": "lore-foo"}
            ])
        assert len(expanded) == 1
        assert activated == []

    @pytest.mark.asyncio
    async def test_active_group_expands_tags(self, admin_client):
        """Active group (is_active=True) injects member tags."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        lb_resp = await client.post("/api/lorebooks", json={"name": "LB1", "tag": "lbtag1"})
        lb_id = lb_resp.json()["id"]

        ct_resp = await client.post("/api/cantrips", json={
            "name": "CT1", "tag": "cttag1", "code": "1",
        })
        ct_id = ct_resp.json()["id"]

        await client.post("/api/tag-groups", json={
            "name": "Active",
            "tag": "activegrp",
            "is_active": True,
            "members": [
                {"member_type": "lorebook", "member_id": lb_id},
                {"member_type": "cantrip", "member_id": ct_id},
            ],
        })

        async with TestSessionLocal() as db:
            expanded, activated = await resolve_group_tags(db, user_id, [])

        tag_names = {(t["type"], t["name"]) for t in expanded}
        assert ("lore", "lbtag1") in tag_names
        assert ("cantrip", "cttag1") in tag_names
        assert activated == ["Active"]

    @pytest.mark.asyncio
    async def test_tag_activated_group(self, admin_client):
        """Group activated by <#groupname#> tag expands member tags."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        lb_resp = await client.post("/api/lorebooks", json={"name": "LB", "tag": "mylb"})
        lb_id = lb_resp.json()["id"]

        await client.post("/api/tag-groups", json={
            "name": "TagActivated",
            "tag": "mygroup",
            "is_active": False,
            "members": [{"member_type": "lorebook", "member_id": lb_id}],
        })

        tags = [{"type": "taggroup", "name": "mygroup", "owner": None, "raw": "mygroup"}]
        async with TestSessionLocal() as db:
            expanded, activated = await resolve_group_tags(db, user_id, tags)

        assert ("lore", "mylb") in {(t["type"], t["name"]) for t in expanded}
        assert activated == ["TagActivated"]

    @pytest.mark.asyncio
    async def test_inactive_group_no_tag_no_activation(self, admin_client):
        """Inactive group without matching tag -> no expansion."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        await client.post("/api/lorebooks", json={"name": "LB", "tag": "sometag"})
        await client.post("/api/tag-groups", json={
            "name": "Inactive",
            "tag": "inactivegrp",
            "is_active": False,
            "members": [],
        })

        async with TestSessionLocal() as db:
            expanded, activated = await resolve_group_tags(db, user_id, [])

        assert expanded == []
        assert activated == []

    @pytest.mark.asyncio
    async def test_missing_member_skipped(self, admin_client):
        """Missing lorebook/cantrip is silently skipped with a log warning."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        await client.post("/api/tag-groups", json={
            "name": "MissingMember",
            "tag": "missing",
            "is_active": True,
            "members": [
                {"member_type": "lorebook", "member_id": "nonexistent-lb-id"},
            ],
        })

        async with TestSessionLocal() as db:
            expanded, activated = await resolve_group_tags(db, user_id, [])

        assert expanded == []
        assert activated == ["MissingMember"]

    @pytest.mark.asyncio
    async def test_deduplication(self, admin_client):
        """Resource called via own tag + group tag only appears once."""
        from tests.conftest import TestSessionLocal
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()
        user_id = me["id"]

        lb_resp = await client.post("/api/lorebooks", json={"name": "LB", "tag": "dupe"})
        lb_id = lb_resp.json()["id"]

        await client.post("/api/tag-groups", json={
            "name": "Duper",
            "tag": "dupergroup",
            "is_active": True,
            "members": [{"member_type": "lorebook", "member_id": lb_id}],
        })

        tags = [{"type": "lore", "name": "dupe", "owner": None, "raw": "lore-dupe"}]
        async with TestSessionLocal() as db:
            expanded, _ = await resolve_group_tags(db, user_id, tags)

        lore_dupe_tags = [t for t in expanded if t["type"] == "lore" and t["name"] == "dupe"]
        assert len(lore_dupe_tags) == 1


# ============================================================================
# Pipeline integration
# ============================================================================

class TestPipelineIntegration:
    @pytest.mark.asyncio
    async def test_group_activates_members_in_proxy(self, admin_client, httpx_mock):
        """Active group causes lorebook injection in proxy pipeline."""
        client, _, gitv_key = admin_client

        await client.post("/api/endpoints", json={
            "name": "Test",
            "base_url": MOCK_ENDPOINT_URL,
            "api_key": "sk-test",
        })

        lb_resp = await client.post("/api/lorebooks", json={
            "name": "World Lore",
            "tag": "worldlore",
            "is_active": False,
        })
        lb_id = lb_resp.json()["id"]

        await client.post(f"/api/lorebooks/{lb_id}/entries", json={
            "name": "Entry1",
            "keys": ["test"],
            "content": "INJECTED_BY_GROUP",
            "position": "before_last_message",
            "insertion_order": 10,
            "is_constant": False,
            "is_selective": False,
        })

        await client.post("/api/tag-groups", json={
            "name": "Pipeline Test",
            "tag": "pipetest",
            "is_active": True,
            "members": [{"member_type": "lorebook", "member_id": lb_id}],
        })

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_response("response"),
            status_code=200,
        )

        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "test message"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {gitv_key}"},
        )
        assert resp.status_code == 200

        forwarded = httpx_mock.get_requests()[-1]
        body = json.loads(forwarded.content)
        full_text = json.dumps(body["messages"])
        assert "INJECTED_BY_GROUP" in full_text

    @pytest.mark.asyncio
    async def test_group_tag_in_message_activates(self, admin_client, httpx_mock):
        """<#grouptag#> in message activates member lorebooks."""
        client, _, gitv_key = admin_client

        await client.post("/api/endpoints", json={
            "name": "Test2",
            "base_url": MOCK_ENDPOINT_URL,
            "api_key": "sk-test",
        })

        lb_resp = await client.post("/api/lorebooks", json={
            "name": "Scene Lore",
            "tag": "scenelore",
            "is_active": False,
        })
        lb_id = lb_resp.json()["id"]

        await client.post(f"/api/lorebooks/{lb_id}/entries", json={
            "name": "SceneEntry",
            "keys": ["hello"],
            "content": "SCENE_BY_TAG",
            "position": "before_last_message",
            "insertion_order": 10,
            "is_constant": False,
            "is_selective": False,
        })

        await client.post("/api/tag-groups", json={
            "name": "Scene",
            "tag": "myscene",
            "is_active": False,
            "members": [{"member_type": "lorebook", "member_id": lb_id}],
        })

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_response("ok"),
            status_code=200,
        )

        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hello <#myscene#>"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {gitv_key}"},
        )
        assert resp.status_code == 200

        forwarded = httpx_mock.get_requests()[-1]
        body = json.loads(forwarded.content)
        full_text = json.dumps(body["messages"])
        assert "SCENE_BY_TAG" in full_text
