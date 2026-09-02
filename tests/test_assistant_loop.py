"""The turn loop end to end (Phase 26b).

Only the outer boundary is mocked: the upstream LLM, at a distinct URL, with
OpenAI-shaped responses. The registry, schema, permission evaluation, executor
and store are the real ones, so a regression in any of them fails here rather
than sailing through.

The confirmation cases deliberately register a second upstream response and
assert it was *not* consumed: a loop that ran on past an `awaiting_confirmation`
would quietly execute the thing the user was being asked about.
"""

from __future__ import annotations

import json

import pytest

from app.services.assistant.loop import CONTINUE_MARKER

MOCK_URL = "https://assistant-upstream.test"
CHAT_URL = f"{MOCK_URL}/v1/chat/completions"
VALID_CANTRIP = "export default function(context) { return context }"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _calls(*calls: tuple[str, str, dict]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for call_id, name, args in calls
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _frames(body: str) -> list[tuple[str, dict]]:
    """Parse an `event:`/`data:` SSE body into (name, payload) pairs."""
    out: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if name:
            out.append((name, json.loads(data) if data else {}))
    return out


def _names(frames) -> list[str]:
    return [name for name, _ in frames]


def _first(frames, wanted):
    for name, data in frames:
        if name == wanted:
            return data
    raise AssertionError(f"no {wanted} frame in {_names(frames)}")


async def _setup(client, *, permissions=None, yolo=False):
    """An endpoint, an assistant config and a conversation."""
    endpoint = await client.post(
        "/api/endpoints",
        json={
            "name": "Assistant EP",
            "base_url": MOCK_URL,
            "api_key": "sk-assistant-upstream",
            "default_model": "test-model",
            "role_tag": "tool_use",
        },
    )
    assert endpoint.status_code == 201

    config = await client.put(
        "/api/assistant/config",
        json={"endpoint_id": endpoint.json()["id"], "model": "test-model"},
    )
    assert config.status_code == 200

    if permissions is not None:
        resp = await client.put("/api/assistant/permissions", json=permissions)
        assert resp.status_code == 200, resp.text

    conversation = await client.post("/api/assistant/conversations")
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]

    if yolo:
        patched = await client.patch(
            f"/api/assistant/conversations/{conversation_id}", json={"yolo": True}
        )
        assert patched.status_code == 200

    return endpoint.json()["id"], conversation_id


