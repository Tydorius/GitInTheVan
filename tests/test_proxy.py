import json

import httpx
import pytest

from app.config import Settings

MOCK_ENDPOINT_URL = "http://mock-backend:9999"


@pytest.fixture(autouse=True)
def set_endpoint(monkeypatch):
    test_settings = Settings(
        default_endpoint_url=MOCK_ENDPOINT_URL,
        default_endpoint_api_key="sk-test-key",
        default_endpoint_model="test-model",
        default_endpoint_api_base_path="",
    )
    monkeypatch.setattr("app.services.proxy.settings", test_settings)


def _make_request(**overrides):
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "temperature": 0.7,
        "max_tokens": 100,
        "stream": False,
    }
    body.update(overrides)
    return body


def _mock_response(content: str = "Hi there!", model: str = "test-model", **overrides):
    resp = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp.update(overrides)
    return resp


@pytest.mark.asyncio
async def test_forward_non_streaming(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response("Hello back!"),
        status_code=200,
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 200
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "Hello back!"
    assert data["model"] == "test-model"

    forwarded = httpx_mock.get_request()
    assert forwarded is not None
    assert forwarded.headers["authorization"] == "Bearer sk-test-key"
    forwarded_body = json.loads(forwarded.content)
    assert forwarded_body["model"] == "test-model"
    assert forwarded_body["stream"] is False


@pytest.mark.asyncio
async def test_forward_streaming(client, httpx_mock):
    sse_chunks = [
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk"'
        b',{"choices":[{"delta":{"content":"Hel"},"index":0}]}\n\n',
        b'data: {"id":"chatcmpl-1","object":"chat.completion.chunk"'
        b',{"choices":[{"delta":{"content":"lo!"},"index":0}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        content=b"".join(sse_chunks),
        status_code=200,
        headers={"content-type": "text/event-stream"},
    )

    response = await client.post(
        "/v1/chat/completions",
        json=_make_request(stream=True),
    )
    assert response.status_code == 200
    body = response.text
    assert "Hel" in body
    assert "lo!" in body
    assert "[DONE]" in body


@pytest.mark.asyncio
async def test_destination_timeout(client, httpx_mock):
    import httpx as _httpx

    httpx_mock.add_exception(
        _httpx.TimeoutException("timed out"),
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 504
    assert "timed out" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_destination_connection_refused(client, httpx_mock):
    import httpx as _httpx

    httpx_mock.add_exception(
        _httpx.ConnectError("connection refused"),
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 502
    assert "connect" in response.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_destination_error_401(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
        status_code=401,
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 401
    assert "Invalid API key" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_destination_error_429(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
        status_code=429,
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_destination_error_500(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={"error": {"message": "Internal server error", "type": "server_error"}},
        status_code=500,
    )

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 500


@pytest.mark.asyncio
async def test_no_endpoint_configured(client, monkeypatch):
    empty_settings = Settings(default_endpoint_url="", default_endpoint_api_key="")
    monkeypatch.setattr("app.services.proxy.settings", empty_settings)

    response = await client.post("/v1/chat/completions", json=_make_request())
    assert response.status_code == 503
    assert "No endpoint configured" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_request_headers_forwarded(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json=_mock_response(),
        status_code=200,
    )

    response = await client.post(
        "/v1/chat/completions",
        json=_make_request(),
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200

    forwarded = httpx_mock.get_request()
    assert forwarded.headers["accept"] == "application/json"
    assert forwarded.headers["authorization"] == "Bearer sk-test-key"


@pytest.mark.asyncio
async def test_generic_route_forwarding(client, httpx_mock):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/models",
        json={"object": "list", "data": [{"id": "test-model", "object": "model"}]},
        status_code=200,
    )

    response = await client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["data"][0]["id"] == "test-model"


@pytest.mark.asyncio
async def test_v1beta_chat_completions_forwarded(client, httpx_mock):
    """Requests to /v1beta/chat/completions should be forwarded through the proxy."""
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1beta/chat/completions",
        json={"choices": [{"message": {"content": "v1beta response"}}]},
        status_code=200,
    )

    response = await client.post(
        "/v1beta/chat/completions",
        json=_make_request(),
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "v1beta response"


@pytest.mark.asyncio
async def test_v1beta_generic_route_forwarding(client, httpx_mock):
    """Generic routes under /v1beta/ should be forwarded (e.g. models endpoint)."""
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1beta/models",
        json={"object": "list", "data": [{"id": "gemini-model", "object": "model"}]},
        status_code=200,
    )

    response = await client.get("/v1beta/models")
    assert response.status_code == 200
    data = response.json()
    assert data["data"][0]["id"] == "gemini-model"


class TestLiteLLMToolsPassthrough:
    """`tools`/`tool_choice`/`parallel_tool_calls` were absent from
    `LITELLM_NATIVE_PARAMS`, so a client that sent them to a provider endpoint
    lost them silently -- the same class of bug as the pre-0.24
    `reasoning_effort` loss (see tests/test_llm_params_pipeline.py). Mocks only
    `litellm.acompletion`, the outer boundary of the provider path."""

    @pytest.mark.asyncio
    async def test_client_tools_reach_litellm(self, monkeypatch):
        from app.services.proxy import _do_forward_litellm

        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)

            class _Resp:
                def model_dump(self):
                    return _mock_response()

            return _Resp()

        import litellm

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        tools = [{
            "type": "function",
            "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
        }]
        body = json.dumps({
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }).encode()

        _, status = await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0)
        )
        assert status == 200
        assert captured["tools"] == tools
        assert captured["tool_choice"] == "auto"
        assert captured["parallel_tool_calls"] is False


# ============================================================================
# Authenticated streaming (0.29.0)
#
# Every authenticated streaming request buffers the response (memstore
# extraction) and re-emits it as SSE. Until 0.29.0 the buffered forward kept
# `stream: true` in the upstream body, so a raw (non-LiteLLM) endpoint
# answered with SSE that _do_forward could not JSON-parse -- and the client
# received an empty stream. The one pre-existing streaming test uses the
# unauthenticated fixture (true passthrough), which is why this survived.
# ============================================================================


class TestAuthenticatedStreaming:
    @pytest.mark.asyncio
    async def test_streaming_sends_non_streaming_upstream_and_returns_content(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Raw", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        assert ep_resp.status_code in (200, 201), ep_resp.text
        await client.put("/api/settings", json={"default_endpoint_id": ep_resp.json()["id"]})

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_response("Hello back!"),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        # The client still receives SSE...
        assert "Hello back!" in resp.text
        assert "[DONE]" in resp.text
        # ...but the upstream call was made non-streaming, or an SSE answer
        # would fail _do_forward's JSON parse and the stream would come back
        # empty (the 0.9.x-0.28.x behavior on raw endpoints).
        forwarded = httpx_mock.get_request(url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions")
        assert json.loads(forwarded.content)["stream"] is False

    @pytest.mark.asyncio
    async def test_streaming_runs_post_response_cantrips(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Raw", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        assert ep_resp.status_code in (200, 201), ep_resp.text
        await client.put("/api/settings", json={"default_endpoint_id": ep_resp.json()["id"]})

        cantrip_resp = await client.post("/api/cantrips", json={
            "name": "Stamp",
            "code": 'context.response.content = context.response.content + " [POSTNAV]";',
            "is_active": True,
            "run_post_navigator": True,
        })
        assert cantrip_resp.status_code in (200, 201), cantrip_resp.text

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_response("Base reply"),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        # Response-side stages run on buffered streams by default (0.29.0);
        # before that, a post-navigator cantrip silently never ran on any
        # streaming request without verification.
        assert "Base reply [POSTNAV]" in resp.text

    @pytest.mark.asyncio
    async def test_streaming_post_processing_disabled_by_setting(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Raw", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        assert ep_resp.status_code in (200, 201), ep_resp.text
        await client.put("/api/settings", json={"default_endpoint_id": ep_resp.json()["id"]})

        cantrip_resp = await client.post("/api/cantrips", json={
            "name": "Stamp",
            "code": 'context.response.content = context.response.content + " [POSTNAV]";',
            "is_active": True,
            "run_post_navigator": True,
        })
        assert cantrip_resp.status_code in (200, 201), cantrip_resp.text

        settings_resp = await client.put("/api/settings", json={
            "streaming_post_processing_enabled": False,
        })
        assert settings_resp.status_code == 200
        assert settings_resp.json()["streaming_post_processing_enabled"] is False

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_response("Base reply"),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert "Base reply" in resp.text
        assert "[POSTNAV]" not in resp.text
