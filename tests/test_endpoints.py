import pytest


@pytest.mark.asyncio
async def test_list_endpoints_empty(admin_client):
    client, _, _ = admin_client
    resp = await client.get("/api/endpoints")
    assert resp.status_code == 200
    assert resp.json()["endpoints"] == []


@pytest.mark.asyncio
async def test_create_and_list_endpoint(admin_client):
    client, _, _ = admin_client
    create_resp = await client.post(
        "/api/endpoints",
        json={
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api",
            "api_key": "sk-or-test",
            "enabled": True,
        },
    )
    assert create_resp.status_code == 201
    ep = create_resp.json()
    assert ep["name"] == "OpenRouter"
    assert ep["base_url"] == "https://openrouter.ai/api"
    assert ep["enabled"] is True

    list_resp = await client.get("/api/endpoints")
    assert list_resp.status_code == 200
    eps = list_resp.json()["endpoints"]
    assert len(eps) == 1
    assert eps[0]["name"] == "OpenRouter"


@pytest.mark.asyncio
async def test_update_endpoint(admin_client):
    client, _, _ = admin_client
    create_resp = await client.post(
        "/api/endpoints",
        json={
            "name": "Test",
            "base_url": "https://example.com/v1",
            "api_key": "key1",
        },
    )
    ep_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/endpoints/{ep_id}",
        json={"name": "Updated", "base_url": "https://new.example.com/v1"},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Updated"
    assert update_resp.json()["base_url"] == "https://new.example.com/v1"


@pytest.mark.asyncio
async def test_delete_endpoint(admin_client):
    client, _, _ = admin_client
    create_resp = await client.post(
        "/api/endpoints",
        json={
            "name": "ToDelete",
            "base_url": "https://example.com",
            "api_key": "key1",
        },
    )
    ep_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/endpoints/{ep_id}")
    assert delete_resp.status_code == 204

    list_resp = await client.get("/api/endpoints")
    assert list_resp.json()["endpoints"] == []


@pytest.mark.asyncio
async def test_endpoint_not_found(admin_client):
    client, _, _ = admin_client
    resp = await client.put(
        "/api/endpoints/nonexistent",
        json={"name": "X"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_settings_default(admin_client):
    client, _, _ = admin_client
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_endpoint_id"] is None
    assert data["default_model"] == ""


@pytest.mark.asyncio
async def test_update_settings(admin_client):
    client, _, _ = admin_client

    ep_resp = await client.post(
        "/api/endpoints",
        json={"name": "EP", "base_url": "https://test.com", "api_key": "k"},
    )
    ep_id = ep_resp.json()["id"]

    resp = await client.put(
        "/api/settings",
        json={"default_endpoint_id": ep_id, "default_model": "gpt-4"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["default_endpoint_id"] == ep_id
    assert data["default_model"] == "gpt-4"


@pytest.mark.asyncio
async def test_admin_can_create_user(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/users",
        json={"username": "testuser", "password": "userpass123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "testuser"
    assert data["is_admin"] is False
    assert data["api_key"].startswith("gitv_")


@pytest.mark.asyncio
async def test_admin_can_list_users(admin_client):
    client, _, _ = admin_client

    await client.post("/api/users", json={"username": "user1", "password": "pass1"})

    resp = await client.get("/api/users")
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert len(users) == 2
    usernames = [u["username"] for u in users]
    assert "admin" in usernames
    assert "user1" in usernames


@pytest.mark.asyncio
async def test_non_admin_cannot_create_user(client, admin_client):
    _, _, _ = admin_client
    await client.post("/api/users", json={"username": "regular", "password": "pass123"})
    regular_login = await client.post(
        "/api/auth/login", json={"username": "regular", "password": "pass123"}
    )
    token = regular_login.json()["access_token"]

    resp = await client.post(
        "/api/users",
        json={"username": "unauthorized", "password": "pass"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_username_fails(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/users",
        json={"username": "admin", "password": "pass"},
    )
    assert resp.status_code == 409
