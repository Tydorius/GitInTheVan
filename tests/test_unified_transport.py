"""Phase 24: every outbound LLM call goes through one forwarder.

`endpoint.provider` -- the switch that routes a call through LiteLLM -- used to
be read by `proxy._do_forward` and nowhere else. The verification judge, the
verification retry, both summarizers and both map-stage calls each built an
OpenAI-shaped body and POSTed it straight at `endpoint.base_url`, so an endpoint
set to `gemini` or `anthropic` worked as the Driver and silently failed
everywhere else.

The judge and summarizer cases live with their own suites
(`test_verification.py`, `test_summarization.py`). This file covers the map
stages, the metrics those calls now emit, and the two smaller gaps closed
alongside them.

Two properties matter more than any single assertion here:

1. a `provider` endpoint reaches LiteLLM at every site, and
2. a `provider=""` endpoint sends byte-identical requests to the same URLs it
   always did -- that is the configuration in real use, and it must not move.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.models.endpoint import Endpoint
from app.models.map import MapStage

OK = {"choices": [{"message": {"role": "assistant", "content": "text"}}]}
JUDGE_OK = {
    "choices": [
        {"message": {"role": "assistant", "content": '{"violation": false, "reason": ""}'}}
    ]
}


def _forward_returning(*results):
    """Patch the one forwarder every call site now shares."""
    calls: list[dict] = []
    queue = list(results)

    async def fake(method, url, headers, body, timeout, **kwargs):
        calls.append({"url": url, "headers": headers, "body": json.loads(body), **kwargs})
        return queue.pop(0) if queue else ({}, 500)

    return patch("app.services.proxy._do_forward", side_effect=fake), calls


def _endpoint(name="Stage EP", host="https://stage.test", provider="", api_base_path=""):
    return Endpoint(
        id=f"ep-{name}",
        user_id="user-1",
        name=name,
        base_url=host,
        api_key="sk-stage",
        api_base_path=api_base_path,
        provider=provider,
        enabled=True,
    )


def _stage(**kwargs):
    defaults = {
        "id": "stage-1",
        "map_id": "map-1",
        "stage_order": 0,
        "name": "Draft",
        "system_instructions": "Write.",
        "verification_enabled": True,
        "verification_instructions": "Check it",
        "verification_max_retries": 0,
        "verification_parameters_json": "[]",
        "parameters_json": "[]",
    }
    defaults.update(kwargs)
    return MapStage(**defaults)


# ============================================================================
# Map stage generation
# ============================================================================

class TestMapStageGeneration:
    @pytest.mark.asyncio
    async def test_a_stage_honours_the_endpoint_provider(self):
        from app.services.map_pipeline import _forward_stage_llm

        patcher, calls = _forward_returning((OK, 200))
        with patcher:
            data, status = await _forward_stage_llm(
                {"messages": [{"role": "user", "content": "hi"}]},
                [_endpoint(provider="anthropic")],
                "claude-sonnet-4",
                httpx.Timeout(30.0),
            )

        assert status == 200
        assert data == OK
        assert calls[0]["provider"] == "anthropic"
        assert calls[0]["base_url"] == "https://stage.test"
        assert calls[0]["body"]["model"] == "claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_the_raw_path_still_builds_the_same_url(self):
        """The control case. `api_base_path` handling moved to the shared
        `_build_upstream_url`; it must produce what the inline copy did."""
        from app.services.map_pipeline import _forward_stage_llm

        patcher, calls = _forward_returning((OK, 200))
        with patcher:
            await _forward_stage_llm(
                {"messages": []}, [_endpoint(api_base_path="/api")], "m",
                httpx.Timeout(30.0),
            )

        assert calls[0]["provider"] == ""
        assert calls[0]["url"] == "https://stage.test/api/chat/completions"

    @pytest.mark.asyncio
    async def test_failover_still_advances_and_stays_instrumented(self):
        """Phase 22 added per-candidate `record_llm_call` here because map stages
        were invisible to Debug. Swapping the transport must not lose it."""
        from app.services.debug import init_debug
        from app.services.map_pipeline import _forward_stage_llm

        body_json = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}
        init_debug(body_json, [])

        patcher, calls = _forward_returning(({"error": {}}, 502), (OK, 200))
        with patcher:
            _, status = await _forward_stage_llm(
                body_json,
                [_endpoint(name="First", host="https://a.test"),
                 _endpoint(name="Second", host="https://b.test")],
                "m",
                httpx.Timeout(30.0),
                stage_index=0,
            )

        assert status == 200
        assert [c["base_url"] for c in calls] == ["https://a.test", "https://b.test"]
        recorded = body_json["_gitv_debug"]["run"]["llm_calls"]
        assert [c["purpose"] for c in recorded] == ["map_stage", "map_stage"]
        assert [c["failover_attempt"] for c in recorded] == [0, 1]


# ============================================================================
# Map stage verification
# ============================================================================

class TestMapStageVerification:
    @pytest.mark.asyncio
    async def test_stage_verification_honours_the_provider(self, monkeypatch):
        from app.services import map_pipeline

        endpoint = _endpoint(provider="gemini")

        async def fake_resolve(db, stage, user_id):
            return endpoint, "gemini-2.0-flash"

        async def fake_params(db, stage, ep, model, user_id):
            return {}

        async def fake_body(content, stage):
            return [{"role": "user", "content": content}]

        monkeypatch.setattr(map_pipeline, "_resolve_verification_endpoint", fake_resolve)
        monkeypatch.setattr(map_pipeline, "_stage_verification_parameters", fake_params)
        monkeypatch.setattr(map_pipeline, "_build_stage_verification_body", fake_body)

        patcher, calls = _forward_returning((JUDGE_OK, 200))
        with patcher:
            approved, reason, errored = await map_pipeline._verify_stage(
                "some output", _stage(), "user-1"
            )

        assert (approved, reason, errored) == (True, "", False)
        assert calls[0]["provider"] == "gemini"

    @pytest.mark.asyncio
    async def test_an_unreachable_stage_judge_is_marked_not_checked(self, monkeypatch):
        """A stage whose judge dies is approved so the pipeline keeps moving.
        Without the flag that is indistinguishable from a stage that passed."""
        from app.services import map_pipeline

        async def fake_resolve(db, stage, user_id):
            return _endpoint(), "m"

        async def fake_params(db, stage, ep, model, user_id):
            return {}

        async def fake_body(content, stage):
            return [{"role": "user", "content": content}]

        monkeypatch.setattr(map_pipeline, "_resolve_verification_endpoint", fake_resolve)
        monkeypatch.setattr(map_pipeline, "_stage_verification_parameters", fake_params)
        monkeypatch.setattr(map_pipeline, "_build_stage_verification_body", fake_body)

        patcher, _ = _forward_returning(({"error": {}}, 404))
        with patcher:
            approved, reason, errored = await map_pipeline._verify_stage(
                "some output", _stage(name="Draft"), "user-1"
            )

        assert approved is True
        assert errored is True
        assert reason == (
            "Verification resulted in a 404 error, so stage 'Draft' was not checked. "
            "The stage output was kept unverified."
        )


# ============================================================================
# Metrics that existed but were never emitted
# ============================================================================

class TestPurposesFinallyEmitted:
    @pytest.mark.asyncio
    async def test_the_judge_call_lands_in_the_run_metrics(self):
        """`PURPOSE_VERIFICATION` has been defined in `debug_metrics` since
        Phase 22 and no call site ever passed it, so judge cost was invisible."""
        from app.models.verification import VerificationRule
        from app.services.debug import init_debug
        from app.services.verification import check_response

        body_json = {"messages": [], "model": "m"}
        init_debug(body_json, [])

        rule = VerificationRule(
            id="rule-1", user_id="user-1", name="Tone", prompt="check",
            is_active=True, max_retries=0, execution_order=0,
        )

        patcher, _ = _forward_returning((JUDGE_OK, 200))
        with patcher:
            await check_response(
                "text", [rule], _endpoint(name="Judge"), "judge-model",
                body_json=body_json,
            )

        calls = body_json["_gitv_debug"]["run"]["llm_calls"]
        assert [c["purpose"] for c in calls] == ["verification_judge"]
        assert calls[0]["model_requested"] == "judge-model"
        assert calls[0]["status_code"] == 200

    @pytest.mark.asyncio
    async def test_a_failed_judge_candidate_is_recorded_too(self):
        """A run whose first judge endpoint 502'd and whose second answered spent
        both latencies; recording only the winner would misattribute the time."""
        from app.models.verification import VerificationRule
        from app.services.debug import init_debug
        from app.services.routing import FailoverEndpoint
        from app.services.verification import check_response

        body_json = {"messages": [], "model": "m"}
        init_debug(body_json, [])

        rule = VerificationRule(
            id="rule-1", user_id="user-1", name="Tone", prompt="check",
            is_active=True, max_retries=0, execution_order=0,
        )
        chain = [
            FailoverEndpoint(base_url="https://a.test", endpoint_name="A"),
            FailoverEndpoint(base_url="https://b.test", endpoint_name="B"),
        ]

        patcher, _ = _forward_returning(({"error": {}}, 502), (JUDGE_OK, 200))
        with patcher:
            await check_response(
                "text", [rule], _endpoint(), "m",
                rule_chains={rule.id: chain}, body_json=body_json,
            )

        calls = body_json["_gitv_debug"]["run"]["llm_calls"]
        assert len(calls) == 2
        assert [c["endpoint_name"] for c in calls] == ["A", "B"]
        assert [c["status_code"] for c in calls] == [502, 200]


# ============================================================================
# Through the real pipeline
# ============================================================================

class TestThroughTheRealPipeline:
    """Driven through `/v1/chat/completions` with a mocked upstream, then read
    back from the stored run -- the recorders in isolation are not the thing that
    broke. Maps ran uninstrumented for two months behind passing unit tests."""

    async def _setup(self, client):
        from tests.test_debug_pipeline import enable_debug

        uid = await enable_debug(client)
        ep = await client.post("/api/endpoints", json={
            "name": "Main", "base_url": "http://ep.test", "api_key": "sk-1",
        })
        assert ep.status_code == 201, ep.text
        endpoint_id = ep.json()["id"]
        await client.put("/api/settings", json={"default_endpoint_id": endpoint_id})
        rule = await client.post("/api/verification/rules", json={
            "name": "No Purple Prose", "prompt": "Check tone",
        })
        assert rule.status_code == 201, rule.text
        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": endpoint_id,
            "verification_model": "judge-model",
        })
        return uid

    @pytest.mark.asyncio
    async def test_the_judge_call_appears_in_the_stored_run(self, admin_client, httpx_mock):
        from tests.test_debug_pipeline import latest_run

        client, _, api_key = admin_client
        uid = await self._setup(client)

        url = "http://ep.test/v1/chat/completions"
        httpx_mock.add_response(url=url, json=OK)
        httpx_mock.add_response(url=url, json=JUDGE_OK)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

        run = await latest_run(uid)
        purposes = [c["purpose"] for c in run["pipeline_data"]["run"]["llm_calls"]]
        assert purposes == ["main", "verification_judge"]

        totals = run["pipeline_data"]["run"]["totals"]
        assert totals["llm_call_count"] == 2, "the judge's cost is still not counted"

    @pytest.mark.asyncio
    async def test_a_dead_judge_says_so_in_the_run(self, admin_client, httpx_mock):
        """The end the whole phase turns on: the reply still goes through, and
        the run states that the rule did not process."""
        from tests.test_debug_pipeline import latest_run

        client, _, api_key = admin_client
        uid = await self._setup(client)

        url = "http://ep.test/v1/chat/completions"
        httpx_mock.add_response(url=url, json=OK)
        httpx_mock.add_response(url=url, status_code=404, json={"error": "nope"})

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "text"

        run = await latest_run(uid)
        note = run["verification_data"]["check_history"][0]["thinking"]
        assert note == (
            "Verification resulted in a 404 error, so rule 'No Purple Prose' did not "
            "process. The response was returned unchecked."
        )

        logs = (await client.get("/api/verification/logs")).json()["logs"]
        assert "did not process" in logs[0]["violation_reason"], (
            "the Logs page still shows an approved row with a blank reason"
        )


# ============================================================================
# Client parameters survive a replay
# ============================================================================

class TestOriginalParams:
    def test_init_debug_captures_the_clients_own_parameters(self):
        from app.services.debug import init_debug

        body_json = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "temperature": 0.9,
            "reasoning_effort": "high",
            "_gitv_provider": "gemini",
        }
        init_debug(body_json, [])

        params = body_json["_gitv_debug"]["original_params"]
        assert params == {"temperature": 0.9, "reasoning_effort": "high"}

    def test_pipeline_owned_and_unserializable_keys_are_excluded(self):
        """`_gitv_failover_chain` holds live `FailoverEndpoint` objects, and the
        trace gets serialized."""
        from app.services.debug import original_client_params
        from app.services.routing import FailoverEndpoint

        params = original_client_params({
            "model": "m", "messages": [], "stream": True,
            "_gitv_failover_chain": [FailoverEndpoint(base_url="x")],
            "top_p": 0.4,
        })
        assert params == {"top_p": 0.4}

    def test_replay_rebuilds_the_body_with_the_original_parameters(self):
        """A replay that sends `{model, messages, stream}` only diverges from the
        run it claims to reproduce."""
        from app.services.debug_replay import _original_params

        exchange = {"pipeline_data": {"original_params": {"reasoning_effort": "max"}}}
        assert _original_params(exchange) == {"reasoning_effort": "max"}

    def test_a_trace_captured_before_this_replays_as_it_did(self):
        from app.services.debug_replay import _original_params

        assert _original_params({"pipeline_data": {"stages": []}}) == {}
        assert _original_params({}) == {}

    def test_reserved_keys_cannot_come_back_through_a_replay(self):
        """Defence in depth: the capture already excludes these, but a trace is
        stored data and the rebuild must not trust it."""
        from app.services.debug_replay import _original_params

        exchange = {
            "pipeline_data": {
                "original_params": {
                    "messages": [{"role": "user", "content": "injected"}],
                    "stream": True,
                    "_gitv_replay": False,
                    "temperature": 0.5,
                }
            }
        }
        assert _original_params(exchange) == {"temperature": 0.5}
