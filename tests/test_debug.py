"""Tests for the debug capture system (stage-based pipeline tracking)."""

import json

import pytest

from app.services.debug import (
    _migrate_legacy_pipeline,
    debug_capture,
    debug_capture_response,
    init_debug,
)

MOCK_ENDPOINT_URL = "http://mock-backend:9999"


@pytest.fixture(autouse=True)
def set_endpoint(monkeypatch):
    from app.config import Settings
    test_settings = Settings(
        default_endpoint_url=MOCK_ENDPOINT_URL,
        default_endpoint_api_key="sk-test-key",
        default_endpoint_model="test-model",
        default_endpoint_api_base_path="",
    )
    monkeypatch.setattr("app.services.proxy.settings", test_settings)


def _mock_response(content: str = "Hi there!", model: str = "test-model"):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# ============================================================================
# Unit tests: debug helper functions
# ============================================================================

def test_init_debug_creates_container():
    """init_debug should create the _gitv_debug dict with stages list."""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    tags = [{"raw": "<#test#>"}]

    init_debug(body, tags)

    assert "_gitv_debug" in body
    debug = body["_gitv_debug"]
    assert debug["stages"] == []
    assert json.loads(debug["original_messages"]) == [{"role": "user", "content": "hi"}]
    assert debug["tags"] == ["<#test#>"]


def test_init_debug_empty_tags():
    """init_debug should handle empty/null tags gracefully."""
    body = {"messages": []}

    init_debug(body, None)

    assert body["_gitv_debug"]["tags"] == []


def test_debug_capture_first_stage():
    """First capture should use original_messages as messages_before."""
    body = {"messages": [{"role": "user", "content": "hello"}]}
    init_debug(body, [])

    body["messages"].append({"role": "assistant", "content": "response"})
    debug_capture(body, "test_stage", "Test Stage", detail="Test detail")

    stages = body["_gitv_debug"]["stages"]
    assert len(stages) == 1
    stage = stages[0]
    assert stage["name"] == "test_stage"
    assert stage["label"] == "Test Stage"
    assert stage["detail"] == "Test detail"
    before = json.loads(stage["messages_before"])
    after = json.loads(stage["messages_after"])
    assert len(before) == 1
    assert len(after) == 2


def test_debug_capture_chains_stages():
    """Each capture should use the previous stage's messages_after as before."""
    body = {"messages": [{"role": "user", "content": "hello"}]}
    init_debug(body, [])

    body["messages"][0]["content"] = "modified1"
    debug_capture(body, "stage1", "Stage 1")

    body["messages"][0]["content"] = "modified2"
    debug_capture(body, "stage2", "Stage 2")

    stages = body["_gitv_debug"]["stages"]
    assert len(stages) == 2
    assert json.loads(stages[1]["messages_before"])[0]["content"] == "modified1"
    assert json.loads(stages[1]["messages_after"])[0]["content"] == "modified2"


def test_debug_capture_metadata():
    """Metadata dict should be preserved on the stage entry."""
    body = {"messages": []}
    init_debug(body, [])

    debug_capture(body, "mem", "Memory", metadata={"keys": ["a", "b"], "count": 2})

    stage = body["_gitv_debug"]["stages"][0]
    assert stage["metadata"] == {"keys": ["a", "b"], "count": 2}


def test_debug_capture_setting():
    """Setting and setting_value should be captured."""
    body = {"messages": []}
    init_debug(body, [])

    debug_capture(body, "test", "Test", setting="debug_mode", setting_value=True)

    stage = body["_gitv_debug"]["stages"][0]
    assert stage["setting"] == "debug_mode"
    assert stage["setting_value"] is True


def test_debug_capture_response_stage():
    """debug_capture_response should track content_before/after."""
    body = {"messages": []}
    init_debug(body, [])

    debug_capture_response(body, "verification", "Verification",
                           content_before="bad content",
                           content_after="good content",
                           detail="Approved")

    stage = body["_gitv_debug"]["stages"][0]
    assert stage["content_before"] == "bad content"
    assert stage["content_after"] == "good content"
    assert stage["messages_before"] is None
    assert stage["messages_after"] is None


def test_debug_capture_no_debug_container():
    """debug_capture should be a no-op if _gitv_debug is not set."""
    body = {"messages": []}
    debug_capture(body, "test", "Test")
    assert "_gitv_debug" not in body


def test_debug_capture_response_no_debug_container():
    """debug_capture_response should be a no-op if _gitv_debug is not set."""
    body = {"messages": []}
    debug_capture_response(body, "test", "Test")
    assert "_gitv_debug" not in body


def test_migrate_legacy_pipeline():
    """Legacy pipeline_data without stages should be migrated."""
    legacy = {
        "original_messages": '[{"role":"user","content":"hi"}]',
        "modified_messages": '[{"role":"user","content":"hi"},{"role":"system","content":"lore"}]',
        "tags": ["<#tag#>"],
        "budget": {"percent": 10},
    }

    migrated = _migrate_legacy_pipeline(legacy)

    assert "stages" in migrated
    assert len(migrated["stages"]) == 2
    assert migrated["stages"][0]["name"] == "original"
    assert migrated["stages"][1]["name"] == "modified"
    assert migrated["stages"][1]["metadata"]["budget"] == {"percent": 10}
    assert migrated["tags"] == ["<#tag#>"]


