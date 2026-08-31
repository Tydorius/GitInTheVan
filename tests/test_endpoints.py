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

    await client.post("/api/users", json={"username": "user1", "password": "password1"})

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
    await client.post("/api/users", json={"username": "regular", "password": "pass1234"})
    regular_login = await client.post(
        "/api/auth/login", json={"username": "regular", "password": "pass1234"}
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


# ---------------------------------------------------------------------------
# Phase 23: endpoint parameters and the curated model list
# ---------------------------------------------------------------------------


async def _make_endpoint(client, **overrides):
    payload = {"name": "EP", "base_url": "https://example.com", "api_key": "k"}
    payload.update(overrides)
    resp = await client.post("/api/endpoints", json=payload)
    return resp


@pytest.mark.asyncio
async def test_endpoint_defaults_to_no_parameters_and_no_models(admin_client):
    client, _, _ = admin_client
    resp = await _make_endpoint(client)
    assert resp.status_code == 201
    assert resp.json()["parameters"] == []
    assert resp.json()["models"] == []


@pytest.mark.asyncio
async def test_endpoint_parameters_round_trip(admin_client):
    client, _, _ = admin_client
    resp = await _make_endpoint(
        client,
        parameters=[
            {
                "name": "reasoning_effort",
                "type": "string",
                "value": "max",
                "description": "Thinking budget",
                "required": True,
                "options": ["low", "high", "max"],
            },
            {"name": "max_tokens", "type": "integer", "value": 128000},
        ],
    )
    assert resp.status_code == 201

    listed = (await client.get("/api/endpoints")).json()["endpoints"][0]
    by_name = {p["name"]: p for p in listed["parameters"]}
    assert by_name["reasoning_effort"]["value"] == "max"
    assert by_name["reasoning_effort"]["required"] is True
    assert by_name["reasoning_effort"]["options"] == ["low", "high", "max"]
    assert by_name["reasoning_effort"]["description"] == "Thinking budget"
    assert by_name["max_tokens"]["type"] == "integer"


@pytest.mark.asyncio
async def test_model_list_with_per_model_parameters_round_trips(admin_client):
    client, _, _ = admin_client
    resp = await _make_endpoint(
        client,
        models=[
            {
                "name": "gpt-5",
                "description": "Flagship",
                "parameters": [{"name": "reasoning_effort", "type": "string", "value": "max"}],
            },
            {"name": "gpt-5-mini", "description": "", "parameters": []},
        ],
    )
    assert resp.status_code == 201

    models = (await client.get("/api/endpoints")).json()["endpoints"][0]["models"]
    assert [m["name"] for m in models] == ["gpt-5", "gpt-5-mini"]
    assert models[0]["description"] == "Flagship"
    assert models[0]["parameters"][0]["value"] == "max"
    assert models[1]["parameters"] == []


@pytest.mark.asyncio
async def test_updating_models_replaces_the_whole_list(admin_client):
    client, _, _ = admin_client
    ep = (await _make_endpoint(client, models=[{"name": "old-a"}, {"name": "old-b"}])).json()

    resp = await client.put(
        f"/api/endpoints/{ep['id']}",
        json={"models": [{"name": "new-only"}]},
    )
    assert resp.status_code == 200
    assert [m["name"] for m in resp.json()["models"]] == ["new-only"]

    listed = (await client.get("/api/endpoints")).json()["endpoints"][0]
    assert [m["name"] for m in listed["models"]] == ["new-only"]


@pytest.mark.asyncio
async def test_omitting_models_on_update_leaves_them_alone(admin_client):
    """A PUT that only changes the name must not silently wipe the model list."""
    client, _, _ = admin_client
    ep = (await _make_endpoint(client, models=[{"name": "keep-me"}])).json()

    resp = await client.put(f"/api/endpoints/{ep['id']}", json={"name": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed"
    assert [m["name"] for m in resp.json()["models"]] == ["keep-me"]


@pytest.mark.asyncio
async def test_updating_parameters_replaces_them(admin_client):
    client, _, _ = admin_client
    ep = (
        await _make_endpoint(
            client, parameters=[{"name": "temperature", "type": "float", "value": 0.7}]
        )
    ).json()

    resp = await client.put(
        f"/api/endpoints/{ep['id']}",
        json={"parameters": [{"name": "top_p", "type": "float", "value": 0.9}]},
    )
    assert resp.status_code == 200
    assert [p["name"] for p in resp.json()["parameters"]] == ["top_p"]


@pytest.mark.asyncio
async def test_deleting_an_endpoint_removes_its_models(admin_client):
    client, _, _ = admin_client
    ep = (await _make_endpoint(client, models=[{"name": "m1"}])).json()

    assert (await client.delete(f"/api/endpoints/{ep['id']}")).status_code == 204
    assert (await client.get("/api/endpoints")).json()["endpoints"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"name": "stream", "type": "boolean", "value": True},
        {"name": "model", "value": "evil"},
        {"name": "messages", "value": "x"},
        {"name": "_gitv_tags", "value": "x"},
        {"name": "has space", "value": "x"},
        {"name": "temperature", "type": "datetime", "value": "x"},
        {"name": "temperature", "value": "", "required": True},
        {"name": "reasoning_effort", "value": "ultra", "options": ["low", "max"]},
    ],
)
async def test_invalid_parameters_are_rejected(admin_client, bad):
    client, _, _ = admin_client
    resp = await _make_endpoint(client, parameters=[bad])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_parameter_names_are_rejected(admin_client):
    client, _, _ = admin_client
    resp = await _make_endpoint(
        client,
        parameters=[{"name": "temperature", "value": "1"}, {"name": "temperature", "value": "2"}],
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicate_model_names_are_rejected(admin_client):
    client, _, _ = admin_client
    resp = await _make_endpoint(client, models=[{"name": "gpt-5"}, {"name": "gpt-5"}])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_model_probe_falls_back_to_the_curated_list(admin_client):
    """The live probe cannot reach example.com in tests, so the curated names
    are what comes back -- an endpoint behind a firewall still lists models."""
    client, _, _ = admin_client
    ep = (
        await _make_endpoint(client, models=[{"name": "zeta"}, {"name": "alpha"}])
    ).json()

    resp = await client.get(f"/api/endpoints/{ep['id']}/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_the_model_probe_respects_a_chat_completions_api_base_path(
    admin_client, httpx_mock
):
    """The probe used to append `/models` to whatever `api_base_path` held.

    For an OpenWebUI-style endpoint that is `/api/chat/completions`, so it asked
    for `/api/chat/completions/models`, got the SPA's HTML back with a 200, and
    failed to parse it -- "Fetch from provider" returned an empty list and gave
    no indication anything had gone wrong. It must hit `/api/models`.
    """
    client, _, _ = admin_client
    ep = (
        await client.post(
            "/api/endpoints",
            json={
                "name": "OpenWebUI",
                "base_url": "https://owui.test",
                "api_key": "sk-owui",
                "api_base_path": "/api/chat/completions",
            },
        )
    ).json()

    httpx_mock.add_response(
        url="https://owui.test/api/models",
        json={"data": [{"id": "llama-3.3-70b"}, {"id": "qwen-2.5-coder"}]},
    )

    resp = await client.get(f"/api/endpoints/{ep['id']}/models")
    assert resp.status_code == 200
    assert resp.json()["models"] == ["llama-3.3-70b", "qwen-2.5-coder"]


@pytest.mark.asyncio
async def test_the_model_probe_leaves_a_plain_api_base_path_alone(
    admin_client, httpx_mock
):
    """A path that is a prefix rather than a full chat route is unchanged."""
    client, _, _ = admin_client
    ep = (
        await client.post(
            "/api/endpoints",
            json={
                "name": "z.ai",
                "base_url": "https://api.z.test",
                "api_key": "sk-z",
                "api_base_path": "/api/coding/paas/v4",
            },
        )
    ).json()

    httpx_mock.add_response(
        url="https://api.z.test/api/coding/paas/v4/models",
        json={"data": [{"id": "glm-4.6"}]},
    )

    resp = await client.get(f"/api/endpoints/{ep['id']}/models")
    assert resp.json()["models"] == ["glm-4.6"]
