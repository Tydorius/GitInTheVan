import pytest

from app.config import Settings

MOCK_ENDPOINT_URL = "http://mock-upstream.test"


async def _create_user_with_endpoint(client, httpx_mock):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert setup_resp.status_code == 201
    admin_token = setup_resp.json()["access_token"]

    user_resp = await client.post(
        "/api/users",
        json={"username": "testuser", "password": "userpass123"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert user_resp.status_code == 201
    user_api_key = user_resp.json()["api_key"]

    login_resp = await client.post(
        "/api/auth/login",
        json={"username": "testuser", "password": "userpass123"},
    )
    user_token = login_resp.json()["access_token"]

    ep_resp = await client.post(
        "/api/endpoints",
        json={
            "name": "MockEndpoint",
            "base_url": MOCK_ENDPOINT_URL,
            "api_key": "upstream-key",
            "enabled": True,
        },
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert ep_resp.status_code == 201

    await client.put(
        "/api/settings",
        json={"default_endpoint_id": ep_resp.json()["id"]},
        headers={"Authorization": f"Bearer {user_token}"},
    )

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={
            "id": "chatcmpl-routed",
            "object": "chat.completion",
            "choices": [{"message": {"content": "routed!"}, "index": 0}],
        },
        status_code=200,
    )

    return user_api_key


@pytest.mark.asyncio
async def test_api_key_routes_to_user_endpoint(client, httpx_mock):
    user_api_key = await _create_user_with_endpoint(client, httpx_mock)

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
        headers={"Authorization": f"Bearer {user_api_key}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "chatcmpl-routed"


@pytest.mark.asyncio
async def test_invalid_api_key_returns_503(client, httpx_mock):
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
        headers={"Authorization": "Bearer gitv_invalidkey123456789"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_different_users_different_endpoints(client):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    admin_token = setup_resp.json()["access_token"]

    await client.post(
        "/api/users",
        json={"username": "user1", "password": "pass1234"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    await client.post(
        "/api/users",
        json={"username": "user2", "password": "pass4567"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login1 = await client.post("/api/auth/login", json={"username": "user1", "password": "pass1234"})
    token1 = login1.json()["access_token"]

    login2 = await client.post("/api/auth/login", json={"username": "user2", "password": "pass4567"})
    token2 = login2.json()["access_token"]

    await client.post(
        "/api/endpoints",
        json={"name": "EP1", "base_url": "http://ep1.test", "api_key": "key1"},
        headers={"Authorization": f"Bearer {token1}"},
    )

    await client.post(
        "/api/endpoints",
        json={"name": "EP2", "base_url": "http://ep2.test", "api_key": "key2"},
        headers={"Authorization": f"Bearer {token2}"},
    )

    ep_list1 = await client.get(
        "/api/endpoints", headers={"Authorization": f"Bearer {token1}"}
    )
    ep_list2 = await client.get(
        "/api/endpoints", headers={"Authorization": f"Bearer {token2}"}
    )

    assert len(ep_list1.json()["endpoints"]) == 1
    assert len(ep_list2.json()["endpoints"]) == 1
    assert ep_list1.json()["endpoints"][0]["name"] == "EP1"
    assert ep_list2.json()["endpoints"][0]["name"] == "EP2"


@pytest.mark.asyncio
async def test_no_bearer_token_falls_back_to_default(client, httpx_mock):
    await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )

    httpx_mock.add_response(
        url="http://default.test/v1/chat/completions",
        json={"id": "chatcmpl-default", "object": "chat.completion", "choices": []},
        status_code=200,
    )

    import app.services.proxy as _proxy_module

    default_settings = Settings(
        default_endpoint_url="http://default.test",
        default_endpoint_api_key="default-key",
        default_endpoint_api_base_path="",
    )

    original_settings = _proxy_module.settings
    _proxy_module.settings = default_settings
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == "chatcmpl-default"
    finally:
        _proxy_module.settings = original_settings
