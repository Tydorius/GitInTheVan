"""Phase 17 — Passenger: scripted LLM calls from cantrips.

Covers the refresh doc's test list: the gate matrix, the rule-22 two-half
budget clamp, host-side cap enforcement, payload caps, tag -> chain ->
default-fallback resolution with a cross-user exclusion row, per-attempt
failover with layer re-resolution, record_llm_call rows and debug captures
on success and every failure, the degradation contract (every failure
resolves to null for the script and a forwarded, untouched request), the
Deno sentinel protocol (async wrap, multi-call, interleaved ids, garbage
host lines ignored), sandbox-flagged Tester rows, the async-wrapped syntax
validator, and the 058 upgrade path with a vacuity check.

No content-parts stripper test exists because no stripper exists: the old
design's <ADDITIONAL INSTRUCTIONS> fallback was removed (decisions.md,
2026-11-18) — scripts branch on context.passenger.enabled instead.

Handler-level tests build a PassengerHandler against an in-memory body with
the metrics accumulator and debug trace initialized, and mock only the outer
boundary (upstream HTTP via httpx_mock), per the mocking standard. E2E tests
drive /v1/chat/completions end to end with distinct mock URLs for the
Passenger and Driver roles.
"""
import json

import pytest

from app.services.deno_runner import DENO_PATH, validate_cantrip_syntax
from tests.conftest import TestSessionLocal

SKIP_DENO = not DENO_PATH

# Cantrip code that turns one Passenger call into a personality block, so the
# E2E assertions can read the result straight off the forwarded driver body.
PASSENGER_SCRIPT = """
const r = await context.passenger.call({
    tag: "tool_use",
    instructions: "Generate a one-line NPC description.",
    context: "A fantasy tavern scene.",
    model: "",
});
context.character.personality = r === null ? "" : String(r);
"""

DOUBLE_CALL_SCRIPT = """
const a = await context.passenger.call({
    tag: "tool_use",
    instructions: "First.", context: "c1", model: "",
});
const b = await context.passenger.call({
    tag: "tool_use",
    instructions: "Second.", context: "c2", model: "",
});
context.character.personality = (a || "null") + " | " + (b || "null");
"""


def _chat_body(content: str, **overrides):
    body = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a narrator."},
            {"role": "user", "content": content},
        ],
        "stream": False,
    }
    body.update(overrides)
    return body


def _upstream(content: str = "The gates open."):
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


