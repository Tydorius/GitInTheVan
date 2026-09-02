"""Context compaction and conversation rotation (Phase 26b).

No conversation limit is ever a wall, so these tests are about the three
mechanisms that make that true: elision (free, and usually enough), the rolling
summary (one upstream call, and never allowed to break the turn when it fails),
and rotation of the oldest *unsaved* conversation.
"""

from __future__ import annotations

import json

import pytest

from app.services.assistant import compaction, store
from app.services.assistant.compaction import (
    DEFAULT_KEEP_LAST,
    Outbound,
    apply,
    elide_tool_results,
    outbound_with_summary,
    plan,
    trim_stored,
)
from app.services.budget import estimate_messages_tokens

MOCK_URL = "https://compaction-upstream.test"


def _tool_heavy(pairs: int = 12, body_chars: int = 4000) -> list[dict]:
    """A conversation whose bulk is tool results, as a real one would be."""
    messages: list[dict] = []
    for index in range(pairs):
        messages.append({"role": "user", "content": f"question {index}"})
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "list_cantrips", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "name": "list_cantrips",
                "content": json.dumps({"cantrips": ["c" * body_chars]}),
            }
        )
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


class TestElision:
    def test_old_tool_results_are_replaced(self):
        messages = _tool_heavy(pairs=4)
        elided = elide_tool_results(messages, keep_last=4)
        old = [m for m in elided[:-4] if m["role"] == "tool"]
        assert old
        for message in old:
            assert message["content"].startswith("[tool result elided: list_cantrips,")

    def test_recent_tool_results_are_untouched(self):
        messages = _tool_heavy(pairs=4)
        elided = elide_tool_results(messages, keep_last=4)
        assert elided[-2]["content"] == messages[-2]["content"]

    def test_the_stored_list_is_not_mutated(self):
        messages = _tool_heavy(pairs=4)
        before = json.dumps(messages)
        elide_tool_results(messages, keep_last=4)
        assert json.dumps(messages) == before

    def test_non_tool_messages_are_never_elided(self):
        messages = _tool_heavy(pairs=4)
        elided = elide_tool_results(messages, keep_last=2)
        for original, result in zip(messages, elided, strict=True):
            if original["role"] != "tool":
                assert result == original


class TestPlan:
    @pytest.mark.asyncio
    async def test_elision_alone_brings_it_under_budget_with_no_llm_call(self, httpx_mock):
        """The cheap step must be enough for an ordinary tool-heavy chat.

        `httpx_mock` asserts every registered response is consumed, so
        registering none and passing proves no upstream call was made.
        """
        messages = _tool_heavy(pairs=12, body_chars=4000)
        budget = 8000
        assert estimate_messages_tokens(messages) > budget * 0.8

        outbound = plan(messages, {}, budget)
        assert outbound.needs_summary is False
        assert estimate_messages_tokens(outbound.messages) <= budget * 0.8

    def test_over_budget_after_elision_asks_for_a_summary(self):
        # Long assistant text, which elision cannot touch.
        messages = [{"role": "assistant", "content": "z" * 6000} for _ in range(30)]
        outbound = plan(messages, {}, 8000)
        assert outbound.needs_summary is True
        assert outbound.fold_through == len(messages) - DEFAULT_KEEP_LAST

    def test_a_short_conversation_never_needs_a_summary(self):
        messages = [{"role": "user", "content": "hi"}]
        outbound = plan(messages, {}, 64000)
        assert outbound == Outbound(messages=messages, needs_summary=False, fold_through=0)

    def test_nothing_to_fold_means_no_summary(self):
        messages = [{"role": "assistant", "content": "z" * 90000} for _ in range(4)]
        outbound = plan(messages, {}, 1000)
        assert outbound.needs_summary is False


class TestOutboundWithSummary:
    def test_outbound_is_summary_then_the_tail(self):
        messages = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        compacted = apply({}, "the story so far", 22)
        outbound = outbound_with_summary(messages, compacted, keep_last=DEFAULT_KEEP_LAST)

        assert outbound[0]["role"] == "system"
        assert "the story so far" in outbound[0]["content"]
        assert len(outbound) == 1 + DEFAULT_KEEP_LAST
        assert [m["content"] for m in outbound[1:]] == [f"m{i}" for i in range(22, 30)]

    def test_no_summary_means_no_system_note(self):
        messages = [{"role": "user", "content": "hi"}]
        assert outbound_with_summary(messages, {}, 8) == messages

    def test_a_through_index_past_the_end_is_clamped(self):
        messages = [{"role": "user", "content": "hi"}]
        outbound = outbound_with_summary(messages, {"summary": "s", "through_index": 99}, 8)
        assert len(outbound) == 1
        assert outbound[0]["role"] == "system"


class TestApplyAndTrim:
    def test_apply_records_the_point(self):
        out = apply({}, "summary one", 10)
        assert out["summary"] == "summary one"
        assert out["through_index"] == 10
        assert out["revisions"] == 1

    def test_apply_rolls_forward(self):
        out = apply(apply({}, "one", 10), "two", 20)
        assert out["summary"] == "two"
        assert out["through_index"] == 20
        assert out["revisions"] == 2

    def test_trim_stored_shrinks_old_tool_bodies(self):
        messages = _tool_heavy(pairs=6)
        trimmed = trim_stored(messages, through_index=12)
        old_tools = [m for m in trimmed[:12] if m["role"] == "tool"]
        assert old_tools
        for message in old_tools:
            assert len(message["content"]) <= compaction.STORED_TOOL_LIMIT + 60
            assert "behind the compaction point" in message["content"]

    def test_trim_stored_leaves_recent_bodies_alone(self):
        messages = _tool_heavy(pairs=6)
        trimmed = trim_stored(messages, through_index=12)
        assert trimmed[14]["content"] == messages[14]["content"]

    def test_trim_stored_never_touches_text_messages(self):
        messages = [{"role": "assistant", "content": "z" * 9000}]
        assert trim_stored(messages, 1) == messages


