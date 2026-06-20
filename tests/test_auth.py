import pytest

from app.services.auth import decode_access_token


@pytest.mark.asyncio
async def test_setup_creates_admin(client):
    resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "access_token" in data
    assert "api_key" in data
    assert data["api_key"].startswith("gitv_")

    payload = decode_access_token(data["access_token"])
    assert payload is not None
    assert payload["username"] == "admin"
    assert payload["is_admin"] is True


@pytest.mark.asyncio
async def test_setup_fails_if_admin_exists(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/auth/setup",
        json={"username": "another_admin", "password": "pass123"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login_success(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client, admin_client):
    _, _, _ = admin_client
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrongpassword"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client):
    resp = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "pass"},
    )
    assert resp.status_code == 401