async def _send(client, conversation_id: str, content: str):
    resp = await client.post(
        f"/api/assistant/conversations/{conversation_id}/message",
        json={"content": content, "route": {"page": "/cantrips"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _frames(resp.text)


async def _resume(client, conversation_id: str, payload: dict):
    resp = await client.post(
        f"/api/assistant/conversations/{conversation_id}/resume", json=payload
    )
    assert resp.status_code == 200, resp.text
    return _frames(resp.text)


async def _audit_actions(client) -> list[str]:
    resp = await client.get("/api/audit?limit=200")
    assert resp.status_code == 200
    return [row["action"] for row in resp.json()["logs"]]


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


class TestReadThenAnswer:
    @pytest.mark.asyncio
    async def test_tool_call_then_final_text(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        await client.post("/api/cantrips", json={"name": "Alpha", "code": VALID_CANTRIP})

        httpx_mock.add_response(url=CHAT_URL, json=_calls(("c1", "list_cantrips", {})))
        httpx_mock.add_response(url=CHAT_URL, json=_text("You have one cantrip, Alpha."))

        frames = await _send(client, conversation_id, "What cantrips do I have?")
        names = _names(frames)

        assert names[0] == "meta"
        assert names.index("assistant_message") < names.index("tool_call")
        assert names.index("tool_call") < names.index("tool_result")
        assert names[-1] == "done"
        assert _first(frames, "done")["reason"] == "stop"

        result = _first(frames, "tool_result")
        assert result["ok"] is True
        assert result["result"]["cantrips"][0]["name"] == "Alpha"

    @pytest.mark.asyncio
    async def test_the_tools_array_reaches_the_provider(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(url=CHAT_URL, json=_text("Hello."))

        await _send(client, conversation_id, "Hi")
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert sent["tool_choice"] == "auto"
        assert sent["stream"] is False
        names = {t["function"]["name"] for t in sent["tools"]}
        assert "list_cantrips" in names
        # The packs group is admin-gated off, so none of it is offered.
        assert "link_repo" not in names

    @pytest.mark.asyncio
    async def test_transcript_and_usage_are_persisted(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(url=CHAT_URL, json=_calls(("c1", "list_cantrips", {})))
        httpx_mock.add_response(url=CHAT_URL, json=_text("Done."))

        await _send(client, conversation_id, "List them")
        stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()

        roles = [m["role"] for m in stored["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert stored["title"] == "List them"
        assert stored["llm_calls"] == 2
        assert stored["tool_calls"] == 1
        assert stored["prompt_tokens"] == 150
        assert stored["completion_tokens"] == 30
        assert stored["pending"] is None

    @pytest.mark.asyncio
    async def test_usage_accumulates_across_turns(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(url=CHAT_URL, json=_text("One."))
        httpx_mock.add_response(url=CHAT_URL, json=_text("Two."))

        await _send(client, conversation_id, "first")
        await _send(client, conversation_id, "second")

        stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
        assert stored["llm_calls"] == 2
        assert stored["prompt_tokens"] == 200
        assert stored["completion_tokens"] == 40


class TestConfirmation:
    @pytest.mark.asyncio
    async def test_destructive_call_pauses_for_confirmation(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        made = await client.post("/api/cantrips", json={"name": "Doomed", "code": VALID_CANTRIP})
        cantrip_id = made.json()["id"]

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "delete_cantrip", {"cantrip_id": cantrip_id}))
        )
        # Registered but must NOT be consumed: the loop has to stop dead.
        httpx_mock.add_response(url=CHAT_URL, json=_text("Deleted."))

        frames = await _send(client, conversation_id, "Delete Doomed")
        assert _names(frames)[-1] == "awaiting_confirmation"
        assert len(httpx_mock.get_requests()) == 1

        card = _first(frames, "awaiting_confirmation")
        assert card["name"] == "delete_cantrip"
        assert card["risk"] == "destructive"
        assert card["group"] == "Cantrips"

        stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
        assert stored["pending"]["call_id"] == "c1"
        assert stored["pending"]["name"] == "delete_cantrip"
        assert stored["pending"]["tool_calls_used"] == 1

        # And the cantrip is still there.
        assert (await client.get(f"/api/cantrips/{cantrip_id}")).status_code == 200

        httpx_mock.reset()

    @pytest.mark.asyncio
    async def test_approve_executes_and_the_loop_continues(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        made = await client.post("/api/cantrips", json={"name": "Doomed", "code": VALID_CANTRIP})
        cantrip_id = made.json()["id"]

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "delete_cantrip", {"cantrip_id": cantrip_id}))
        )
        await _send(client, conversation_id, "Delete Doomed")

        httpx_mock.add_response(url=CHAT_URL, json=_text("Deleted it."))
        frames = await _resume(client, conversation_id, {"call_id": "c1", "decision": "approve"})

        assert "tool_result" in _names(frames)
        assert _first(frames, "tool_result")["ok"] is True
        assert _first(frames, "done")["reason"] == "stop"
        assert (await client.get(f"/api/cantrips/{cantrip_id}")).status_code == 404
        assert "assistant_tool_approved" in await _audit_actions(client)

    @pytest.mark.asyncio
    async def test_reject_leaves_the_object_alone(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        made = await client.post("/api/cantrips", json={"name": "Doomed", "code": VALID_CANTRIP})
        cantrip_id = made.json()["id"]

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "delete_cantrip", {"cantrip_id": cantrip_id}))
        )
        await _send(client, conversation_id, "Delete Doomed")

        httpx_mock.add_response(url=CHAT_URL, json=_text("Understood."))
        frames = await _resume(
            client,
            conversation_id,
            {"call_id": "c1", "decision": "reject", "note": "I still want it"},
        )
        assert _first(frames, "done")["reason"] == "stop"
        assert (await client.get(f"/api/cantrips/{cantrip_id}")).status_code == 200

        stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
        tool_messages = [m for m in stored["messages"] if m["role"] == "tool"]
        assert json.loads(tool_messages[-1]["content"])["rejected"] is True
        assert "I still want it" in tool_messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_allow_for_session_collapses_the_next_ask(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "create_cantrip", {"name": "One", "code": VALID_CANTRIP}))
        )
        await _send(client, conversation_id, "Make one")

        httpx_mock.add_response(url=CHAT_URL, json=_text("Made it."))
        await _resume(client, conversation_id, {"call_id": "c1", "decision": "allow_session"})

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c2", "create_cantrip", {"name": "Two", "code": VALID_CANTRIP}))
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("And another."))
        frames = await _send(client, conversation_id, "Make another")

        assert "awaiting_confirmation" not in _names(frames)
        assert _first(frames, "tool_result")["ok"] is True

    @pytest.mark.asyncio
    async def test_a_mismatched_call_id_is_refused(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        made = await client.post("/api/cantrips", json={"name": "Doomed", "code": VALID_CANTRIP})

        httpx_mock.add_response(
            url=CHAT_URL,
            json=_calls(("c1", "delete_cantrip", {"cantrip_id": made.json()["id"]})),
        )
        await _send(client, conversation_id, "Delete Doomed")

        frames = await _resume(client, conversation_id, {"call_id": "wrong", "decision": "approve"})
        assert _first(frames, "error")["code"] == "no_pending"
        assert (await client.get(f"/api/cantrips/{made.json()['id']}")).status_code == 200

    @pytest.mark.asyncio
    async def test_yolo_collapses_content_asks_but_not_configuration(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        endpoint_id, conversation_id = await _setup(client, yolo=True)

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "create_cantrip", {"name": "Yolo", "code": VALID_CANTRIP}))
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Created."))
        frames = await _send(client, conversation_id, "Make a cantrip")
        assert "awaiting_confirmation" not in _names(frames)

        httpx_mock.add_response(
            url=CHAT_URL,
            json=_calls(("c2", "update_endpoint", {"endpoint_id": endpoint_id, "name": "Renamed"})),
        )
        frames = await _send(client, conversation_id, "Rename the endpoint")
        assert _names(frames)[-1] == "awaiting_confirmation"