async def _make_endpoint(client, name, host, role_tag="default", **overrides):
    payload = {
        "name": name, "base_url": f"http://{host}", "api_key": "sk-test",
        "role_tag": role_tag, "enabled": True,
    }
    payload.update(overrides)
    resp = await client.post("/api/endpoints", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _make_cantrip(client, code, name="psg", **overrides):
    payload = {"name": name, "code": code, "run_pre_driver": True, "is_active": True}
    payload.update(overrides)
    resp = await client.post("/api/cantrips", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _traced_body(user_id: str, messages=None) -> dict:
    """A body carrying the metrics accumulator and debug trace, so handler
    activity lands in both exactly as it would mid-pipeline."""
    from app.services.debug import init_debug
    from app.services.metrics import init_metrics

    body = {"model": "test-model", "messages": messages or [
        {"role": "user", "content": "Hello"}], "stream": False}
    init_metrics(body, user_id)
    init_debug(body, [])
    return body


async def _llm_rows(purpose="passenger"):
    from sqlalchemy import select

    from app.models.metrics import LlmCallMetric
    async with TestSessionLocal() as db:
        rows = (await db.execute(
            select(LlmCallMetric).where(LlmCallMetric.purpose == purpose)
        )).scalars().all()
        return rows


async def _handler(body, user_id) -> "object":
    from app.services.passenger import get_passenger_handler

    return await get_passenger_handler(body, user_id)


def _stage_names(body) -> list[str]:
    return [s["name"] for s in body.get("_gitv_debug", {}).get("stages", [])]


async def _me(client) -> str:
    return (await client.get("/api/auth/me")).json()["id"]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestGates:
    async def test_both_gates_on_by_default(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        handler = await _handler({}, uid)
        assert handler.enabled is True

    async def test_admin_gate_off_disables(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        try:
            resp = await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": False})
            assert resp.status_code == 200
            handler = await _handler({}, uid)
            assert handler.enabled is False
        finally:
            await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": True})

    async def test_user_gate_off_disables(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        try:
            resp = await client.put("/api/settings", json={
                "passenger_calling_enabled": False})
            assert resp.status_code == 200
            handler = await _handler({}, uid)
            assert handler.enabled is False
        finally:
            await client.put("/api/settings", json={
                "passenger_calling_enabled": True})

    async def test_missing_settings_row_means_enabled(self, admin_client):
        """The lexicon precedent: a user who never opened Settings gets the
        defaults, not a silently disabled feature."""
        from sqlalchemy import delete

        from app.models.user_settings import UserSettings
        client, _, _ = admin_client
        uid = await _me(client)
        async with TestSessionLocal() as db:
            await db.execute(delete(UserSettings).where(UserSettings.user_id == uid))
            await db.commit()
        handler = await _handler({}, uid)
        assert handler.enabled is True


# ---------------------------------------------------------------------------
# Rule 22: two-half budgets
# ---------------------------------------------------------------------------


class TestBudgetHalves:
    def test_watchdog_arithmetic(self):
        """Script budget + every Passenger call's upstream timeout + slack.
        The cantrip default 5s alone would kill every Passenger run."""
        from app.services.passenger import WATCHDOG_SLACK_MS, PassengerHandler

        h = PassengerHandler({}, "u", enabled=True, max_calls=3,
                             timeout_seconds=60, passenger_parameters_json="[]")
        assert h.watchdog_ms(5000) == 5000 + 3 * 60 * 1000 + WATCHDOG_SLACK_MS

    async def test_user_budget_zero_means_admin_cap(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        await client.put("/api/settings", json={"passenger_call_budget": 0})
        handler = await _handler({}, uid)
        assert handler.max_calls == 3  # admin default

    async def test_user_budget_below_admin_cap_wins(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        try:
            await client.put("/api/settings", json={"passenger_call_budget": 2})
            handler = await _handler({}, uid)
            assert handler.max_calls == 2
        finally:
            await client.put("/api/settings", json={"passenger_call_budget": 0})

    async def test_user_budget_above_admin_cap_clamps(self, admin_client):
        """Neither half can raise the other's limit (rule 22)."""
        client, _, _ = admin_client
        uid = await _me(client)
        try:
            await client.put("/api/settings", json={"passenger_call_budget": 10})
            handler = await _handler({}, uid)
            assert handler.max_calls == 3
        finally:
            await client.put("/api/settings", json={"passenger_call_budget": 0})

    async def test_settings_surface_the_effective_cap(self, admin_client):
        """The clamp is visible next to the field, not mysterious."""
        client, _, _ = admin_client
        try:
            await client.put("/api/settings", json={"passenger_call_budget": 1})
            s = (await client.get("/api/settings")).json()
            assert s["passenger_effective_cap"] == 1
        finally:
            await client.put("/api/settings", json={"passenger_call_budget": 0})
        s = (await client.get("/api/settings")).json()
        assert s["passenger_effective_cap"] == 3


# ---------------------------------------------------------------------------
# Handler transport (outer boundary mocked only)
# ---------------------------------------------------------------------------


class TestHandlerTransport:
    async def test_happy_path_shapes_request_and_records(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Driver", "driver.test")
        psg_id = await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")

        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("A gruff dwarf named Borin."))

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, outcome = await handler.call(
            tag="tool_use",
            instructions="Generate an NPC.",
            context="fantasy tavern",
            model="",
        )
        assert text == "A gruff dwarf named Borin."
        assert "psg.test" not in text
        assert outcome  # the endpoint/model summary

        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent["messages"] == [
            {"role": "system", "content": "Generate an NPC."},
            {"role": "user", "content": "fantasy tavern"},
        ]
        # stream is owned by the pipeline: a configured layer cannot flip it.
        assert sent["stream"] is False
        assert sent["model"]  # endpoint default or gpt-4 fallback

        rows = body["_gitv_metrics"]["rows_llm"]
        assert len(rows) == 1
        assert rows[0]["purpose"] == "passenger"
        assert rows[0]["endpoint_id"] == psg_id
        assert rows[0]["failover_attempt"] == 0
        # Captures on all three timeline kinds (success: call + result).
        assert "passenger_call_1" in _stage_names(body)
        assert "passenger_result_1" in _stage_names(body)

    async def test_tag_resolves_priority_order_not_creation_order(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools low", "low.test",
                             role_tag="tool_use", priority=9)
        await _make_endpoint(client, "Tools high", "high.test",
                             role_tag="tool_use", priority=1)

        httpx_mock.add_response(url="http://high.test/v1/chat/completions",
                                json=_upstream("from high priority"))

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text == "from high priority"
        assert len(httpx_mock.get_requests()) == 1

    async def test_unmatched_tag_falls_back_to_default_chain(
        self, admin_client, httpx_mock
    ):
        """Open question 5, default: a typo'd or empty-matching tag falls
        back to the user's default endpoint chain (verification precedent)."""
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Driver", "driver.test")

        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("default chain answer"))

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, _ = await handler.call(
            tag="no-such-tag", instructions="i", context="c", model="")
        assert text == "default chain answer"

    async def test_other_users_endpoints_never_resolve(
        self, admin_client, httpx_mock
    ):
        """Cross-user exclusion needs a row that must be excluded: a second
        user owns an endpoint with the same role tag; only the caller's
        endpoint may serve the call."""
        from app.models.endpoint import Endpoint

        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Mine", "mine.test", role_tag="tool_use")
        async with TestSessionLocal() as db:
            db.add(Endpoint(user_id="00000000-0000-0000-0000-0000000000ee",
                            name="Theirs", base_url="http://theirs.test",
                            api_key="sk-x", role_tag="tool_use", priority=0,
                            enabled=True))
            await db.commit()

        httpx_mock.add_response(url="http://mine.test/v1/chat/completions",
                                json=_upstream("mine"))

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text == "mine"
        for req in httpx_mock.get_requests():
            assert "theirs.test" not in str(req.url)

    async def test_failover_advances_and_reresolves_layers_per_attempt(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        # Two tagged endpoints: the first fails with 500 and carries its own
        # endpoint parameter layer (temperature 0.9); the second succeeds with
        # a different one (0.1). Per-attempt re-resolution means each forwarded
        # body carries its candidate's layer, not the first candidate's.
        await _make_endpoint(client, "Psg A", "a.test", role_tag="tool_use", priority=1,
                             parameters=[{"name": "temperature", "value": 0.9}])
        await _make_endpoint(client, "Psg B", "b.test", role_tag="tool_use", priority=2,
                             parameters=[{"name": "temperature", "value": 0.1}])

        httpx_mock.add_response(url="http://a.test/v1/chat/completions",
                                status_code=500, json={"error": "boom"})
        httpx_mock.add_response(url="http://b.test/v1/chat/completions",
                                json=_upstream("from B"))

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text == "from B"

        reqs = httpx_mock.get_requests()
        assert len(reqs) == 2
        # Parameter values keep their configured type through the layer
        # system; compare numerically so either representation passes.
        assert float(json.loads(reqs[0].content)["temperature"]) == 0.9
        assert float(json.loads(reqs[1].content)["temperature"]) == 0.1

        rows = body["_gitv_metrics"]["rows_llm"]
        assert len(rows) == 2  # every attempt recorded, failure included
        assert rows[0]["status_code"] == 500
        assert rows[0]["failover_attempt"] == 0
        assert rows[1]["failover_attempt"] == 1

    async def test_user_parameter_layer_applies(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        try:
            resp = await client.put("/api/settings", json={
                "passenger_parameters": [{"name": "max_tokens", "value": 123}]})
            assert resp.status_code == 200
            httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                    json=_upstream("layered"))
            body = _traced_body(uid)
            handler = await _handler(body, uid)
            text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
            assert text == "layered"
            assert float(json.loads(httpx_mock.get_requests()[0].content)["max_tokens"]) == 123
        finally:
            await client.put("/api/settings", json={"passenger_parameters": []})

    async def test_empty_output_is_failure_not_empty_string(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("   "))
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, error = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text is None
        assert error == "empty"

    async def test_chain_exhaustion_records_every_attempt(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Psg A", "a.test", role_tag="tool_use", priority=1)
        await _make_endpoint(client, "Psg B", "b.test", role_tag="tool_use", priority=2)
        httpx_mock.add_response(url="http://a.test/v1/chat/completions", status_code=500, json={})
        httpx_mock.add_response(url="http://b.test/v1/chat/completions", status_code=502, json={})

        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, error = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text is None
        assert error == "chain_failed"
        assert len(body["_gitv_metrics"]["rows_llm"]) == 2
        assert "passenger_error_1" in _stage_names(body)

    async def test_no_endpoint_at_all(self, admin_client):
        client, _, _ = admin_client
        uid = await _me(client)
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, error = await handler.call(tag="", instructions="i", context="c", model="")
        assert text is None
        assert error == "no_endpoint"
        assert "passenger_error_1" in _stage_names(body)

    async def test_host_side_cap_refuses_past_budget(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("first"))
        try:
            await client.put("/api/settings", json={"passenger_call_budget": 1})
            body = _traced_body(uid)
            handler = await _handler(body, uid)
            assert handler.max_calls == 1
            text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
            assert text == "first"
            text2, error2 = await handler.call(tag="tool_use", instructions="i", context="c", model="")
            assert text2 is None
            assert error2 == "cap"
            assert len(httpx_mock.get_requests()) == 1  # refused without a round trip
            assert "passenger_error_2" in _stage_names(body)
        finally:
            await client.put("/api/settings", json={"passenger_call_budget": 0})

    @pytest.mark.parametrize("field,length", [
        ("instructions", 8001), ("context", 24001),
    ])
    async def test_payload_caps_refuse_without_upstream_call(
        self, admin_client, httpx_mock, field, length
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        kwargs = {"tag": "", "instructions": "i", "context": "c", "model": ""}
        kwargs[field] = "x" * length
        text, error = await handler.call(**kwargs)
        assert text is None
        assert error == "oversize"
        assert httpx_mock.get_requests() == []
        assert "passenger_error_1" in _stage_names(body)

    async def test_result_truncated_at_cap_with_marker(
        self, admin_client, httpx_mock
    ):
        from app.services.passenger import RESULT_CAP, RESULT_TRUNCATION_MARKER

        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("y" * (RESULT_CAP + 100)))
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, _ = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert len(text) == RESULT_CAP + len(RESULT_TRUNCATION_MARKER)
        assert text.endswith(RESULT_TRUNCATION_MARKER)

    async def test_timeout_maps_to_timeout_error(
        self, admin_client, monkeypatch
    ):
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")

        import app.services.proxy as proxy_module

        async def _hang(*args, **kwargs):
            raise TimeoutError("simulated upstream timeout")

        monkeypatch.setattr(proxy_module, "_do_forward", _hang)
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        text, error = await handler.call(tag="tool_use", instructions="i", context="c", model="")
        assert text is None
        assert error == "timeout"
        assert "passenger_error_1" in _stage_names(body)

    async def test_injection_flags_write_audit_rows(
        self, admin_client, httpx_mock
    ):
        from sqlalchemy import select

        from app.models.audit_log import AuditLog
        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("ok"))
        body = _traced_body(uid)
        handler = await _handler(body, uid)
        await handler.call(tag="tool_use", instructions="invis\u200bible",
                           context="c", model="")
        async with TestSessionLocal() as db:
            rows = (await db.execute(select(AuditLog).where(
                AuditLog.action == "content_injection_flag"))).scalars().all()
        assert any("passenger:1:instructions" in (r.target_type or "") for r in rows)


# ---------------------------------------------------------------------------
# Protocol and pipeline (Deno required)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SKIP_DENO, reason="Deno runtime not available")
class TestPassengerPipeline:
    async def test_e2e_call_reaches_upstream_and_splices_result(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, PASSENGER_SCRIPT)
        await client.put("/api/settings", json={"debug_mode": True})

        # Distinct URLs per role: passenger first, then the driver forward.
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("A gruff dwarf named Borin."))
        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("The tavern door swings open."))

        resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                 headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200

        reqs = httpx_mock.get_requests()
        assert len(reqs) == 2
        psg_sent = json.loads(reqs[0].content)
        assert psg_sent["messages"][0] == {
            "role": "system", "content": "Generate a one-line NPC description."}
        driver_sent = json.loads(reqs[1].content)
        # The splice: the passenger result reached the driver prompt.
        assert "A gruff dwarf named Borin." in json.dumps(driver_sent["messages"])

        rows = await _llm_rows()
        assert any(r.purpose == "passenger" and r.context == "live" for r in rows)

    async def test_disabled_returns_null_and_makes_no_call(
        self, admin_client, httpx_mock
    ):
        """The removed fallback's replacement: the script branches on
        context.passenger.enabled; the host injects nothing anywhere."""
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, PASSENGER_SCRIPT)
        try:
            await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": False})
            httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                    json=_upstream("plain reply"))
            resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                     headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 200
            reqs = httpx_mock.get_requests()
            assert len(reqs) == 1  # driver only: zero passenger traffic
            driver_sent = json.loads(reqs[0].content)
            # Untouched request: no injected block, no stripped content.
            assert json.dumps(driver_sent["messages"]).count("[Personality]") == 0
        finally:
            await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": True})

    async def test_cap_enforced_host_side_across_one_request(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, DOUBLE_CALL_SCRIPT)
        try:
            await client.put("/api/admin/settings", json={
                "max_passenger_calls_per_request": 1})
            httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                    json=_upstream("first"))
            httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                    json=_upstream("ok"))
            resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                     headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 200
            psg_reqs = [r for r in httpx_mock.get_requests()
                        if "psg.test" in str(r.url)]
            assert len(psg_reqs) == 1  # the second call was refused host-side
        finally:
            await client.put("/api/admin/settings", json={
                "max_passenger_calls_per_request": 3})

    async def test_sequential_calls_interleave_correctly(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, DOUBLE_CALL_SCRIPT)
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("FIRST-RESULT"))
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("SECOND-RESULT"))
        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("ok"))

        resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                 headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        reqs = httpx_mock.get_requests()
        # Id matching: each reply resolved its own call, not the other's.
        driver_body = json.dumps(json.loads(reqs[-1].content))
        assert "FIRST-RESULT" in driver_body and "SECOND-RESULT" in driver_body

    async def test_host_garbage_lines_are_ignored_by_the_template(
        self, admin_client, httpx_mock, monkeypatch
    ):
        """A host bug writing a malformed line before the real reply must
        not break the call: the template ignores what it cannot parse."""
        import app.services.deno_runner as runner

        original = runner._answer_passenger_call

        async def noisy(proc, handler, msg):
            try:
                proc.stdin.write(b"this is not json\n")
                await proc.stdin.drain()
            except Exception:
                pass
            return await original(proc, handler, msg)

        monkeypatch.setattr(runner, "_answer_passenger_call", noisy)

        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, PASSENGER_SCRIPT)
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("survived the garbage"))
        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("ok"))

        resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                 headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        driver_body = json.dumps(json.loads(httpx_mock.get_requests()[-1].content))
        assert "survived the garbage" in driver_body

    async def test_upstream_failure_forwards_request_untouched(
        self, admin_client, httpx_mock
    ):
        """Degradation contract: every passenger candidate fails; the request
        forwards with exactly the messages it arrived with."""
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(client, PASSENGER_SCRIPT)
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                status_code=500, json={})
        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("still works"))

        resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                 headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert "still works" in resp.json()["choices"][0]["message"]["content"]

        driver_sent = json.loads(httpx_mock.get_requests()[-1].content)
        original = _chat_body("Hello")["messages"]
        # The cantrip set personality to "" (null result): no injection block,
        # message list identical to the original request.
        assert driver_sent["messages"] == original

        rows = await _llm_rows()
        assert any(r.status_code == 500 and r.failover_attempt == 0 for r in rows)

    async def test_watchdog_kills_hung_script_and_request_survives(
        self, admin_client, httpx_mock
    ):
        """A script that never finishes is killed by the watchdog (script
        budget + passenger time + slack), the cantrip is skipped, and the
        request still forwards."""
        client, _, api_key = admin_client
        await _make_endpoint(client, "Driver", "driver.test")
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        await _make_cantrip(
            client, "await new Promise(() => {});", name="hang",
            timeout_ms=200,
        )
        httpx_mock.add_response(url="http://driver.test/v1/chat/completions",
                                json=_upstream("survived"))

        resp = await client.post("/v1/chat/completions", json=_chat_body("Hello"),
                                 headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert "survived" in resp.json()["choices"][0]["message"]["content"]

    async def test_stage_index_threads_from_map_stage_passes(
        self, admin_client, httpx_mock
    ):
        """record_llm_call entries carry the map stage the call originated
        from (the design's stage_index threading)."""
        from app.services.cantrip import process_cantrips

        client, _, _ = admin_client
        uid = await _me(client)
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        cantrip_id = await _make_cantrip(client, PASSENGER_SCRIPT)
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("stage call"))

        body = _traced_body(uid)
        await process_cantrips(body, uid, {}, stage_index=2)
        calls = body["_gitv_debug"]["run"]["llm_calls"]
        assert calls and calls[0]["stage_index"] == 2
        assert calls[0]["purpose"] == "passenger"
        assert body["_gitv_debug"]["run"]["cantrips"][0]["id"] == cantrip_id


