import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def auth_client(client):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert setup_resp.status_code == 201
    token = setup_resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.headers.pop("Authorization", None)


@pytest.fixture
async def anon_client():
    # Separate client instance: site-banner tests assert behavior from an
    # unauthenticated perspective alongside an authenticated one.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
class TestSiteBanner:
    async def test_public_endpoint_no_auth_required(self, anon_client):
        resp = await anon_client.get("/api/site-banner")
        assert resp.status_code == 200
        data = resp.json()
        assert "banner" in data
        assert "level" in data

    async def test_admin_can_set_banner(self, auth_client, anon_client):
        resp = await auth_client.put(
            "/api/admin/settings",
            json={"site_banner": "Scheduled maintenance tonight", "site_banner_level": "warning"},
        )
        assert resp.status_code == 200
        assert resp.json()["site_banner"] == "Scheduled maintenance tonight"
        assert resp.json()["site_banner_level"] == "warning"

        public_resp = await anon_client.get("/api/site-banner")
        assert public_resp.json() == {"banner": "Scheduled maintenance tonight", "level": "warning"}

    async def test_admin_can_clear_banner(self, auth_client, anon_client):
        await auth_client.put("/api/admin/settings", json={"site_banner": "temp notice"})
        await auth_client.put("/api/admin/settings", json={"site_banner": ""})

        public_resp = await anon_client.get("/api/site-banner")
        assert public_resp.json()["banner"] == ""

    async def test_invalid_banner_level_rejected(self, auth_client):
        resp = await auth_client.put("/api/admin/settings", json={"site_banner_level": "not-a-real-level"})
        assert resp.status_code == 400

    async def test_default_banner_level_is_info(self, auth_client):
        resp = await auth_client.get("/api/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["site_banner_level"] == "info"

    async def test_unauthenticated_cannot_update_settings(self, anon_client):
        resp = await anon_client.put("/api/admin/settings", json={"site_banner": "hijacked"})
        assert resp.status_code in (401, 403)
