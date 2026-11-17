"""Per-object export endpoints (Phase 28).

Cantrips, skills, verification rules and memory rules could be snapshotted but
never downloaded. Each test asserts the content that matters for round-tripping
plus one ownership case -- the export must not become a cross-user read.
"""

import pytest

VALID_CANTRIP = "export default function(context) { return context }"


@pytest.mark.asyncio
class TestObjectExport:
    async def _setup(self, client):
        """Create one of each of the four exportable types, owned by `client`."""
        cantrip = await client.post(
            "/api/cantrips",
            json={"name": "Dice Roller", "code": VALID_CANTRIP, "llm_instructions": "Roll dice."},
        )
        assert cantrip.status_code == 201, cantrip.text
        skill = await client.post(
            "/api/skills",
            json={"name": "Fencing", "description": "How to fence", "content": "Parry.", "type": "skill"},
        )
        assert skill.status_code == 201, skill.text
        rule = await client.post(
            "/api/verification/rules",
            json={"name": "Stay in character", "prompt": "Never speak as the user.", "max_retries": 1},
        )
        assert rule.status_code in (200, 201), rule.text
        memory = await client.post(
            "/api/memory-rules",
            json={"name": "Battle chat", "prompt": "Track the battle.", "token_threshold": 4000},
        )
        assert memory.status_code in (200, 201), memory.text
        return cantrip.json(), skill.json(), rule.json(), memory.json()

    async def test_cantrip_export(self, admin_client):
        client, _, _ = admin_client
        cantrip, _, _, _ = await self._setup(client)
        resp = await client.get(f"/api/cantrips/{cantrip['id']}/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Dice Roller"
        assert body["code"] == VALID_CANTRIP
        assert body["llm_instructions"] == "Roll dice."
        assert "attachment" in resp.headers.get("content-disposition", "")

    async def test_skill_export(self, admin_client):
        client, _, _ = admin_client
        _, skill, _, _ = await self._setup(client)
        resp = await client.get(f"/api/skills/{skill['id']}/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Fencing"
        assert body["content"] == "Parry."
        assert body["type"] == "skill"

    async def test_verification_rule_export(self, admin_client):
        client, _, _ = admin_client
        _, _, rule, _ = await self._setup(client)
        resp = await client.get(f"/api/verification/rules/{rule['id']}/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Stay in character"
        assert body["prompt"] == "Never speak as the user."
        assert body["max_retries"] == 1

    async def test_memory_rule_export(self, admin_client):
        client, _, _ = admin_client
        _, _, _, memory = await self._setup(client)
        resp = await client.get(f"/api/memory-rules/{memory['id']}/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Battle chat"
        assert body["prompt"] == "Track the battle."
        assert body["token_threshold"] == 4000

    async def test_export_is_not_a_cross_user_read(self, admin_client):
        client, _, _ = admin_client
        cantrip, _, _, _ = await self._setup(client)

        created = await client.post(
            "/api/users",
            json={"username": "other", "password": "otherpass123", "is_admin": False},
        )
        assert created.status_code in (200, 201), created.text
        login = await client.post(
            "/api/auth/login", json={"username": "other", "password": "otherpass123"}
        )
        assert login.status_code == 200

        resp = await client.get(
            f"/api/cantrips/{cantrip['id']}/export",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert resp.status_code == 404

    async def test_missing_object_is_404(self, admin_client):
        client, _, _ = admin_client
        assert (
            await client.get("/api/cantrips/no-such-id/export")
        ).status_code == 404
