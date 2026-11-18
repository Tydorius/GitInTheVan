"""0.35.0 Lorebook & Chat Context bundle.

Three features, one file:
- inject_after_cantrips -- deferred lorebook injection, so cantrips cannot
  read a book's entries through context.chat.last_messages
- scan_depth -- per-book "scan only the newest N messages" matching
- context.chat.turn_index -- the monotonic per-conversation request counter
  (message_count stalls once the client trims history)
"""

from __future__ import annotations

import json

import pytest

from app.services.deno_runner import DENO_PATH
from app.services.lorebook import match_book_entries

MOCK_ENDPOINT_URL = "http://lorebook-e2e.test"

requires_deno = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")


# ============================================================================
# Scan depth (unit)
# ============================================================================

def _entry(name="entry", keys=None, content="lore content", is_constant=False):
    return {
        "name": name,
        "keys": json.dumps(keys or []),
        "secondary_keys": "[]",
        "content": content,
        "position": "before_last_message",
        "insertion_order": 10,
        "is_constant": is_constant,
        "is_selective": False,
        "is_disabled": False,
    }


def _messages(*texts):
    return [{"role": "user", "content": t} for t in texts]


class TestScanDepth:
    def test_window_excludes_old_mentions(self):
        entries = [_entry(keys=["dragon"], content="Dragons.")]
        messages = _messages(*(["small talk"] * 8), "long ago there was a dragon",
                             "newest message", "another one")
        # The dragon mention is the 3rd-from-last message: inside a depth-3
        # window, outside a depth-2 one.
        assert len(match_book_entries(messages, entries, scan_depth=3)) == 1
        assert match_book_entries(messages, entries, scan_depth=2) == []
        # The control: depth 0 is today's behavior and scans everything.
        assert len(match_book_entries(messages, entries, scan_depth=0)) == 1
        # And depth defaults to off.
        assert len(match_book_entries(messages, entries)) == 1

    def test_window_includes_newest(self):
        entries = [_entry(keys=["dragon"], content="Dragons.")]
        messages = _messages("old chat", "a dragon appears")
        assert len(match_book_entries(messages, entries, scan_depth=1)) == 1

    def test_boundary_is_inclusive_at_the_window_edge(self):
        entries = [_entry(keys=["dragon"], content="Dragons.")]
        messages = _messages("a dragon at the boundary", "one newer", "two newer")
        # The dragon is messages[-3]: the oldest message inside a depth-3
        # window, and outside a depth-2 one.
        assert len(match_book_entries(messages, entries, scan_depth=3)) == 1
        assert match_book_entries(messages, entries, scan_depth=2) == []

    def test_constant_entries_ignore_depth(self):
        entries = [_entry(keys=[], content="Always present.", is_constant=True)]
        messages = _messages("old", "new")
        matched = match_book_entries(messages, entries, scan_depth=1)
        assert len(matched) == 1 and matched[0].content == "Always present."

    def test_depth_does_not_truncate_mid_message(self):
        """The window cuts the message list, not the flattened text: a keyword
        in an old message must not match just because the text blob merged."""
        entries = [_entry(keys=["dragon"], content="Dragons.")]
        messages = _messages("dragon", "hello")
        assert match_book_entries(messages, entries, scan_depth=1) == []


# ============================================================================
# API round-trip of the two book fields
# ============================================================================