class TestDeny:
    @pytest.mark.asyncio
    async def test_a_denied_tool_is_never_executed(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(
            client, permissions={"groups": {"cantrips": "deny"}, "tools": {}}
        )
        made = await client.post("/api/cantrips", json={"name": "Safe", "code": VALID_CANTRIP})
        cantrip_id = made.json()["id"]

        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "delete_cantrip", {"cantrip_id": cantrip_id}))
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("I cannot do that."))

        frames = await _send(client, conversation_id, "Delete Safe")
        result = _first(frames, "tool_result")
        assert result["ok"] is False
        assert result["result"]["denied"] is True
        assert "Cantrips" in result["result"]["detail"]

        assert (await client.get(f"/api/cantrips/{cantrip_id}")).status_code == 200
        assert "assistant_tool_denied" in await _audit_actions(client)

    @pytest.mark.asyncio
    async def test_denied_tools_are_not_offered_but_are_named_in_the_prompt(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        _, conversation_id = await _setup(
            client, permissions={"groups": {"cantrips": "deny"}, "tools": {}}
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Hello."))
        await _send(client, conversation_id, "Hi")

        sent = json.loads(httpx_mock.get_requests()[0].content)
        offered = {t["function"]["name"] for t in sent["tools"]}
        assert "delete_cantrip" not in offered

        system = sent["messages"][0]["content"]
        assert "delete_cantrip: This tool is forbidden by the user's security settings." in system
        assert "they must change it on Cantrips." in system


class TestToolCap:
    @pytest.mark.asyncio
    async def test_the_cap_ends_the_turn(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        from app.services.admin import get_admin_settings, update_admin_settings

        original = (await get_admin_settings()).max_assistant_tool_calls_per_turn
        try:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": 2})
            for _ in range(3):
                httpx_mock.add_response(url=CHAT_URL, json=_calls(("c", "list_cantrips", {})))

            frames = await _send(client, conversation_id, "Keep listing")
            assert _first(frames, "done")["reason"] == "tool_cap"

            stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
            assert stored["tool_calls"] == 2
        finally:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": original})
            httpx_mock.reset()

    @pytest.mark.asyncio
    async def test_the_counter_survives_a_resume(self, admin_client, httpx_mock):
        """A reload between an ask and its answer must not reset the counter."""
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        from app.services.admin import get_admin_settings, update_admin_settings

        original = (await get_admin_settings()).max_assistant_tool_calls_per_turn
        try:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": 2})

            httpx_mock.add_response(
                url=CHAT_URL,
                json=_calls(
                    ("c1", "list_cantrips", {}),
                    ("c2", "create_cantrip", {"name": "Asked", "code": VALID_CANTRIP}),
                ),
            )
            frames = await _send(client, conversation_id, "Do two things")
            assert _names(frames)[-1] == "awaiting_confirmation"

            stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
            assert stored["pending"]["tool_calls_used"] == 2

            # Resuming continues the same turn, so the very next model reply
            # asking for another tool must hit the cap rather than start fresh.
            httpx_mock.add_response(url=CHAT_URL, json=_calls(("c3", "list_cantrips", {})))
            frames = await _resume(client, conversation_id, {"call_id": "c2", "decision": "approve"})
            assert _first(frames, "done")["reason"] == "tool_cap"
        finally:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": original})
            httpx_mock.reset()

    @pytest.mark.asyncio
    async def test_continue_starts_a_fresh_counter(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        from app.services.admin import get_admin_settings, update_admin_settings

        original = (await get_admin_settings()).max_assistant_tool_calls_per_turn
        try:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": 1})
            httpx_mock.add_response(url=CHAT_URL, json=_calls(("c1", "list_cantrips", {})))
            frames = await _send(client, conversation_id, "Go")
            assert _first(frames, "done")["reason"] == "tool_cap"

            httpx_mock.add_response(url=CHAT_URL, json=_text("All done."))
            frames = await _send(client, conversation_id, CONTINUE_MARKER)
            assert _first(frames, "meta")["tool_calls_used"] == 0
            assert _first(frames, "done")["reason"] == "stop"

            stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
            user_texts = [m["content"] for m in stored["messages"] if m["role"] == "user"]
            # The marker itself is never stored; it becomes a plain nudge.
            assert CONTINUE_MARKER not in user_texts
            assert "Continue." in user_texts
            assert stored["title"] == "Go"
        finally:
            await update_admin_settings({"max_assistant_tool_calls_per_turn": original})
            httpx_mock.reset()


class TestClientTools:
    @pytest.mark.asyncio
    async def test_navigate_emits_a_client_action_and_waits(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        httpx_mock.add_response(
            url=CHAT_URL,
            json=_calls(("n1", "navigate", {"page": "/endpoints", "params": {"id": "abc"}})),
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Have a look."))

        frames = await _send(client, conversation_id, "Show me my endpoints")
        assert _names(frames)[-2:] == ["client_action", "awaiting_client"]
        assert len(httpx_mock.get_requests()) == 1

        action = _first(frames, "client_action")
        assert action["args"] == {"page": "/endpoints", "params": {"id": "abc"}}

        frames = await _resume(
            client, conversation_id, {"call_id": "n1", "client_result": {"navigated": True}}
        )
        assert _first(frames, "done")["reason"] == "stop"

        stored = (await client.get(f"/api/assistant/conversations/{conversation_id}")).json()
        tool_messages = [m for m in stored["messages"] if m["role"] == "tool"]
        assert json.loads(tool_messages[-1]["content"]) == {"navigated": True}

    @pytest.mark.asyncio
    async def test_an_unknown_page_is_a_tool_error_not_a_client_action(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("n1", "navigate", {"page": "/etc/passwd"}))
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Sorry."))

        frames = await _send(client, conversation_id, "Go somewhere")
        assert "client_action" not in _names(frames)
        assert "Unknown page" in json.dumps(_first(frames, "tool_result")["result"])

    @pytest.mark.asyncio
    async def test_an_unsupported_param_key_is_refused(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(
            url=CHAT_URL,
            json=_calls(("n1", "navigate", {"page": "/settings", "params": {"token": "x"}})),
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Sorry."))

        frames = await _send(client, conversation_id, "Go somewhere")
        assert "client_action" not in _names(frames)
        assert "Unsupported params" in json.dumps(_first(frames, "tool_result")["result"])


class TestProviderEndpoint:
    @pytest.mark.asyncio
    async def test_tools_reach_litellm_and_tool_calls_come_back(self, admin_client, monkeypatch):
        """A provider endpoint must carry `tools` through LiteLLM (26a's fix)."""
        client, _, _ = admin_client

        endpoint = await client.post(
            "/api/endpoints",
            json={
                "name": "Provider EP",
                "base_url": MOCK_URL,
                "api_key": "sk-provider",
                "default_model": "test-model",
                "provider": "openai",
            },
        )
        assert endpoint.status_code == 201
        await client.put(
            "/api/assistant/config",
            json={"endpoint_id": endpoint.json()["id"], "model": "test-model"},
        )
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]

        seen: list[dict] = []
        replies = [
            _calls(("c1", "list_cantrips", {})),
            _text("There are none."),
        ]

        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def model_dump(self):
                return self._payload

        async def fake_acompletion(**kwargs):
            seen.append(kwargs)
            return _Response(replies[len(seen) - 1])

        import litellm

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        frames = await _send(client, conversation_id, "Any cantrips?")
        assert _first(frames, "done")["reason"] == "stop"
        assert len(seen) == 2
        assert "tools" in seen[0]
        assert any(t["function"]["name"] == "list_cantrips" for t in seen[0]["tools"])
        assert _first(frames, "tool_result")["ok"] is True


class TestGuards:
    @pytest.mark.asyncio
    async def test_an_unknown_tool_name_is_reported_not_crashed(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(url=CHAT_URL, json=_calls(("c1", "obliterate_everything", {})))
        httpx_mock.add_response(url=CHAT_URL, json=_text("My mistake."))

        frames = await _send(client, conversation_id, "Do it")
        assert "No tool named" in json.dumps(_first(frames, "tool_result")["result"])
        assert _first(frames, "done")["reason"] == "stop"

    @pytest.mark.asyncio
    async def test_an_upstream_error_ends_the_turn(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(url=CHAT_URL, status_code=502, json={"error": {"message": "nope"}})

        frames = await _send(client, conversation_id, "Hi")
        error = _first(frames, "error")
        assert error["code"] == "upstream"
        assert error["status"] == 502

    @pytest.mark.asyncio
    async def test_no_endpoint_configured_reports_no_endpoint(self, admin_client):
        client, _, _ = admin_client
        conversation_id = (await client.post("/api/assistant/conversations")).json()["id"]
        frames = await _send(client, conversation_id, "Hi")
        assert _first(frames, "error")["code"] == "no_endpoint"

    @pytest.mark.asyncio
    async def test_local_docs_tool_runs_in_process(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)
        httpx_mock.add_response(
            url=CHAT_URL, json=_calls(("c1", "search_docs", {"query": "cantrip context api"}))
        )
        httpx_mock.add_response(url=CHAT_URL, json=_text("Here is what the guide says."))

        frames = await _send(client, conversation_id, "How do cantrips work?")
        result = _first(frames, "tool_result")
        assert result["ok"] is True
        assert result["result"]["results"]

    @pytest.mark.asyncio
    async def test_another_users_conversation_is_not_reachable(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        _, conversation_id = await _setup(client)

        created = await client.post(
            "/api/users", json={"username": "other", "password": "otherpass123", "is_admin": False}
        )
        assert created.status_code in (200, 201)
        login = await client.post(
            "/api/auth/login", json={"username": "other", "password": "otherpass123"}
        )
        resp = await client.post(
            f"/api/assistant/conversations/{conversation_id}/message",
            json={"content": "steal"},
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
        assert resp.status_code == 404
