"""Phase 23: the narrow parameter scopes -- rules, map stages, scenario rules.

`test_llm_params_pipeline.py` covers the driver call and the LiteLLM forwarder.
This file covers the scopes closest to the message: a verification rule, a map
stage, a scenario rule, and the three user-settings role layers.

The judge is the interesting one. It is a separate call, on a possibly different
endpoint and model, that used to be built from a five-field literal nobody could
change. It now resolves its own layers, ending with the rule.
"""

from __future__ import annotations

import json

import pytest

MAIN = "http://ep.test/v1/chat/completions"

DRIVER_REPLY = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
JUDGE_PASS = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": '{"violation": false, "reason": "", "severity": "none"}',
            }
        }
    ]
}


async def _chat(client, api_key, **body):
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}
    payload.update(body)
    return await client.post(
        "/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {api_key}"}
    )


def _bodies(httpx_mock, url=MAIN):
    return [json.loads(r.content) for r in httpx_mock.get_requests() if str(r.url) == url]


async def _setup_verification(client, *, endpoint_params, rule_params):
    ep = await client.post(
        "/api/endpoints",
        json={
            "name": "Main",
            "base_url": "http://ep.test",
            "api_key": "sk-1",
            "parameters": endpoint_params,
        },
    )
    assert ep.status_code == 201, ep.text
    endpoint_id = ep.json()["id"]
    await client.put("/api/settings", json={"default_endpoint_id": endpoint_id})
    rule = await client.post(
        "/api/verification/rules",
        json={"name": "Tone", "prompt": "Check tone", "parameters": rule_params},
    )
    assert rule.status_code == 201, rule.text
    await client.put(
        "/api/verification/settings",
        json={
            "verification_enabled": True,
            "verification_endpoint_id": endpoint_id,
            "verification_model": "judge-model",
        },
    )