class TestLorebookFieldRoundTrip:
    async def test_create_and_update_carries_both_fields(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/lorebooks", json={
            "name": "Deferred Book",
            "inject_after_cantrips": True,
            "scan_depth": 10,
        })
        assert resp.status_code in (200, 201), resp.text
        lb = resp.json()
        assert lb["inject_after_cantrips"] is True
        assert lb["scan_depth"] == 10

        resp = await client.put(f"/api/lorebooks/{lb['id']}", json={
            "inject_after_cantrips": False,
            "scan_depth": 4,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["inject_after_cantrips"] is False
        assert resp.json()["scan_depth"] == 4

    async def test_defaults_match_previous_behavior(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/lorebooks", json={"name": "Plain Book"})
        assert resp.status_code in (200, 201), resp.text
        lb = resp.json()
        assert lb["inject_after_cantrips"] is False
        assert lb["scan_depth"] == 0

    async def test_negative_scan_depth_clamps_to_off(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/lorebooks", json={"name": "Clamp", "scan_depth": -3})
        lb = resp.json()
        assert lb["scan_depth"] == 0


# ============================================================================
# Deferred injection through the real pipeline (needs Deno for the cantrip)
# ============================================================================

async def _make_e2e_endpoint(client):
    resp = await client.post("/api/endpoints", json={
        "name": "Lorebook E2E",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "upstream-key",
        "enabled": True,
    })
    assert resp.status_code == 201, resp.text
    await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})


async def _make_constant_book(client, name, marker_text, *, after_cantrips=False):
    """A book whose single constant entry always injects, so the test does not
    depend on keyword matching -- only on which side of the cantrip pass it
    lands."""
    resp = await client.post("/api/lorebooks", json={
        "name": name,
        "is_active": True,
        "inject_after_cantrips": after_cantrips,
    })
    assert resp.status_code in (200, 201), resp.text
    lb_id = resp.json()["id"]
    resp = await client.post(f"/api/lorebooks/{lb_id}/entries", json={
        "name": "constant",
        "content": marker_text,
        "is_constant": True,
        "position": "system_start",
    })
    assert resp.status_code == 201, resp.text
    return lb_id


class TestDeferredInjectionVisibility:
    @requires_deno
    async def test_cantrips_cannot_read_deferred_book_entries(self, admin_client, httpx_mock):
        """The headline claim: a book marked inject-after-cantrips reaches the
        model in the same request, but a pre-driver cantrip running on that
        request cannot see its entries."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)

        await client.post("/api/cantrips", json={
            "name": "Dragon Spotter",
            "code": (
                'const seen = context.chat.last_messages.some('
                'm => m.message.includes("DRAGONSTONE"));'
                'context.character.scenario += seen ? " MARKER_SEEN" : " MARKER_BLIND";'
            ),
            "is_active": True,
        })
        # The pre book proves the cantrip CAN read ordinary injected entries
        # in the same request; only the deferred book must be invisible.
        await _make_constant_book(client, "Pre Book", "PREBOOK_ENTRY_TEXT")
        await _make_constant_book(
            client, "Deferred Book", "DRAGONSTONE_DEFERRED_ENTRY",
            after_cantrips=True,
        )

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "t", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        forwarded = json.loads(httpx_mock.get_request().content)
        forwarded_text = json.dumps(forwarded["messages"])
        # The deferred entries still reach the model...
        assert "DRAGONSTONE_DEFERRED_ENTRY" in forwarded_text
        assert "PREBOOK_ENTRY_TEXT" in forwarded_text
        # ...but the cantrip saw only the pre book's content.
        assert "MARKER_BLIND" in forwarded_text
        assert "MARKER_SEEN" not in forwarded_text

    @requires_deno
    async def test_cantrips_read_entries_when_the_flag_is_off(self, admin_client, httpx_mock):
        """The control: without the flag the book injects before cantrips, so
        the same cantrip sees the same entry."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)

        await client.post("/api/cantrips", json={
            "name": "Dragon Spotter Control",
            "code": (
                'const seen = context.chat.last_messages.some('
                'm => m.message.includes("DRAGONSTONE"));'
                'context.character.scenario += seen ? " MARKER_SEEN" : " MARKER_BLIND";'
            ),
            "is_active": True,
        })
        await _make_constant_book(client, "Ordinary Book", "DRAGONSTONE_ORDINARY_ENTRY")

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "t", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        forwarded_text = json.dumps(
            json.loads(httpx_mock.get_request().content)["messages"]
        )
        assert "MARKER_SEEN" in forwarded_text
        assert "MARKER_BLIND" not in forwarded_text

    @requires_deno
    async def test_scan_depth_hides_old_history_from_the_book(self, admin_client, httpx_mock):
        """E2E for the window: a keyword mentioned only in ancient history must
        not trigger a depth-limited book, and must trigger an unlimited one."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)

        resp = await client.post("/api/lorebooks", json={
            "name": "Shallow Book", "is_active": True, "scan_depth": 4,
        })
        assert resp.status_code in (200, 201), resp.text
        await client.post(f"/api/lorebooks/{resp.json()['id']}/entries", json={
            "name": "dragon lore",
            "keys": ["dragon"],
            "content": "SHALLOW_BOOK_TRIGGERED",
            "position": "system_start",
        })
        resp = await client.post("/api/lorebooks", json={
            "name": "Deep Book", "is_active": True,
        })
        assert resp.status_code in (200, 201), resp.text
        await client.post(f"/api/lorebooks/{resp.json()['id']}/entries", json={
            "name": "dragon lore",
            "keys": ["dragon"],
            "content": "DEEP_BOOK_TRIGGERED",
            "position": "system_start",
        })

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "t", "object": "chat.completion", "created": 1, "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )
        history = [
            {"role": "user", "content": "once upon a time a dragon flew by"},
            *[{"role": "user", "content": "filler"} for _ in range(8)],
            {"role": "user", "content": "current message"},
        ]
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": history},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200, resp.text

        forwarded_text = json.dumps(
            json.loads(httpx_mock.get_request().content)["messages"]
        )
        assert "DEEP_BOOK_TRIGGERED" in forwarded_text
        assert "SHALLOW_BOOK_TRIGGERED" not in forwarded_text


# ============================================================================
# Turn counter
# ============================================================================

async def _counter_value(user_id: str, conversation_id: str) -> int | None:
    from sqlalchemy import select

    from app.models.conversation_turn_counter import ConversationTurnCounter
    from tests.conftest import TestSessionLocal

    async with TestSessionLocal() as db:
        return (await db.execute(
            select(ConversationTurnCounter.turn_count).where(
                ConversationTurnCounter.user_id == user_id,
                ConversationTurnCounter.conversation_id == conversation_id,
            )
        )).scalar_one_or_none()


class TestTurnCounterService:
    async def test_counts_1_2_3(self):
        from app.services.conversation import bump_turn_counter

        uid = "11111111-1111-1111-1111-111111111111"
        assert await bump_turn_counter(uid, "conv-a") == 1
        assert await bump_turn_counter(uid, "conv-a") == 2
        assert await bump_turn_counter(uid, "conv-a") == 3

    async def test_conversations_are_independent(self):
        from app.services.conversation import bump_turn_counter

        uid = "22222222-2222-2222-2222-222222222222"
        assert await bump_turn_counter(uid, "conv-x") == 1
        assert await bump_turn_counter(uid, "conv-y") == 1
        assert await bump_turn_counter(uid, "conv-x") == 2
        assert await bump_turn_counter(uid, "conv-y") == 2

    async def test_users_are_independent(self):
        from app.services.conversation import bump_turn_counter

        assert await bump_turn_counter("u-one", "same-conv") == 1
        assert await bump_turn_counter("u-two", "same-conv") == 1


class TestTurnIndexContext:
    def test_extract_context_params_reads_the_stamped_index(self):
        from app.services.cantrip_context import extract_context_params

        params = extract_context_params(
            {"model": "m", "_gitv_turn_index": 7, "_gitv_chat_id": "chat"}, {}
        )
        assert params["turn_index"] == 7

    def test_build_context_exposes_turn_index_in_chat(self):
        from app.services.cantrip_context import build_context

        ctx = build_context([{"role": "user", "content": "hi"}], turn_index=7)
        assert ctx["chat"]["turn_index"] == 7
        # Default 0 when nothing could be resolved.
        assert build_context([{"role": "user", "content": "hi"}])["chat"]["turn_index"] == 0


class TestTurnIndexEndToEnd:
    @staticmethod
    def _upstream():
        return {
            "id": "t", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    async def test_counter_advances_across_requests_and_survives_trimming(
        self, admin_client, httpx_mock
    ):
        """The complaint being fixed: message_count stalls once the client
        trims history, so cantrips keeping state could not count turns."""
        client, _, api_key = admin_client
        resp = await client.post("/api/endpoints", json={
            "name": "Turn E2E", "base_url": MOCK_ENDPOINT_URL,
            "api_key": "k", "enabled": True,
        })
        assert resp.status_code == 201, resp.text
        await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=self._upstream(), is_reusable=True,
        )
        conv = "turn-e2e-conversation"
        me = (await client.get("/api/auth/me")).json()["id"]

        long_history = [{"role": "user", "content": f"msg {i}"} for i in range(8)]
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": long_history},
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": conv},
        )
        assert resp.status_code == 200, resp.text

        # The client trims history: half the messages, same conversation.
        trimmed = long_history[:4] + [{"role": "user", "content": "and one more"}]
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": trimmed},
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": conv},
        )
        assert resp.status_code == 200, resp.text

        assert await _counter_value(me, conv) == 2

    async def test_conversations_get_separate_counters(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        resp = await client.post("/api/endpoints", json={
            "name": "Turn E2E 2", "base_url": MOCK_ENDPOINT_URL,
            "api_key": "k", "enabled": True,
        })
        assert resp.status_code == 201, resp.text
        await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})
        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=self._upstream(), is_reusable=True,
        )
        me = (await client.get("/api/auth/me")).json()["id"]

        for conv in ("conv-1", "conv-2", "conv-1"):
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {api_key}",
                         "x-conversation-id": conv},
            )
            assert resp.status_code == 200, resp.text

        assert await _counter_value(me, "conv-1") == 2
        assert await _counter_value(me, "conv-2") == 1

    async def test_replay_does_not_advance_the_counter(
        self, admin_client, httpx_mock
    ):
        """A replay must hold the conversation still between A and B; a
        counter that advances during replays would defeat its purpose."""
        from app.services.debug import get_exchange, list_exchanges

        client, _, api_key = admin_client
        await client.put("/api/settings", json={"debug_mode": True})
        me = (await client.get("/api/auth/me")).json()["id"]
        resp = await client.post("/api/endpoints", json={
            "name": "Turn Replay", "base_url": MOCK_ENDPOINT_URL,
            "api_key": "k", "enabled": True,
        })
        assert resp.status_code == 201, resp.text
        await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})
        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=self._upstream(), is_reusable=True,
        )

        conv = "replay-conv"
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": conv},
        )
        assert resp.status_code == 200, resp.text
        assert await _counter_value(me, conv) == 1

        source_id = (await list_exchanges(me, limit=1))[0]["id"]
        replay_resp = await client.post(f"/api/debug/{source_id}/replay")
        assert replay_resp.status_code == 200, replay_resp.text
        await get_exchange(me, replay_resp.json()["run_id"])  # replay really ran

        assert await _counter_value(me, conv) == 1


# ============================================================================
# Upgrade path (rule 10): the new columns and table must arrive by migration
# ============================================================================

class TestUpgradePath:
    @staticmethod
    async def _upgraded_engine():
        """A database the way an upgrade produces it: create_all from the
        models of the release BEFORE 057, then the migration set."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models import Base
        from app.services.migrations import run_migrations

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Roll the lorebook columns back to their pre-057 shape and
            # remove the turn-counter table entirely, as on 0.34.x.
            await conn.execute(
                text("ALTER TABLE lorebooks DROP COLUMN inject_after_cantrips;")
            )
            await conn.execute(text("ALTER TABLE lorebooks DROP COLUMN scan_depth;"))
            await conn.execute(text("DROP TABLE conversation_turn_counters;"))

        await run_migrations(engine)
        return engine

    @staticmethod
    async def _pre057_engine():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models import Base

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text("ALTER TABLE lorebooks DROP COLUMN inject_after_cantrips;")
            )
            await conn.execute(text("ALTER TABLE lorebooks DROP COLUMN scan_depth;"))
            await conn.execute(text("DROP TABLE conversation_turn_counters;"))
        return engine

    async def test_the_fixture_really_reproduces_the_pre057_state(self):
        """Vacuity check: assert the columns are genuinely absent before
        migrating, or this class proves nothing."""
        from sqlalchemy import inspect

        engine = await self._pre057_engine()
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(
                    lambda sync: [c["name"] for c in inspect(sync).get_columns("lorebooks")]
                )
                tables = await conn.run_sync(
                    lambda sync: inspect(sync).get_table_names()
                )
            assert "inject_after_cantrips" not in cols
            assert "scan_depth" not in cols
            assert "conversation_turn_counters" not in tables
        finally:
            await engine.dispose()

    async def test_migrating_057_restores_columns_and_table(self):
        from sqlalchemy import inspect

        engine = await self._upgraded_engine()
        try:
            async with engine.connect() as conn:
                cols = await conn.run_sync(
                    lambda sync: [c["name"] for c in inspect(sync).get_columns("lorebooks")]
                )
                tables = await conn.run_sync(
                    lambda sync: inspect(sync).get_table_names()
                )
            assert "inject_after_cantrips" in cols
            assert "scan_depth" in cols
            assert "conversation_turn_counters" in tables
        finally:
            await engine.dispose()

    async def test_the_hot_path_query_works_on_an_upgraded_database(self):
        """The lorebook loader selects whole ORM rows on every proxied
        request; a column the migration failed to add is a 500 on every
        request, not a broken admin page."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.models.lorebook import Lorebook

        engine = await self._upgraded_engine()
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                db.add(Lorebook(
                    user_id="u", name="Upgraded Book",
                    inject_after_cantrips=True, scan_depth=12,
                ))
                await db.commit()
                row = (await db.execute(select(Lorebook))).scalar_one()
                assert row.inject_after_cantrips is True
                assert row.scan_depth == 12
        finally:
            await engine.dispose()
