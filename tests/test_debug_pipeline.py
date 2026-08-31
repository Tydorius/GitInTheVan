"""End-to-end debug capture through the real proxy pipeline (Phase 22 Stage A).

These drive `/v1/chat/completions` with a mocked upstream and then read the
stored exchange back, so they prove the wiring rather than the recorders in
isolation — which is exactly the gap that let Maps run uninstrumented for two
months.
"""

from __future__ import annotations

import json

import pytest

from app.services.debug import get_exchange, list_exchanges


async def enable_debug(client) -> str:
    """Turn on debug mode and return the user id."""
    await client.put("/api/settings", json={"debug_mode": True})
    return (await client.get("/api/auth/me")).json()["id"]


async def make_endpoint(client, url: str = "http://mock-upstream:9999", **overrides) -> dict:
    payload = {
        "name": "Primary",
        "base_url": url,
        "api_key": "sk-test",
        "model": "test-model",
        "enabled": True,
    }
    payload.update(overrides)
    resp = await client.post("/api/endpoints", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def upstream_response(content: str = "Hi there!", *, usage: dict | None = None,
                      reasoning: str = "", model: str = "test-model-0613") -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    body: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


async def latest_run(user_id: str) -> dict:
    runs = await list_exchanges(user_id, limit=1)
    assert runs, "no debug exchange was captured"
    return await get_exchange(user_id, runs[0]["id"])


@pytest.mark.asyncio
class TestNonStreamingCapture:
    async def test_upstream_usage_and_latency_reach_the_trace(self, admin_client, httpx_mock):
        """The app never read upstream `usage` anywhere before Phase 22."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)

        httpx_mock.add_response(json=upstream_response(usage={
            "prompt_tokens": 812,
            "completion_tokens": 44,
            "completion_tokens_details": {"reasoning_tokens": 12},
        }))

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200

        run = await latest_run(uid)
        totals = run["pipeline_data"]["run"]["totals"]
        assert totals["prompt_tokens"] == 812
        assert totals["completion_tokens"] == 44
        assert totals["reasoning_tokens"] == 12
        assert totals["tokens_source"] == "upstream"
        assert totals["llm_call_count"] == 1
        assert totals["llm_latency_ms"] >= 0
        assert totals["total_latency_ms"] >= totals["llm_latency_ms"] or totals["overhead_ms"] == 0

        call = run["pipeline_data"]["run"]["llm_calls"][0]
        assert call["endpoint_name"] == "Primary"
        assert call["status_code"] == 200
        # The model the upstream actually served, not the one requested.
        assert call["model_resolved"] == "test-model-0613"
        assert call["model_requested"] == "test-model"

    async def test_missing_usage_falls_back_to_estimation_and_says_so(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        httpx_mock.add_response(json=upstream_response(content="x" * 400))

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        assert run["pipeline_data"]["run"]["totals"]["tokens_source"] == "estimated"

    async def test_reasoning_is_stored_whole(self, admin_client, httpx_mock):
        """It was clipped at 500 characters, which made comparing two runs'
        reasoning impossible."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)

        reasoning = "".join(f"step {i} " for i in range(400))
        assert len(reasoning) > 2000
        httpx_mock.add_response(json=upstream_response(reasoning=reasoning))

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        stored = [
            s["metadata"]["thinking"]
            for s in run["pipeline_data"]["stages"]
            if s.get("metadata", {}).get("thinking")
        ]
        assert stored, "reasoning was not captured"
        assert stored[0] == reasoning

    async def test_response_content_is_not_truncated(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)

        long_content = "y" * 15000  # Over the old 10,000-character store limit.
        httpx_mock.add_response(json=upstream_response(content=long_content))

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        assert len(run["response_content"]) == 15000

    async def test_the_trace_carries_the_schema_version_and_run_block(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        httpx_mock.add_response(json=upstream_response())

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        pipeline = (await latest_run(uid))["pipeline_data"]
        assert pipeline["schema_version"] == 2
        assert pipeline["run"]["source"] == "live"
        assert isinstance(pipeline["run"]["cantrips"], list)


@pytest.mark.asyncio
class TestStreamingCapture:
    async def test_a_streaming_request_now_produces_an_exchange(
        self, admin_client, httpx_mock
    ):
        """Streaming produced no debug exchange at all before Phase 22, so a
        user whose client streams saw an empty Debug tab while the pipeline was
        working normally.

        An authenticated request has a conversation, so it takes the buffered
        streaming path: the response is fetched whole, processed, then re-emitted
        as SSE. That path had the complete exchange in hand and never saved it.
        """
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)

        httpx_mock.add_response(json=upstream_response(
            content="Hello", usage={"prompt_tokens": 30, "completion_tokens": 5},
        ))

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": True},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        # Drain, so the generator's finally block runs and saves the exchange.
        await resp.aread()

        runs = await list_exchanges(uid, limit=1)
        assert runs, "streaming request produced no debug exchange"
        run = await get_exchange(uid, runs[0]["id"])
        assert run["response_content"] == "Hello"
        totals = run["pipeline_data"]["run"]["totals"]
        assert totals["prompt_tokens"] == 30
        assert totals["completion_tokens"] == 5
        assert totals["tokens_source"] == "upstream"

    async def test_a_streamed_call_records_which_endpoint_served_it(
        self, admin_client, httpx_mock
    ):
        """`_record_stream_call` has read `_gitv_endpoint_id` since Phase 22 and
        nothing ever wrote it, so every streamed run attributed its call to an
        empty endpoint id -- which the comparison view groups by."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        endpoint = await make_endpoint(client)

        httpx_mock.add_response(json=upstream_response(content="Hi"))

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": True},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        await resp.aread()

        run = await latest_run(uid)
        call = run["pipeline_data"]["run"]["llm_calls"][0]
        assert call["endpoint_id"] == endpoint["id"]

    def test_the_sse_accumulator_reassembles_a_passthrough_stream(self):
        """The raw passthrough path — no conversation, so nothing is buffered —
        never holds the whole response; it accumulates deltas as they fly past.

        Chunk boundaries fall anywhere, including mid-line, which is what this
        covers.
        """
        from app.services.proxy import _StreamAccumulator

        acc = _StreamAccumulator()
        raw = (
            b'data: {"id":"1","model":"stream-model","choices":[{"delta":{"content":"Hel"}}]}\n\n'
            b'data: {"id":"1","choices":[{"delta":{"content":"lo"},"finish_reason":null}]}\n\n'
            b'data: {"id":"1","choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
            b'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":30,"completion_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        for i in range(0, len(raw), 17):  # deliberately split mid-line
            acc.feed(raw[i:i + 17])

        result = acc.as_response("fallback-model")
        message = result["choices"][0]["message"]
        assert message["content"] == "Hello"
        assert message["reasoning_content"] == "thinking"
        assert result["model"] == "stream-model"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"]["prompt_tokens"] == 30

    def test_the_accumulator_survives_a_truncated_stream(self):
        """A stream that died partway still produced a trace worth keeping."""
        from app.services.proxy import _StreamAccumulator

        acc = _StreamAccumulator()
        acc.feed(b'data: {"choices":[{"delta":{"content":"partial')
        result = acc.as_response("fallback-model")
        assert result["choices"][0]["message"]["content"] == ""
        assert result["model"] == "fallback-model"


@pytest.mark.asyncio
class TestCantripCapture:
    async def _make_cantrip(self, client, name: str, code: str, tag: str = "") -> dict:
        resp = await client.post("/api/cantrips", json={
            "name": name,
            "description": "",
            "code": code,
            "hook_type": "pre",
            "is_active": True,
            "run_pre_driver": True,
            "execution_order": 10,
            "timeout_ms": 5000,
            "tag": tag,
        })
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    async def test_a_cantrip_that_ran_is_recorded_with_its_code_and_output(
        self, admin_client, httpx_mock
    ):
        """cantrip.py had zero debug_capture calls; nothing about an execution
        was recorded anywhere."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        cantrip = await self._make_cantrip(
            client, "Personality Setter",
            "export function run(context) { return { personality: 'bold' } }",
        )
        httpx_mock.add_response(json=upstream_response())

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        recorded = run["pipeline_data"]["run"]["cantrips"]
        entry = next((c for c in recorded if c["id"] == cantrip["id"]), None)
        assert entry is not None, f"cantrip not recorded; got {recorded}"
        assert entry["name"] == "Personality Setter"
        assert entry["code_hash"]
        assert "duration_ms" in entry

    async def test_an_active_cantrip_runs_regardless_of_tags(
        self, admin_client, httpx_mock
    ):
        """Active means blanket: rule 4 of the Activation Hierarchy.

        A tag never *restricts*, because activation is a union and no source
        subtracts. So an Active cantrip runs whether or not its tag appears. To
        make a cantrip tag-only, turn Active off -- the tag can then switch it
        on, which is rule 3 and is covered in `test_activation_hierarchy.py`.
        """
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        cantrip = await self._make_cantrip(
            client, "Tagged Only",
            "export function run(context) { return {} }",
            tag="only-on-request",
        )
        httpx_mock.add_response(json=upstream_response())

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model",
                  "messages": [{"role": "user", "content": "Hello, no tags here"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        entry = next(
            (c for c in run["pipeline_data"]["run"]["cantrips"] if c["id"] == cantrip["id"]),
            None,
        )
        assert entry is not None, "cantrip was not recorded at all"
        assert entry["triggered"] is True

    async def test_a_failing_cantrip_records_its_error(self, admin_client, httpx_mock):
        """The error was logged and discarded; nothing reached the trace."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        cantrip = await self._make_cantrip(
            client, "Broken", "this is not valid javascript {{{",
        )
        httpx_mock.add_response(json=upstream_response())

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        entry = next(
            (c for c in run["pipeline_data"]["run"]["cantrips"] if c["id"] == cantrip["id"]),
            None,
        )
        assert entry is not None
        assert entry["error"], "the cantrip failed but no error was recorded"

    async def test_a_cantrip_pass_appears_as_a_timeline_stage(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        await self._make_cantrip(
            client, "Noop", "export function run(context) { return {} }"
        )
        httpx_mock.add_response(json=upstream_response())

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        run = await latest_run(uid)
        names = [s["name"] for s in run["pipeline_data"]["stages"]]
        assert "cantrips_pre_driver" in names, names


@pytest.mark.asyncio
class TestRetentionUnderRealTraffic:
    async def test_a_saved_run_survives_a_burst_of_new_traffic(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        httpx_mock.add_response(json=upstream_response(), is_reusable=True)

        async def send():
            await client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                      "stream": False},
                headers={"Authorization": f"Bearer {api_key}"},
            )

        await send()
        first = (await list_exchanges(uid, limit=1))[0]["id"]
        assert (await client.post(f"/api/debug/{first}/save", json={"label": "Baseline"})).status_code == 200

        for _ in range(25):
            await send()

        assert await get_exchange(uid, first) is not None


@pytest.mark.asyncio
class TestReplay:
    async def test_replay_reruns_the_original_messages_and_links_back(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        httpx_mock.add_response(json=upstream_response(content="first"), is_reusable=True)

        original_text = "Tell me about dragons"
        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": original_text}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        source_id = (await list_exchanges(uid, limit=1))[0]["id"]

        resp = await client.post(f"/api/debug/{source_id}/replay")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["run_id"] != source_id
        # The lack of cantrip sandboxing is stated, not implied.
        assert "not sandboxed" in body["warning"].lower()

        replayed = await get_exchange(uid, body["run_id"])
        run = replayed["pipeline_data"]["run"]
        assert run["source"] == "replay"
        assert run["replay_of"] == source_id

        sent = json.loads(replayed["pipeline_data"]["original_messages"])
        assert sent[-1]["content"] == original_text

    async def test_replay_does_not_touch_the_real_conversation(
        self, admin_client, httpx_mock
    ):
        """A replay resends messages the real conversation already contains, so
        an unisolated replay would summarise and re-hash it."""
        client, _, api_key = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        httpx_mock.add_response(json=upstream_response(), is_reusable=True)

        await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}],
                  "stream": False},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        source = await get_exchange(uid, (await list_exchanges(uid, limit=1))[0]["id"])
        real_chat_id = source["chat_id"]

        resp = await client.post(f"/api/debug/{source['id']}/replay")
        replayed = await get_exchange(uid, resp.json()["run_id"])

        assert replayed["chat_id"] != real_chat_id
        assert replayed["chat_id"].startswith("replay:")

    async def test_replaying_a_missing_run_is_a_404(self, admin_client):
        client, _, _ = admin_client
        assert (await client.post("/api/debug/nope/replay")).status_code == 404
