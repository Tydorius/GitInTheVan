import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


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
class TestScenarioRuleCRUD:
    async def test_create_pre_rule(self, auth_client):
        resp = await auth_client.post("/api/scenario-rules", json={
            "name": "Pre Compressor",
            "token_threshold": 3000,
            "fire_position": "pre",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Pre Compressor"
        assert data["fire_position"] == "pre"
        assert data["token_threshold"] == 3000
        assert data["is_active"] is True

    async def test_create_post_rule(self, auth_client):
        resp = await auth_client.post("/api/scenario-rules", json={
            "name": "Post Compressor",
            "token_threshold": 8000,
            "fire_position": "post",
        })
        assert resp.status_code == 201
        assert resp.json()["fire_position"] == "post"

    async def test_invalid_position_rejected(self, auth_client):
        resp = await auth_client.post("/api/scenario-rules", json={
            "name": "Bad",
            "fire_position": "invalid",
        })
        assert resp.status_code == 400

    async def test_list_rules(self, auth_client):
        await auth_client.post("/api/scenario-rules", json={"name": "R1", "fire_position": "pre"})
        await auth_client.post("/api/scenario-rules", json={"name": "R2", "fire_position": "post"})
        resp = await auth_client.get("/api/scenario-rules")
        assert resp.status_code == 200
        rules = resp.json()["rules"]
        assert len(rules) >= 2

    async def test_update_rule(self, auth_client):
        create = await auth_client.post("/api/scenario-rules", json={"name": "Original", "fire_position": "pre"})
        rule_id = create.json()["id"]
        resp = await auth_client.put(f"/api/scenario-rules/{rule_id}", json={"token_threshold": 5000})
        assert resp.status_code == 200
        assert resp.json()["token_threshold"] == 5000

    async def test_delete_rule(self, auth_client):
        create = await auth_client.post("/api/scenario-rules", json={"name": "ToDelete", "fire_position": "pre"})
        rule_id = create.json()["id"]
        resp = await auth_client.delete(f"/api/scenario-rules/{rule_id}")
        assert resp.status_code == 204
        resp = await auth_client.get(f"/api/scenario-rules/{rule_id}")
        assert resp.status_code == 404

    async def test_get_default_prompt(self, auth_client):
        resp = await auth_client.get("/api/scenario-rules/default-prompt")
        assert resp.status_code == 200
        assert "Summarize" in resp.json()["prompt"]


class TestScenarioSummarizerLogic:
    def test_find_first_system_index(self):
        from app.services.scenario_summarizer import _find_first_system_index
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "sys"},
            {"role": "system", "content": "sys2"},
        ]
        assert _find_first_system_index(messages) == 1

    def test_find_first_system_index_none(self):
        from app.services.scenario_summarizer import _find_first_system_index
        messages = [{"role": "user", "content": "hi"}]
        assert _find_first_system_index(messages) is None

    def test_find_first_system_index_empty(self):
        from app.services.scenario_summarizer import _find_first_system_index
        assert _find_first_system_index([]) is None
