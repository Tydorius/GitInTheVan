import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.admin import update_admin_settings


@pytest.fixture
async def auth_client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            await client.post("/api/auth/setup", json={"username": "testadmin", "password": "TestPass123!"})
        except Exception:
            pass
        resp = await client.post("/api/auth/login", json={"username": "testadmin", "password": "TestPass123!"})
        token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.mark.asyncio
class TestMemoryRuleCRUD:
    async def test_create_rule(self, auth_client):
        resp = await auth_client.post("/api/memory-rules", json={
            "name": "Aggressive Trim",
            "token_threshold": 4000,
            "keep_recent": 10,
            "prompt": "Summarize aggressively.",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Aggressive Trim"
        assert data["prompt"] == "Summarize aggressively."

    async def test_list_rules(self, auth_client):
        await auth_client.post("/api/memory-rules", json={"name": "R1"})
        resp = await auth_client.get("/api/memory-rules")
        assert resp.status_code == 200
        assert len(resp.json()["rules"]) >= 1

    async def test_update_rule_prompt(self, auth_client):
        create_resp = await auth_client.post("/api/memory-rules", json={"name": "R2", "prompt": "old"})
        rule_id = create_resp.json()["id"]
        resp = await auth_client.put(f"/api/memory-rules/{rule_id}", json={"prompt": "new prompt"})
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "new prompt"

    async def test_delete_rule(self, auth_client):
        create_resp = await auth_client.post("/api/memory-rules", json={"name": "R3"})
        rule_id = create_resp.json()["id"]
        resp = await auth_client.delete(f"/api/memory-rules/{rule_id}")
        assert resp.status_code == 204

    async def test_create_strips_control_chars_in_prompt(self, auth_client):
        resp = await auth_client.post("/api/memory-rules", json={"name": "R4", "prompt": "bad\x00prompt"})
        assert resp.status_code == 201
        assert resp.json()["prompt"] == "badprompt"

    async def test_create_exceeds_prompt_size_limit(self, auth_client):
        await update_admin_settings({"max_rule_size_kb": 1})
        resp = await auth_client.post("/api/memory-rules", json={"name": "R5", "prompt": "x" * 2000})
        assert resp.status_code == 413
        await update_admin_settings({"max_rule_size_kb": 25})

    async def test_update_exceeds_prompt_size_limit(self, auth_client):
        create_resp = await auth_client.post("/api/memory-rules", json={"name": "R6", "prompt": "short"})
        rule_id = create_resp.json()["id"]
        await update_admin_settings({"max_rule_size_kb": 1})
        resp = await auth_client.put(f"/api/memory-rules/{rule_id}", json={"prompt": "x" * 2000})
        assert resp.status_code == 413
        await update_admin_settings({"max_rule_size_kb": 25})
