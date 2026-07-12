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
class TestSkillCRUD:
    async def test_create_skill(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Combat Expert",
            "description": "Combat writing skill",
            "content": "You are an expert at writing combat scenes.",
            "type": "skill",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Combat Expert"
        assert data["type"] == "skill"
        assert data["content"] == "You are an expert at writing combat scenes."
        assert data["endpoints"] == []

    async def test_create_sample(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Prose Style",
            "content": "Match this writing style...",
            "type": "sample",
        })
        assert resp.status_code == 201
        assert resp.json()["type"] == "sample"

    async def test_invalid_type_rejected(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Bad",
            "type": "invalid",
        })
        assert resp.status_code == 400

    async def test_list_skills(self, auth_client):
        await auth_client.post("/api/skills", json={"name": "Skill 1", "type": "skill"})
        await auth_client.post("/api/skills", json={"name": "Sample 1", "type": "sample"})
        resp = await auth_client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) >= 2

    async def test_get_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "Test Skill", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Skill"

    async def test_update_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "Original", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.put(f"/api/skills/{skill_id}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    async def test_delete_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "To Delete", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.delete(f"/api/skills/{skill_id}")
        assert resp.status_code == 204
        resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert resp.status_code == 404

    async def test_create_exceeds_content_size_limit(self, auth_client):
        from app.services.admin import update_admin_settings
        await update_admin_settings({"max_rule_size_kb": 1})
        resp = await auth_client.post("/api/skills", json={
            "name": "TooBig", "content": "x" * 2000, "type": "skill",
        })
        assert resp.status_code == 413
        await update_admin_settings({"max_rule_size_kb": 25})

    async def test_create_strips_control_chars_in_content(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Clean", "content": "hello\x00world", "type": "skill",
        })
        assert resp.status_code == 201
        assert resp.json()["content"] == "helloworld"


@pytest.mark.asyncio
class TestSkillAttachment:
    async def test_attach_and_detach(self, auth_client):
        skill_resp = await auth_client.post("/api/skills", json={"name": "Attach Test", "type": "skill"})
        skill_id = skill_resp.json()["id"]

        ep_resp = await auth_client.post("/api/endpoints", json={
            "name": "Test EP",
            "base_url": "https://example.com",
            "api_key": "test-key",
        })
        ep_id = ep_resp.json()["id"]

        attach_resp = await auth_client.post(f"/api/skills/{skill_id}/attach", json={"endpoint_id": ep_id})
        assert attach_resp.status_code == 201

        skill_resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert ep_id in skill_resp.json()["endpoints"]

        for_endpoint = await auth_client.get(f"/api/skills/for-endpoint/{ep_id}")
        assert for_endpoint.status_code == 200
        assert len(for_endpoint.json()["skills"]) == 1

        detach_resp = await auth_client.delete(f"/api/skills/{skill_id}/attach/{ep_id}")
        assert detach_resp.status_code == 204

        skill_resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert ep_id not in skill_resp.json()["endpoints"]


class TestSkillInjection:
    def test_inject_skills_into_system_message(self):
        from app.services.skills import inject_skills
        messages = [{"role": "system", "content": "You are a character."}]
        result = inject_skills(messages, ["Always write in third person."])
        assert "<skills>" in result[0]["content"]
        assert "Always write in third person." in result[0]["content"]

    def test_inject_skills_creates_system_if_none(self):
        from app.services.skills import inject_skills
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_skills(messages, ["Be descriptive."])
        assert result[0]["role"] == "system"
        assert "<skills>" in result[0]["content"]

    def test_inject_skills_empty_noop(self):
        from app.services.skills import inject_skills
        messages = [{"role": "system", "content": "Original"}]
        result = inject_skills(messages, [])
        assert result[0]["content"] == "Original"

    def test_inject_samples_before_last_message(self):
        from app.services.skills import inject_samples
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Write something"},
        ]
        result = inject_samples(messages, ["Style reference here"])
        sample_idx = next(i for i, m in enumerate(result) if m.get("content", "").startswith("<writing_sample>"))
        assert result[sample_idx]["role"] == "system"
        assert result[sample_idx + 1]["content"] == "Write something"

    def test_inject_samples_empty_noop(self):
        from app.services.skills import inject_samples
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_samples(messages, [])
        assert len(result) == 1