# ---------------------------------------------------------------------------
# Cantrip Tester integration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SKIP_DENO, reason="Deno runtime not available")
class TestCantripTester:
    async def test_tester_runs_passenger_marked_sandbox(
        self, admin_client, httpx_mock
    ):
        """The Tester exercises Passenger with the user's own gates against a
        synthetic body marked sandbox: rows are recorded but excluded from
        default aggregates."""
        client, _, _ = admin_client
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        httpx_mock.add_response(url="http://psg.test/v1/chat/completions",
                                json=_upstream("tested npc"))
        resp = await client.post("/api/cantrips/test", json={
            "code": PASSENGER_SCRIPT,
            "messages": [{"role": "user", "content": "Hello"}],
            "timeout_ms": 30000,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["error"] is None

        rows = await _llm_rows()
        assert any(r.purpose == "passenger" and r.context == "sandbox" for r in rows)

    async def test_tester_respects_admin_gate(self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        await _make_endpoint(client, "Tools", "psg.test", role_tag="tool_use")
        try:
            await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": False})
            resp = await client.post("/api/cantrips/test", json={
                "code": PASSENGER_SCRIPT,
                "messages": [{"role": "user", "content": "Hello"}],
                "timeout_ms": 30000,
            })
            assert resp.status_code == 200
            # Null result: personality stays empty, no upstream traffic.
            assert resp.json()["personality"] == ""
            assert httpx_mock.get_requests() == []
        finally:
            await client.put("/api/admin/settings", json={
                "passenger_calling_enabled": True})


# ---------------------------------------------------------------------------
# Async-wrapped syntax validator
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SKIP_DENO, reason="Deno runtime not available")
class TestValidatorAsyncWrap:
    def test_awaiting_passenger_call_validates(self):
        assert validate_cantrip_syntax(
            "const r = await context.passenger.call({tag: 'x'});"
        ) == ""

    def test_sync_only_code_still_validates(self):
        assert validate_cantrip_syntax(
            "context.character.personality = 'stoic';"
        ) == ""

    def test_broken_syntax_still_rejected(self):
        assert validate_cantrip_syntax("if (context.chat_data {") != ""


# ---------------------------------------------------------------------------
# Settings and admin API
# ---------------------------------------------------------------------------


class TestSettingsAndAdminAPI:
    async def test_settings_round_trip(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={
            "passenger_calling_enabled": False,
            "passenger_call_budget": 2,
            "passenger_parameters": [{"name": "temperature", "value": 0.4}],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["passenger_calling_enabled"] is False
        assert body["passenger_call_budget"] == 2
        # params_to_api round-trips full parameter definitions; assert the
        # subset the test cares about.
        assert [(p["name"], float(p["value"])) for p in body["passenger_parameters"]] == [
            ("temperature", 0.4)]
        assert body["passenger_effective_cap"] == 2
        await client.put("/api/settings", json={
            "passenger_calling_enabled": True,
            "passenger_call_budget": 0,
            "passenger_parameters": [],
        })

    async def test_admin_caps_round_trip(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/admin/settings", json={
            "passenger_calling_enabled": False,
            "max_passenger_calls_per_request": 5,
            "passenger_timeout_seconds": 30,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["passenger_calling_enabled"] is False
        assert body["max_passenger_calls_per_request"] == 5
        assert body["passenger_timeout_seconds"] == 30
        await client.put("/api/admin/settings", json={
            "passenger_calling_enabled": True,
            "max_passenger_calls_per_request": 3,
            "passenger_timeout_seconds": 60,
        })

    async def test_bad_parameters_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={
            "passenger_parameters": [{"name": "model", "value": "evil"}],
        })
        assert resp.status_code == 422  # reserved name, Phase 23 validator


# ---------------------------------------------------------------------------
# Upgrade path (rule 10)
# ---------------------------------------------------------------------------


class TestPhase17UpgradePath:
    @pytest.mark.asyncio
    async def test_migration_058_upgrades_user_and_admin_tables(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.base import Base
        from app.services.migrations import run_migrations

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # Simulate the pre-058 database. user_settings and
                # admin_settings are not migration-created, so
                # TestMigrationCoverage cannot catch a missing 058 ALTER for
                # them -- only this test can (the skills.budget_weight bug
                # class, rule 10).
                for col in (
                    "passenger_calling_enabled",
                    "passenger_call_budget",
                    "passenger_parameters_json",
                ):
                    await conn.execute(text(f"ALTER TABLE user_settings DROP COLUMN {col}"))
                for col in (
                    "passenger_calling_enabled",
                    "max_passenger_calls_per_request",
                    "passenger_timeout_seconds",
                ):
                    await conn.execute(text(f"ALTER TABLE admin_settings DROP COLUMN {col}"))

            # Vacuity check: this really reproduces the old schema.
            async with engine.connect() as conn:
                user_cols = {r[1] for r in (await conn.execute(
                    text("PRAGMA table_info(user_settings)"))).all()}
                admin_cols = {r[1] for r in (await conn.execute(
                    text("PRAGMA table_info(admin_settings)"))).all()}
            assert "passenger_parameters_json" not in user_cols
            assert "max_passenger_calls_per_request" not in admin_cols

            await run_migrations(engine)

            async with engine.connect() as conn:
                user_cols = {r[1] for r in (await conn.execute(
                    text("PRAGMA table_info(user_settings)"))).all()}
                admin_cols = {r[1] for r in (await conn.execute(
                    text("PRAGMA table_info(admin_settings)"))).all()}
            assert {"passenger_calling_enabled", "passenger_call_budget",
                    "passenger_parameters_json"} <= user_cols
            assert {"passenger_calling_enabled", "max_passenger_calls_per_request",
                    "passenger_timeout_seconds"} <= admin_cols

            # The real query the hot path runs, against the upgraded table.
            # ORM insert (raw SQL would need every NOT NULL column spelled
            # out; Python-side defaults only fire through the ORM).
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            from app.models.user import User
            from app.models.user_settings import UserSettings

            upgraded = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with upgraded() as session:
                session.add(User(id="u1", username="u", password_hash="x",
                                 gitv_api_key="gak-u1"))
                await session.flush()
                session.add(UserSettings(user_id="u1", passenger_call_budget=2))
                await session.commit()
                row = (await session.execute(
                    select(UserSettings).where(UserSettings.user_id == "u1")
                )).scalar_one()
                assert row.passenger_calling_enabled is True
                assert row.passenger_call_budget == 2
                assert row.passenger_parameters_json == "[]"
        finally:
            await engine.dispose()
