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
class TestForbiddenWordCRUD:
    async def test_create_word(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "badword"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["phrase"] == "badword"
        assert data["is_regex"] is False

    async def test_create_regex_word(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": r"\bbad\w*\b", "is_regex": True})
        assert resp.status_code == 201
        assert resp.json()["is_regex"] is True

    async def test_create_empty_phrase_rejected(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "   "})
        assert resp.status_code == 400

    async def test_list_words(self, auth_client):
        await auth_client.post("/api/forbidden-words", json={"phrase": "one"})
        await auth_client.post("/api/forbidden-words", json={"phrase": "two"})
        resp = await auth_client.get("/api/forbidden-words")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 2

    async def test_delete_word(self, auth_client):
        create_resp = await auth_client.post("/api/forbidden-words", json={"phrase": "deleteme"})
        word_id = create_resp.json()["id"]
        resp = await auth_client.delete(f"/api/forbidden-words/{word_id}")
        assert resp.status_code == 204

    async def test_create_strips_control_chars(self, auth_client):
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": "bad\x00word"})
        assert resp.status_code == 201
        assert resp.json()["phrase"] == "badword"

    async def test_create_exceeds_size_limit(self, auth_client):
        await update_admin_settings({"max_rule_size_kb": 1})
        oversized = "x" * 2000
        resp = await auth_client.post("/api/forbidden-words", json={"phrase": oversized})
        assert resp.status_code == 413
        await update_admin_settings({"max_rule_size_kb": 25})