class TestVerificationRuleLayer:
    @pytest.mark.asyncio
    async def test_rule_low_beats_endpoint_max_on_the_judge_call(self, admin_client, httpx_mock):
        """The maintainer's stated example, end to end.

        Endpoint says max, the verification rule says low, the client asked for
        high. The driver call sends max; the judge call sends low.
        """
        client, _, api_key = admin_client
        await _setup_verification(
            client,
            endpoint_params=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
            rule_params=[{"name": "reasoning_effort", "type": "string", "value": "low"}],
        )

        httpx_mock.add_response(url=MAIN, json=DRIVER_REPLY)
        httpx_mock.add_response(url=MAIN, json=JUDGE_PASS)

        resp = await _chat(client, api_key, reasoning_effort="high")
        assert resp.status_code == 200

        bodies = _bodies(httpx_mock)
        assert len(bodies) == 2, f"expected a driver call and a judge call, got {len(bodies)}"
        driver, judge = bodies
        assert driver["reasoning_effort"] == "max"
        assert judge["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_judge_defaults_are_overridable(self, admin_client, httpx_mock):
        """max_tokens 200 / temperature 0.1 were hard-coded literals. They are
        now defaults that a configured parameter replaces."""
        client, _, api_key = admin_client
        await _setup_verification(
            client,
            endpoint_params=[],
            rule_params=[
                {"name": "max_tokens", "type": "integer", "value": 900},
                {"name": "temperature", "type": "float", "value": 0.8},
            ],
        )

        httpx_mock.add_response(url=MAIN, json=DRIVER_REPLY)
        httpx_mock.add_response(url=MAIN, json=JUDGE_PASS)

        await _chat(client, api_key)
        judge = _bodies(httpx_mock)[1]
        assert judge["max_tokens"] == 900
        assert judge["temperature"] == 0.8

    @pytest.mark.asyncio
    async def test_judge_keeps_its_defaults_when_nothing_is_configured(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _setup_verification(client, endpoint_params=[], rule_params=[])

        httpx_mock.add_response(url=MAIN, json=DRIVER_REPLY)
        httpx_mock.add_response(url=MAIN, json=JUDGE_PASS)

        await _chat(client, api_key)
        judge = _bodies(httpx_mock)[1]
        assert judge["max_tokens"] == 200
        assert judge["temperature"] == 0.1

    @pytest.mark.asyncio
    async def test_endpoint_layer_reaches_the_judge_when_the_rule_is_silent(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _setup_verification(
            client,
            endpoint_params=[{"name": "reasoning_effort", "type": "string", "value": "max"}],
            rule_params=[],
        )

        httpx_mock.add_response(url=MAIN, json=DRIVER_REPLY)
        httpx_mock.add_response(url=MAIN, json=JUDGE_PASS)

        await _chat(client, api_key)
        assert _bodies(httpx_mock)[1]["reasoning_effort"] == "max"


class TestVerificationRuleApi:
    @pytest.mark.asyncio
    async def test_rule_parameters_round_trip(self, admin_client):
        client, _, _ = admin_client
        created = await client.post(
            "/api/verification/rules",
            json={
                "name": "Tone",
                "prompt": "p",
                "parameters": [{"name": "reasoning_effort", "type": "string", "value": "low"}],
            },
        )
        assert created.status_code == 201
        assert created.json()["parameters"][0]["value"] == "low"

        rule_id = created.json()["id"]
        updated = await client.put(
            f"/api/verification/rules/{rule_id}",
            json={"parameters": [{"name": "temperature", "type": "float", "value": 0.9}]},
        )
        assert updated.status_code == 200
        assert [p["name"] for p in updated.json()["parameters"]] == ["temperature"]

        listed = (await client.get("/api/verification/rules")).json()["rules"][0]
        assert listed["parameters"][0]["name"] == "temperature"

    @pytest.mark.asyncio
    async def test_a_rename_does_not_clear_parameters(self, admin_client):
        """The update handler feeds a generic setattr loop; `parameters` has to
        be intercepted there or it writes an attribute the ORM does not map and
        silently persists nothing."""
        client, _, _ = admin_client
        created = await client.post(
            "/api/verification/rules",
            json={
                "name": "Tone",
                "prompt": "p",
                "parameters": [{"name": "reasoning_effort", "type": "string", "value": "low"}],
            },
        )
        rule_id = created.json()["id"]

        renamed = await client.put(f"/api/verification/rules/{rule_id}", json={"name": "Tone 2"})
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Tone 2"
        assert renamed.json()["parameters"][0]["value"] == "low"

    @pytest.mark.asyncio
    async def test_invalid_rule_parameters_are_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/verification/rules",
            json={
                "name": "Tone",
                "prompt": "p",
                "parameters": [{"name": "stream", "type": "boolean", "value": True}],
            },
        )
        assert resp.status_code == 422


class TestMapStageApi:
    @pytest.mark.asyncio
    async def test_stage_parameters_round_trip(self, admin_client):
        client, _, _ = admin_client
        created = await client.post(
            "/api/maps",
            json={
                "name": "M",
                "stages": [
                    {
                        "name": "S1",
                        "parameters": [
                            {"name": "reasoning_effort", "type": "string", "value": "low"}
                        ],
                        "verification_parameters": [
                            {"name": "max_tokens", "type": "integer", "value": 50}
                        ],
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        stage = created.json()["stages"][0]
        assert stage["parameters"][0]["value"] == "low"
        assert stage["verification_parameters"][0]["value"] == 50

    @pytest.mark.asyncio
    async def test_a_stage_names_two_models_and_keeps_two_layers_apart(self, admin_client):
        """Generation and verification are separate calls on separate models, so
        the two lists must not bleed into one another."""
        client, _, _ = admin_client
        created = await client.post(
            "/api/maps",
            json={
                "name": "M",
                "stages": [
                    {
                        "name": "S1",
                        "parameters": [{"name": "temperature", "type": "float", "value": 0.9}],
                        "verification_parameters": [
                            {"name": "temperature", "type": "float", "value": 0.0}
                        ],
                    }
                ],
            },
        )
        stage = created.json()["stages"][0]
        assert stage["parameters"][0]["value"] == 0.9
        assert stage["verification_parameters"][0]["value"] == 0.0

    @pytest.mark.asyncio
    async def test_invalid_stage_parameters_are_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/maps",
            json={
                "name": "M",
                "stages": [
                    {
                        "name": "S1",
                        "parameters": [{"name": "stream", "type": "boolean", "value": True}],
                    }
                ],
            },
        )
        assert resp.status_code == 422


class TestScenarioRuleApi:
    @pytest.mark.asyncio
    async def test_scenario_rule_parameters_round_trip(self, admin_client):
        client, _, _ = admin_client
        created = await client.post(
            "/api/scenario-rules",
            json={
                "name": "R",
                "prompt": "p",
                "parameters": [{"name": "temperature", "type": "float", "value": 0.15}],
            },
        )
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]
        assert created.json()["parameters"][0]["value"] == 0.15

        updated = await client.put(
            f"/api/scenario-rules/{rule_id}",
            json={"parameters": [{"name": "max_tokens", "type": "integer", "value": 512}]},
        )
        assert updated.status_code == 200
        assert [p["name"] for p in updated.json()["parameters"]] == ["max_tokens"]


class TestSettingsApi:
    @pytest.mark.asyncio
    async def test_three_role_layers_round_trip(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put(
            "/api/settings",
            json={
                "parameters": [{"name": "temperature", "type": "float", "value": 0.7}],
                "verification_parameters": [
                    {"name": "temperature", "type": "float", "value": 0.1}
                ],
                "summarization_parameters": [
                    {"name": "temperature", "type": "float", "value": 0.2}
                ],
            },
        )
        assert resp.status_code == 200
        got = (await client.get("/api/settings")).json()
        assert got["parameters"][0]["value"] == 0.7
        assert got["verification_parameters"][0]["value"] == 0.1
        assert got["summarization_parameters"][0]["value"] == 0.2

    @pytest.mark.asyncio
    async def test_the_three_layers_are_independent(self, admin_client):
        client, _, _ = admin_client
        await client.put(
            "/api/settings",
            json={"parameters": [{"name": "temperature", "type": "float", "value": 0.7}]},
        )
        got = (await client.get("/api/settings")).json()
        assert got["parameters"][0]["value"] == 0.7
        assert got["verification_parameters"] == []
        assert got["summarization_parameters"] == []
