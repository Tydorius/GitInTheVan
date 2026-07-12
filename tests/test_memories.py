import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.memory import Memory
from app.services.admin import update_admin_settings
from tests.conftest import TestSessionLocal


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


async def _current_user_id(client) -> str:
    resp = await client.get("/api/auth/me")
    return resp.json()["id"]


async def _seed_memory(user_id: str, key: str = "test_key", value: str = "initial value") -> str:
    async with TestSessionLocal() as db:
        memory = Memory(user_id=user_id, conversation_id="conv1", key=key, value=value)
        db.add(memory)
        await db.commit()
        await db.refresh(memory)
        return memory.id


@pytest.mark.asyncio
class TestMemoryList:
    async def test_list_memories(self, auth_client):
        user_id = await _current_user_id(auth_client)
        await _seed_memory(user_id)
        resp = await auth_client.get("/api/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1


@pytest.mark.asyncio
class TestMemoryUpdate:
    async def test_update_memory_value(self, auth_client):
        user_id = await _current_user_id(auth_client)
        memory_id = await _seed_memory(user_id)
        resp = await auth_client.put(f"/api/memories/{memory_id}", json={"value": "updated value"})
        assert resp.status_code == 200
        assert resp.json()["value"] == "updated value"

    async def test_update_nonexistent_memory(self, auth_client):
        resp = await auth_client.put("/api/memories/does-not-exist", json={"value": "x"})
        assert resp.status_code == 404

    async def test_update_strips_control_chars(self, auth_client):
        user_id = await _current_user_id(auth_client)
        memory_id = await _seed_memory(user_id)
        resp = await auth_client.put(f"/api/memories/{memory_id}", json={"value": "hello\x00world"})
        assert resp.status_code == 200
        assert resp.json()["value"] == "helloworld"

    async def test_update_exceeds_total_size_limit(self, auth_client):
        user_id = await _current_user_id(auth_client)
        memory_id = await _seed_memory(user_id)
        await update_admin_settings({"max_memory_size_mb": 1})
        oversized = "x" * (2 * 1024 * 1024)
        resp = await auth_client.put(f"/api/memories/{memory_id}", json={"value": oversized})
        assert resp.status_code == 413
        await update_admin_settings({"max_memory_size_mb": 50})

    async def test_delete_memory(self, auth_client):
        user_id = await _current_user_id(auth_client)
        memory_id = await _seed_memory(user_id)
        resp = await auth_client.delete(f"/api/memories/{memory_id}")
        assert resp.status_code == 204
