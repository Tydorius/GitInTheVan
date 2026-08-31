"""Phase 23: configured parameters reaching the wire.

`tests/test_llm_params.py` pins the merge algebra in isolation. This file pins
the part that is easy to get wrong and impossible to see from a unit test: that
the resolved set actually lands in the outbound request body, on the right call,
with the right layer winning -- and that a failover candidate re-resolves
against its own configuration rather than inheriting the first candidate's.

The regression at the centre of this feature is asserted directly: a client's
`reasoning_effort` reached a raw endpoint intact and was silently dropped the
moment the endpoint had a `provider` set, because `_do_forward_litellm` builds
its call from a fixed list of named kwargs.
"""

from __future__ import annotations

import json

import httpx
import pytest


def _sent_body(httpx_mock, url: str) -> dict:
    for req in httpx_mock.get_requests():
        if str(req.url) == url:
            return json.loads(req.content)
    raise AssertionError(f"No request was sent to {url}")


async def _chat(client, api_key, **body):
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}
    payload.update(body)
    return await client.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )


async def _endpoint(client, **kw):
    payload = {"name": "EP", "base_url": "http://ep.test", "api_key": "sk-1"}
    payload.update(kw)
    resp = await client.post("/api/endpoints", json=payload)
    assert resp.status_code == 201, resp.text
    ep = resp.json()
    await client.put("/api/settings", json={"default_endpoint_id": ep["id"]})
    return ep


OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class TestDriverPath:
    @pytest.mark.asyncio
    async def test_endpoint_parameter_reaches_the_upstream_body(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(
            client,
            parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        assert (await _chat(client, api_key)).status_code == 200
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_endpoint_parameter_overrides_the_client(self, admin_client, httpx_mock):
        """The maintainer's stated rule: configured wins over the sending site."""
        client, _, api_key = admin_client
        await _endpoint(
            client,
            parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key, reasoning_effort="low")
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_unconfigured_client_parameters_survive(self, admin_client, httpx_mock):
        """"Honoured unless overwritten" -- a key nobody configured is untouched."""
        client, _, api_key = admin_client
        await _endpoint(
            client, parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}]
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key, temperature=0.42, top_p=0.9)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["temperature"] == 0.42
        assert body["top_p"] == 0.9
        assert body["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_model_layer_beats_endpoint_layer(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(
            client,
            parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
            models=[
                {
                    "name": "test-model",
                    "parameters": [
                        {"name": "reasoning_effort", "type": "string", "value": "low"}
                    ],
                }
            ],
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_an_unlisted_model_gets_only_the_endpoint_layer(self, admin_client, httpx_mock):
        """Model fields keep a free-text escape hatch, so a model that is not in
        the curated list is normal and simply contributes no layer."""
        client, _, api_key = admin_client
        await _endpoint(
            client,
            parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
            models=[
                {
                    "name": "some-other-model",
                    "parameters": [
                        {"name": "reasoning_effort", "type": "string", "value": "low"}
                    ],
                }
            ],
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_typed_values_are_sent_as_their_type_not_as_strings(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(
            client,
            parameters=[
                {"name": "max_tokens", "type": "integer", "value": "128000"},
                {"name": "temperature", "type": "float", "value": "0.25"},
                {"name": "logprobs", "type": "boolean", "value": "true"},
                {"name": "stop", "type": "string[]", "value": "END, ###"},
            ],
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["max_tokens"] == 128000 and isinstance(body["max_tokens"], int)
        assert body["temperature"] == 0.25
        assert body["logprobs"] is True
        assert body["stop"] == ["END", "###"]

    @pytest.mark.asyncio
    async def test_no_internal_keys_leak_upstream(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(
            client, parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}]
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert not [k for k in body if k.startswith("_gitv")]


class TestUserSettingsLayer:
    @pytest.mark.asyncio
    async def test_user_layer_applies_when_the_endpoint_says_nothing(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(client)
        await client.put(
            "/api/settings",
            json={"parameters": [{"name": "reasoning_effort", "type": "string", "value": "high"}]},
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_endpoint_layer_beats_user_layer(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _endpoint(
            client, parameters=[{"name": "reasoning_effort", "type": "string", "value": "max"}]
        )
        await client.put(
            "/api/settings",
            json={"parameters": [{"name": "reasoning_effort", "type": "string", "value": "high"}]},
        )
        httpx_mock.add_response(url="http://ep.test/v1/chat/completions", json=OK)

        await _chat(client, api_key)
        body = _sent_body(httpx_mock, "http://ep.test/v1/chat/completions")
        assert body["reasoning_effort"] == "max"


class TestFailoverReResolves:
    @pytest.mark.asyncio
    async def test_second_candidate_uses_its_own_parameters(self, admin_client, httpx_mock):
        """A fallback endpoint is a different endpoint. Inheriting the primary's
        parameters would send a value the user configured for a model that is
        not the one now being called."""
        client, _, api_key = admin_client
        ep1 = await client.post(
            "/api/endpoints",
            json={
                "name": "Primary", "base_url": "http://primary.test", "api_key": "sk-1",
                "role_tag": "driver", "priority": 1,
                "parameters": [{"name": "reasoning_effort", "type": "string", "value": "max"}],
            },
        )
        await client.post(
            "/api/endpoints",
            json={
                "name": "Fallback", "base_url": "http://fallback.test", "api_key": "sk-2",
                "role_tag": "driver", "priority": 2,
                "parameters": [{"name": "reasoning_effort", "type": "string", "value": "low"}],
            },
        )
        await client.put("/api/settings", json={"default_endpoint_id": ep1.json()["id"]})

        httpx_mock.add_response(
            url="http://primary.test/v1/chat/completions", json={"error": {}}, status_code=500
        )
        httpx_mock.add_response(url="http://fallback.test/v1/chat/completions", json=OK)

        assert (await _chat(client, api_key)).status_code == 200
        assert _sent_body(httpx_mock, "http://primary.test/v1/chat/completions")[
            "reasoning_effort"
        ] == "max"
        assert _sent_body(httpx_mock, "http://fallback.test/v1/chat/completions")[
            "reasoning_effort"
        ] == "low"


class TestLiteLLMPath:
    """The provider path builds its call from named kwargs, so anything it does
    not name is dropped. These pin what it now forwards."""

    @staticmethod
    def _capture(monkeypatch):
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)

            class _Resp:
                def model_dump(self):
                    return dict(OK)

            return _Resp()

        import litellm

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        return captured

    @pytest.mark.asyncio
    async def test_client_reasoning_effort_is_no_longer_dropped(self, monkeypatch):
        """The bug this phase was built on: `reasoning_effort` was absent from
        the kwarg list, so a provider endpoint silently discarded it."""
        from app.services.proxy import _do_forward_litellm

        captured = self._capture(monkeypatch)
        body = json.dumps(
            {"model": "m", "messages": [], "reasoning_effort": "high", "max_completion_tokens": 42}
        ).encode()

        _, status = await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0)
        )
        assert status == 200
        assert captured["reasoning_effort"] == "high"
        assert captured["max_completion_tokens"] == 42

    @pytest.mark.asyncio
    async def test_configured_parameter_overrides_the_client_value(self, monkeypatch):
        from app.services.proxy import _do_forward_litellm

        captured = self._capture(monkeypatch)
        body = json.dumps({"model": "m", "messages": [], "reasoning_effort": "high"}).encode()

        await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0),
            configured={"reasoning_effort": "max"},
        )
        assert captured["reasoning_effort"] == "max"

    @pytest.mark.asyncio
    async def test_unknown_configured_parameter_rides_in_extra_body(self, monkeypatch):
        """A user-named parameter LiteLLM has no opinion about must still reach
        the provider, or the feature does nothing on provider endpoints."""
        from app.services.proxy import _do_forward_litellm

        captured = self._capture(monkeypatch)
        body = json.dumps({"model": "m", "messages": []}).encode()

        await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0),
            configured={"repetition_penalty": 1.1, "min_p": 0.05},
        )
        assert captured["extra_body"] == {"repetition_penalty": 1.1, "min_p": 0.05}

    @pytest.mark.asyncio
    async def test_native_parameters_do_not_go_to_extra_body(self, monkeypatch):
        from app.services.proxy import _do_forward_litellm

        captured = self._capture(monkeypatch)
        body = json.dumps({"model": "m", "messages": []}).encode()

        await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0),
            configured={"temperature": 0.3},
        )
        assert captured["temperature"] == 0.3
        assert "extra_body" not in captured

    @pytest.mark.asyncio
    async def test_reserved_names_cannot_be_smuggled_through_configured(self, monkeypatch):
        from app.services.proxy import _do_forward_litellm

        captured = self._capture(monkeypatch)
        body = json.dumps({"model": "m", "messages": [], "stream": False}).encode()

        await _do_forward_litellm(
            body, "openai", "http://x.test", "sk", httpx.Timeout(10.0),
            configured={"stream": True, "model": "evil", "_gitv_x": 1},
        )
        assert captured["model"] == "openai/m"
        assert not captured.get("stream")
        assert "extra_body" not in captured
