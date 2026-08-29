"""Sandbox copies: forked, re-runnable requests (Phase 22b).

The properties that matter are isolation ones, so most of these assert about
what the *source* conversation looks like after the sandbox has been used.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.chat_data import ChatData
from app.models.memory import Memory
from app.services.debug import get_exchange, list_exchanges
from app.services.debug_sandbox import SANDBOX_CHAT_PREFIX
from tests.conftest import TestSessionLocal

REAL_CHAT = "real-conversation-1"


async def enable_debug(client) -> str:
    await client.put("/api/settings", json={"debug_mode": True})
    return (await client.get("/api/auth/me")).json()["id"]


async def make_endpoint(client):
    resp = await client.post("/api/endpoints", json={
        "name": "Primary", "base_url": "http://mock-upstream:9999",
        "api_key": "sk-test", "model": "test-model", "enabled": True,
    })
    assert resp.status_code in (200, 201), resp.text


def upstream(content="Hi there!"):
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def seed_run(user_id: str, chat_id: str = REAL_CHAT) -> str:
    """A stored debug run with recoverable original messages."""
    from app.services.debug import capture_exchange

    await capture_exchange(
        user_id=user_id, chat_id=chat_id, model="test-model",
        pipeline_data={
            "schema_version": 2, "tags": [], "stages": [],
            "original_messages": json.dumps(
                [{"role": "user", "content": "Describe the dragon"}]
            ),
            "run": {"source": "live", "replay_of": "", "sandbox_id": "",
                    "llm_calls": [], "cantrips": [], "totals": {}},
        },
        response_content="A dragon.", verification_data={},
    )
    return (await list_exchanges(user_id, limit=1))[0]["id"]


async def seed_conversation_state(user_id: str, chat_id: str = REAL_CHAT):
    async with TestSessionLocal() as db:
        db.add(Memory(user_id=user_id, conversation_id=chat_id,
                      key="gold", value="50", memory_type="flag"))
        db.add(ChatData(user_id=user_id, conversation_id=chat_id,
                        key="turn", value_json="3"))
        await db.commit()


@pytest.mark.asyncio
class TestForking:
    async def test_creating_a_sandbox_copies_the_request(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        run_id = await seed_run(uid)

        resp = await client.post(f"/api/debug/{run_id}/sandbox", json={"name": "Dragon test"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Dragon test"
        assert body["source_exchange_id"] == run_id
        assert body["message_count"] == 1
        assert body["message_list"][0]["content"] == "Describe the dragon"
        assert body["run_count"] == 0

    async def test_the_sandbox_gets_its_own_conversation(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        run_id = await seed_run(uid)

        body = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()
        assert body["sandbox_chat_id"].startswith(SANDBOX_CHAT_PREFIX)
        assert body["sandbox_chat_id"] != body["source_chat_id"]
        assert body["source_chat_id"] == REAL_CHAT

    async def test_conversation_state_is_copied_not_shared(self, admin_client):
        """A sandbox that writes a memory must not change what the real
        conversation sees, so the state is duplicated at fork time."""
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await seed_conversation_state(uid)
        run_id = await seed_run(uid)

        body = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()
        sandbox_chat = body["sandbox_chat_id"]

        async with TestSessionLocal() as db:
            mem = (await db.execute(
                select(Memory).where(Memory.conversation_id == sandbox_chat)
            )).scalars().all()
            chat = (await db.execute(
                select(ChatData).where(ChatData.conversation_id == sandbox_chat)
            )).scalars().all()

        assert [m.key for m in mem] == ["gold"]
        assert mem[0].value == "50"
        assert [c.key for c in chat] == ["turn"]

        # Mutating the copy leaves the original alone.
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(Memory).where(Memory.conversation_id == sandbox_chat)
            )).scalar_one()
            row.value = "999"
            await db.commit()

        async with TestSessionLocal() as db:
            original = (await db.execute(
                select(Memory).where(Memory.conversation_id == REAL_CHAT)
            )).scalar_one()
        assert original.value == "50"

    async def test_forking_an_unknown_run_is_a_404(self, admin_client):
        client, _, _ = admin_client
        await enable_debug(client)
        resp = await client.post("/api/debug/nope/sandbox", json={})
        assert resp.status_code == 404

    async def test_a_run_with_no_recoverable_messages_cannot_be_forked(self, admin_client):
        from app.services.debug import capture_exchange

        client, _, _ = admin_client
        uid = await enable_debug(client)
        await capture_exchange(
            user_id=uid, chat_id="c", model="m",
            pipeline_data={"stages": [], "original_messages": "", "tags": []},
            response_content="", verification_data={},
        )
        run_id = (await list_exchanges(uid, limit=1))[0]["id"]

        resp = await client.post(f"/api/debug/{run_id}/sandbox", json={})
        assert resp.status_code == 400
        assert "cannot be forked" in resp.json()["detail"]


@pytest.mark.asyncio
class TestRunning:
    async def test_a_sandbox_can_be_fired_repeatedly(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        run_id = await seed_run(uid)
        httpx_mock.add_response(json=upstream(), is_reusable=True)

        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]

        first = await client.post(f"/api/debug/sandboxes/{sid}/run")
        assert first.status_code == 200, first.text
        assert first.json()["run_count"] == 1

        second = await client.post(f"/api/debug/sandboxes/{sid}/run")
        assert second.status_code == 200
        assert second.json()["run_count"] == 2
        assert second.json()["run_id"] != first.json()["run_id"]

    async def test_a_sandbox_run_is_labelled_as_such(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        run_id = await seed_run(uid)
        httpx_mock.add_response(json=upstream(), is_reusable=True)

        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]
        result = (await client.post(f"/api/debug/sandboxes/{sid}/run")).json()

        produced = await get_exchange(uid, result["run_id"])
        assert produced["pipeline_data"]["run"]["source"] == "sandbox"
        assert produced["pipeline_data"]["run"]["sandbox_id"] == sid
        assert produced["chat_id"].startswith(SANDBOX_CHAT_PREFIX)

    async def test_running_a_sandbox_never_touches_the_source_conversation(
        self, admin_client, httpx_mock
    ):
        """The whole point: fire it as often as you like, the real thread is
        untouched."""
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        await seed_conversation_state(uid)
        run_id = await seed_run(uid)
        httpx_mock.add_response(json=upstream(), is_reusable=True)

        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]
        for _ in range(3):
            assert (await client.post(f"/api/debug/sandboxes/{sid}/run")).status_code == 200

        async with TestSessionLocal() as db:
            mem = (await db.execute(
                select(Memory).where(Memory.conversation_id == REAL_CHAT)
            )).scalars().all()
            chat = (await db.execute(
                select(ChatData).where(ChatData.conversation_id == REAL_CHAT)
            )).scalars().all()

        assert [(m.key, m.value) for m in mem] == [("gold", "50")]
        assert [(c.key, c.value_json) for c in chat] == [("turn", "3")]

    async def test_the_only_outbound_call_is_to_the_users_own_endpoint(
        self, admin_client, httpx_mock
    ):
        """A sandbox run must not contact the chat service the request came
        from -- that service is not expecting a response and would see an
        unsolicited call."""
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        run_id = await seed_run(uid)
        httpx_mock.add_response(json=upstream(), is_reusable=True)

        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]
        await client.post(f"/api/debug/sandboxes/{sid}/run")

        hosts = {r.url.host for r in httpx_mock.get_requests()}
        assert hosts == {"mock-upstream"}, f"unexpected outbound hosts: {hosts}"


@pytest.mark.asyncio
class TestEditingAndReset:
    async def test_the_request_can_be_edited(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        run_id = await seed_run(uid)
        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]

        resp = await client.patch(f"/api/debug/sandboxes/{sid}", json={
            "messages": [{"role": "user", "content": "Describe the griffin instead"}],
        })
        assert resp.status_code == 200
        assert resp.json()["message_list"][0]["content"] == "Describe the griffin instead"

    async def test_reset_discards_accumulated_state_and_re_forks(
        self, admin_client, httpx_mock
    ):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await make_endpoint(client)
        await seed_conversation_state(uid)
        run_id = await seed_run(uid)
        httpx_mock.add_response(json=upstream(), is_reusable=True)

        body = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()
        sid, sandbox_chat = body["id"], body["sandbox_chat_id"]
        await client.post(f"/api/debug/sandboxes/{sid}/run")

        # Something the sandbox accumulated.
        async with TestSessionLocal() as db:
            db.add(Memory(user_id=uid, conversation_id=sandbox_chat,
                          key="scratch", value="junk", memory_type="flag"))
            await db.commit()

        resp = await client.post(f"/api/debug/sandboxes/{sid}/reset")
        assert resp.status_code == 200
        assert resp.json()["run_count"] == 0

        async with TestSessionLocal() as db:
            keys = {m.key for m in (await db.execute(
                select(Memory).where(Memory.conversation_id == sandbox_chat)
            )).scalars().all()}
        assert keys == {"gold"}, "reset should restore the fork, not keep scratch state"

    async def test_deleting_a_sandbox_removes_its_state(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        await seed_conversation_state(uid)
        run_id = await seed_run(uid)

        body = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()
        sandbox_chat = body["sandbox_chat_id"]

        assert (await client.delete(f"/api/debug/sandboxes/{body['id']}")).status_code == 204

        async with TestSessionLocal() as db:
            leftover = (await db.execute(
                select(Memory).where(Memory.conversation_id == sandbox_chat)
            )).scalars().all()
            original = (await db.execute(
                select(Memory).where(Memory.conversation_id == REAL_CHAT)
            )).scalars().all()

        assert leftover == []
        assert len(original) == 1, "deleting a sandbox must not touch the source"

    async def test_purge_refuses_a_non_sandbox_conversation_id(self, admin_client):
        """The purge deletes by conversation id, so a blank or malformed id
        would otherwise reach real conversations."""
        from app.services.debug_sandbox import _purge_state

        client, _, _ = admin_client
        uid = await enable_debug(client)
        await seed_conversation_state(uid)

        async with TestSessionLocal() as db:
            await _purge_state(db, uid, REAL_CHAT)
            await _purge_state(db, uid, "")
            await db.commit()

        async with TestSessionLocal() as db:
            survived = (await db.execute(
                select(Memory).where(Memory.conversation_id == REAL_CHAT)
            )).scalars().all()
        assert len(survived) == 1


@pytest.mark.asyncio
class TestListingAndIsolation:
    async def test_listing(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        run_id = await seed_run(uid)
        await client.post(f"/api/debug/{run_id}/sandbox", json={"name": "A"})
        await client.post(f"/api/debug/{run_id}/sandbox", json={"name": "B"})

        names = {s["name"] for s in
                 (await client.get("/api/debug/sandboxes/list")).json()["sandboxes"]}
        assert names == {"A", "B"}

    async def test_another_user_cannot_reach_your_sandbox(self, admin_client):
        client, _, _ = admin_client
        uid = await enable_debug(client)
        run_id = await seed_run(uid)
        sid = (await client.post(f"/api/debug/{run_id}/sandbox", json={})).json()["id"]

        await client.post("/api/users", json={"username": "other", "password": "password2"})
        login = await client.post("/api/auth/login",
                                  json={"username": "other", "password": "password2"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        assert (await client.post(f"/api/debug/sandboxes/{sid}/run",
                                  headers=headers)).status_code == 404
        assert (await client.delete(f"/api/debug/sandboxes/{sid}",
                                    headers=headers)).status_code == 404
        assert (await client.get("/api/debug/sandboxes/list",
                                 headers=headers)).json()["sandboxes"] == []