class TestSummarize:
    @pytest.mark.asyncio
    async def test_one_call_and_the_summary_comes_back(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        candidate = await _candidate(client)

        httpx_mock.add_response(
            url=f"{MOCK_URL}/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": "- decided X"}}]},
        )
        summary = await compaction.summarize(
            candidate, "test-model", [], [{"role": "user", "content": "hello"}], ""
        )
        assert summary == "- decided X"
        assert len(httpx_mock.get_requests()) == 1

        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert "tools" not in sent
        assert sent["stream"] is False

    @pytest.mark.asyncio
    async def test_prior_summary_is_rolled_in(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        candidate = await _candidate(client)
        httpx_mock.add_response(
            url=f"{MOCK_URL}/v1/chat/completions",
            json={"choices": [{"message": {"content": "rolled"}}]},
        )
        await compaction.summarize(
            candidate, "test-model", [], [{"role": "user", "content": "new"}], "old summary"
        )
        sent = json.loads(httpx_mock.get_requests()[0].content)
        assert "old summary" in sent["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_a_failed_summary_returns_none_and_does_not_raise(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        candidate = await _candidate(client)
        httpx_mock.add_response(
            url=f"{MOCK_URL}/v1/chat/completions", status_code=500, json={"error": "nope"}
        )
        assert await compaction.summarize(
            candidate, "test-model", [], [{"role": "user", "content": "x"}], ""
        ) is None

    @pytest.mark.asyncio
    async def test_empty_content_is_treated_as_a_failure(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        candidate = await _candidate(client)
        httpx_mock.add_response(
            url=f"{MOCK_URL}/v1/chat/completions",
            json={"choices": [{"message": {"content": "   "}}]},
        )
        assert await compaction.summarize(
            candidate, "test-model", [], [{"role": "user", "content": "x"}], ""
        ) is None

    @pytest.mark.asyncio
    async def test_nothing_to_fold_short_circuits(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        candidate = await _candidate(client)
        assert await compaction.summarize(candidate, "m", [], [], "prior") == "prior"


async def _candidate(client):
    """An endpoint the summariser can call, built the way the loop builds it."""
    resp = await client.post(
        "/api/endpoints",
        json={"name": "Compactor", "base_url": MOCK_URL, "api_key": "sk-compact", "default_model": "test-model"},
    )
    assert resp.status_code == 201
    from sqlalchemy import select

    from app.models.endpoint import Endpoint
    from app.services.assistant.store import async_session
    from app.services.routing import _endpoint_to_candidate

    async with async_session() as db:
        row = (await db.execute(select(Endpoint).where(Endpoint.id == resp.json()["id"]))).scalar_one()
        return _endpoint_to_candidate(row, "test-model")


class TestRotation:
    @pytest.mark.asyncio
    async def test_rotation_removes_the_oldest_unsaved(self, admin_client):
        """Seeded so the oldest is neither the first nor the last inserted."""
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()["id"]

        # Insert four, then back-date the third so age, not insertion order,
        # decides. A cap of four means the next create must evict exactly it.
        rows = []
        for index in range(4):
            row, _ = await store.create(me, 99)
            await store.patch(me, row.id, title=f"conv-{index}")
            rows.append(row.id)

        from datetime import UTC, datetime

        from sqlalchemy import select

        from app.models.assistant_conversation import AssistantConversation
        from app.services.assistant.store import async_session

        async with async_session() as db:
            target = (
                await db.execute(
                    select(AssistantConversation).where(AssistantConversation.id == rows[2])
                )
            ).scalar_one()
            target.created_at = datetime(2020, 1, 1, tzinfo=UTC)
            await db.commit()

        new_row, rotated = await store.create(me, 4)
        assert rotated == "conv-2"

        remaining = {c["id"] for c in await store.list_conversations(me)}
        assert rows[2] not in remaining
        assert rows[0] in remaining and rows[1] in remaining and rows[3] in remaining
        assert new_row.id in remaining

    @pytest.mark.asyncio
    async def test_a_saved_conversation_is_never_rotated(self, admin_client):
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()["id"]

        oldest, _ = await store.create(me, 99)
        await store.patch(me, oldest.id, title="pinned")
        await store.set_saved(me, oldest.id, True)
        second, _ = await store.create(me, 99)
        await store.patch(me, second.id, title="disposable")

        # Cap of one unsaved: the pinned row is exempt, so the disposable one
        # is what has to go.
        _, rotated = await store.create(me, 1)
        assert rotated == "disposable"
        remaining = {c["id"] for c in await store.list_conversations(me)}
        assert oldest.id in remaining
        assert second.id not in remaining

    @pytest.mark.asyncio
    async def test_rotation_is_scoped_to_one_user(self, admin_client):
        client, _, _ = admin_client
        me = (await client.get("/api/auth/me")).json()["id"]
        created = await client.post(
            "/api/users", json={"username": "other", "password": "otherpass123", "is_admin": False}
        )
        assert created.status_code in (200, 201)
        other = created.json()["id"]

        theirs, _ = await store.create(other, 99)
        await store.patch(other, theirs.id, title="theirs")
        mine, _ = await store.create(me, 99)
        await store.patch(me, mine.id, title="mine")

        _, rotated = await store.create(me, 1)
        assert rotated == "mine"
        assert theirs.id in {c["id"] for c in await store.list_conversations(other)}