def test_migrate_legacy_pipeline_empty():
    """Empty legacy pipeline_data should produce empty stages list."""
    migrated = _migrate_legacy_pipeline({})
    assert migrated["stages"] == []
    assert migrated["tags"] == []


def test_migrate_legacy_pipeline_only_original():
    """Legacy data with only original_messages (no modified) should work."""
    legacy = {
        "original_messages": '[{"role":"user","content":"hi"}]',
        "tags": [],
    }

    migrated = _migrate_legacy_pipeline(legacy)
    assert len(migrated["stages"]) == 1
    assert migrated["stages"][0]["name"] == "original"


# ============================================================================
# API tests: debug endpoints
# ============================================================================

@pytest.mark.asyncio
async def test_debug_api_list_empty(admin_client):
    """Debug API should return empty list initially."""
    client, token, _ = admin_client

    resp = await client.get("/api/debug")
    assert resp.status_code == 200
    data = resp.json()
    assert data["exchanges"] == []


@pytest.mark.asyncio
async def test_debug_api_capture_and_list(admin_client, httpx_mock):
    """Full flow: enable debug, make proxy request, verify debug exchange appears."""
    client, token, gitv_key = admin_client

    ep_resp = await client.post("/api/endpoints", json={
        "name": "Test EP",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "sk-test-key",
    })
    assert ep_resp.status_code == 201

    await client.put("/api/settings", json={"debug_mode": True})

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("Hello!"),
        status_code=200,
    )

    proxy_resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": False,
        },
        headers={"Authorization": f"Bearer {gitv_key}"},
    )
    assert proxy_resp.status_code == 200

    list_resp = await client.get("/api/debug")
    assert list_resp.status_code == 200
    exchanges = list_resp.json()["exchanges"]
    assert len(exchanges) >= 1
    assert exchanges[0]["stage_count"] > 0
    assert exchanges[0]["has_response"] is True


@pytest.mark.asyncio
async def test_debug_api_get_exchange(admin_client, httpx_mock):
    """Get a specific exchange and verify it has stage data."""
    client, token, gitv_key = admin_client

    await client.post("/api/endpoints", json={
        "name": "Test EP",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "sk-test-key",
    })
    await client.put("/api/settings", json={"debug_mode": True})

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("World!"),
        status_code=200,
    )

    await client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": False,
        },
        headers={"Authorization": f"Bearer {gitv_key}"},
    )

    list_resp = await client.get("/api/debug")
    exchange_id = list_resp.json()["exchanges"][0]["id"]

    get_resp = await client.get(f"/api/debug/{exchange_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert "stages" in data["pipeline_data"]
    assert len(data["pipeline_data"]["stages"]) > 0
    assert data["response_content"] == "World!"

    stage_names = [s["name"] for s in data["pipeline_data"]["stages"]]
    assert "final_messages" in stage_names
    assert "llm_response" in stage_names


@pytest.mark.asyncio
async def test_debug_api_clear(admin_client, httpx_mock):
    """Clear should remove all debug exchanges."""
    client, token, gitv_key = admin_client

    await client.post("/api/endpoints", json={
        "name": "Test EP",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "sk-test-key",
    })
    await client.put("/api/settings", json={"debug_mode": True})

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("x"),
        status_code=200,
    )

    await client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        headers={"Authorization": f"Bearer {gitv_key}"},
    )

    assert len((await client.get("/api/debug")).json()["exchanges"]) >= 1

    clear_resp = await client.delete("/api/debug")
    assert clear_resp.status_code == 204

    assert len((await client.get("/api/debug")).json()["exchanges"]) == 0


@pytest.mark.asyncio
async def test_debug_disabled_no_capture(admin_client, httpx_mock):
    """With debug_mode off, no exchanges should be captured."""
    client, token, gitv_key = admin_client

    await client.post("/api/endpoints", json={
        "name": "Test EP",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "sk-test-key",
    })

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("x"),
        status_code=200,
        is_reusable=True,
    )

    await client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        headers={"Authorization": f"Bearer {gitv_key}"},
    )

    exchanges = (await client.get("/api/debug")).json()["exchanges"]
    assert len(exchanges) == 0


@pytest.mark.asyncio
async def test_debug_stage_content_verification(admin_client, httpx_mock):
    """Stages should contain expected fields: name, label, detail, messages."""
    client, token, gitv_key = admin_client

    await client.post("/api/endpoints", json={
        "name": "Test EP",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "sk-test-key",
    })
    await client.put("/api/settings", json={"debug_mode": True})

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("resp"),
        status_code=200,
    )

    await client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
        headers={"Authorization": f"Bearer {gitv_key}"},
    )

    list_resp = await client.get("/api/debug")
    exchange_id = list_resp.json()["exchanges"][0]["id"]
    detail = (await client.get(f"/api/debug/{exchange_id}")).json()

    for stage in detail["pipeline_data"]["stages"]:
        assert "name" in stage
        assert "label" in stage
        assert "detail" in stage
        assert "metadata" in stage
        assert "messages_before" in stage or "content_before" in stage
        assert "messages_after" in stage or "content_after" in stage
