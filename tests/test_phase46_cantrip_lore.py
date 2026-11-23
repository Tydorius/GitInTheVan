from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from app.routers.lorebook import EntryInput, _normalize_entry
from app.services.cantrip import apply_history_ops
from app.services.cantrip_context import build_context
from app.services.debug import init_debug
from app.services.deno_runner import DENO_PATH, _parse_history_ops, run_cantrip
from app.services.dynamic_lorebook import _VALID_POSITIONS, _coerce_fields
from app.services.lorebook import INJECTION_POSITIONS, MatchedEntry, inject_entries

SKIP_DENO = not DENO_PATH


def _make_context(messages: list[dict] | None = None) -> dict:
    msgs = messages or [
        {"role": "system", "content": "System prompt."},
        {"role": "user", "content": "First user message."},
        {"role": "assistant", "content": "First assistant reply."},
        {"role": "user", "content": "Latest user message."},
    ]
    return build_context(
        messages=msgs,
        conversation_id="conv-phase46",
        user_name="Tester",
        character_name="Aria",
        character_description="Test character",
        first_message="Hello!",
    )


class TestUserTailLorebookInjection:
    def test_user_tail_in_injection_positions(self):
        assert "user_tail" in INJECTION_POSITIONS

    def test_user_tail_appends_to_newest_user_string_message(self):
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "Earlier user msg."},
            {"role": "assistant", "content": "Earlier assistant msg."},
            {"role": "user", "content": "Newest user msg."},
        ]
        original_snapshot = copy.deepcopy(messages)
        matched = [
            MatchedEntry(content="[OOC: stay concise]", position="user_tail", insertion_order=10, name="ooc")
        ]

        result = inject_entries(messages, matched)

        assert messages == original_snapshot, "caller message dicts must not be mutated"
        assert len(result) == 4
        assert result[0]["content"] == "System prompt."
        assert result[1]["content"] == "Earlier user msg."
        assert result[2]["content"] == "Earlier assistant msg."
        assert result[3]["content"] == "Newest user msg.\n\n[OOC: stay concise]"

    def test_user_tail_empty_user_content(self):
        messages = [{"role": "user", "content": ""}]
        matched = [MatchedEntry(content="Appended note", position="user_tail", insertion_order=10, name="n")]

        result = inject_entries(messages, matched)

        assert result == [{"role": "user", "content": "Appended note"}]

    def test_user_tail_multimodal_list_content(self):
        parts = [
            {"type": "text", "text": "Look at this image"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ]
        messages = [
            {"role": "system", "content": "System."},
            {"role": "user", "content": parts},
        ]
        original_parts = copy.deepcopy(parts)
        matched = [
            MatchedEntry(content="Multimodal tail note", position="user_tail", insertion_order=5, name="mm")
        ]

        result = inject_entries(messages, matched)

        assert parts == original_parts, "caller multimodal list must not be mutated"
        assert len(result) == 2
        assert result[1]["content"] == [
            {"type": "text", "text": "Look at this image"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            {"type": "text", "text": "\n\nMultimodal tail note"},
        ]

    def test_user_tail_multiple_entries_combined(self):
        messages = [
            {"role": "system", "content": "System."},
            {"role": "user", "content": "Hello"},
        ]
        matched = [
            MatchedEntry(content="First tail", position="user_tail", insertion_order=1, name="a"),
            MatchedEntry(content="Second tail", position="user_tail", insertion_order=2, name="b"),
        ]

        result = inject_entries(messages, matched)

        assert len(result) == 2
        assert result[1]["content"] == "Hello\n\nFirst tail\n\nSecond tail"

    def test_user_tail_no_user_message_fallback(self):
        messages = [
            {"role": "system", "content": "System only."},
            {"role": "assistant", "content": "Assistant greeting."},
        ]
        matched = [
            MatchedEntry(content="Fallback user tail", position="user_tail", insertion_order=10, name="fb"),
            MatchedEntry(content="End directive", position="in_chat_system", insertion_order=20, name="ics"),
        ]

        result = inject_entries(messages, matched)

        assert result[0] == {"role": "system", "content": "System only."}
        assert result[1] == {"role": "assistant", "content": "Assistant greeting."}
        assert result[2] == {"role": "user", "content": "Fallback user tail"}
        assert result[3] == {"role": "system", "content": "End directive"}


class TestUserTailValidation:
    def test_dynamic_lorebook_accepts_user_tail(self):
        assert "user_tail" in _VALID_POSITIONS
        coerced = _coerce_fields({"name": "note", "content": "hello", "position": "user_tail"}, partial=False)
        assert coerced is not None
        assert coerced["position"] == "user_tail"

    def test_router_entry_input_accepts_user_tail_and_rejects_invalid(self):
        entry = EntryInput(name="Tail", keys=["k"], content="text", position="user_tail")
        assert entry.position == "user_tail"

        with pytest.raises(ValidationError):
            EntryInput(name="Bad", keys=["k"], content="text", position="not_a_real_position")

    def test_router_normalize_entry_preserves_user_tail(self):
        norm = _normalize_entry({"name": "Tail", "keys": ["k"], "content": "hello", "position": "user_tail"})
        assert norm["position"] == "user_tail"

        fallback = _normalize_entry({"name": "Other", "keys": ["k"], "content": "hello", "position": "bogus"})
        assert fallback["position"] == "before_last_message"

    @pytest.mark.asyncio
    async def test_lorebook_router_api_accepts_user_tail(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        lb_resp = await client.post("/api/lorebooks", json={"name": "UserTailBook"}, headers=headers)
        assert lb_resp.status_code == 201
        lb_id = lb_resp.json()["id"]

        entry_resp = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "TailEntry",
                "keys": ["dragon"],
                "content": "Append to user tail.",
                "position": "user_tail",
            },
            headers=headers,
        )
        assert entry_resp.status_code == 201, entry_resp.text
        assert entry_resp.json()["position"] == "user_tail"

        bad_resp = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "BadEntry",
                "keys": ["dragon"],
                "content": "Bad pos.",
                "position": "invalid_pos",
            },
            headers=headers,
        )
        assert bad_resp.status_code == 422


class TestApplyHistoryOps:
    def test_keep_recent_preserves_system_and_newest_n(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "system", "content": "sys_mid"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ]
        hash_anchor = copy.deepcopy(msgs)
        body = {
            "messages": copy.deepcopy(msgs),
            "_gitv_messages_for_hash": hash_anchor,
            "_gitv_history_ops": [{"op": "keep_recent", "count": 2}],
        }
        init_debug(body, [])

        summary = apply_history_ops(body)

        assert summary == {"ops_applied": 1, "before_count": 7, "after_count": 4, "dropped_count": 3}
        assert [m["content"] for m in body["messages"]] == ["sys0", "sys_mid", "a2", "u3"]
        assert body["_gitv_messages_for_hash"] == msgs, "hash anchor must never be mutated"
        assert "_gitv_history_ops" not in body

        stages = body["_gitv_debug"]["stages"]
        assert any(s["name"] == "history_control" and s["label"] == "History Control" for s in stages)

    def test_keep_recent_zero_still_preserves_newest_user_message(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        body = {"messages": copy.deepcopy(msgs)}

        summary = apply_history_ops(body, [{"op": "keep_recent", "count": 0}])

        assert summary == {"ops_applied": 1, "before_count": 4, "after_count": 2, "dropped_count": 2}
        assert [m["content"] for m in body["messages"]] == ["sys0", "u2"]

    def test_keep_recent_preserves_newest_user_when_trailing_assistant(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        body = {"messages": copy.deepcopy(msgs)}

        summary = apply_history_ops(body, [{"op": "keep_recent", "count": 1}])

        assert summary == {"ops_applied": 1, "before_count": 5, "after_count": 3, "dropped_count": 2}
        assert [m["content"] for m in body["messages"]] == ["sys0", "u2", "a2"]

    def test_drop_indices_positive_negative_and_protects_newest_user(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        hash_anchor = copy.deepcopy(msgs)
        body = {
            "messages": copy.deepcopy(msgs),
            "_gitv_messages_for_hash": hash_anchor,
        }

        # Try to drop u1 (1), a1 (-2), newest user (3 and -1), and out-of-bounds (99)
        summary = apply_history_ops(body, [{"op": "drop", "indices": [1, -2, 3, -1, 99]}])

        assert summary == {"ops_applied": 1, "before_count": 4, "after_count": 2, "dropped_count": 2}
        assert [m["content"] for m in body["messages"]] == ["sys0", "u2"]
        assert body["_gitv_messages_for_hash"] == msgs

    def test_noop_when_no_ops(self):
        msgs = [{"role": "user", "content": "hi"}]
        body = {"messages": msgs}
        summary = apply_history_ops(body)
        assert summary == {"ops_applied": 0, "before_count": 1, "after_count": 1, "dropped_count": 0}
        assert body["messages"] is msgs

    def test_parse_history_ops_validation(self):
        raw = [
            {"op": "keep_recent", "count": 3},
            {"op": "keep_recent", "count": -1},
            {"op": "keep_recent", "count": True},
            {"op": "drop", "indices": [1, -2, "bad", True, 1.5]},
            {"op": "unknown", "count": 1},
            "not-a-dict",
        ]
        parsed = _parse_history_ops(raw)
        assert parsed == [
            {"op": "keep_recent", "count": 3},
            {"op": "drop", "indices": [1, -2]},
        ]


@pytest.mark.skipif(SKIP_DENO, reason="Deno not available")
class TestDenoEndToEndPhase46:
    @pytest.mark.asyncio
    async def test_cantrip_inject_user_tail_and_history_ops(self):
        code = """
        context.inject("Remember the lantern.", "user_tail");
        context.history.keep_recent(2);
        context.history.drop([1]);
        context.history.drop(-2);
        context.history.keep_recent(NaN);
        context.history.drop(["not-an-int", 1.5]);
        """
        result = await run_cantrip(code, _make_context())
        assert result.error is None, result.error
        assert result.injections == [{"text": "Remember the lantern.", "position": "user_tail"}]
        assert result.history_ops == [
            {"op": "keep_recent", "count": 2},
            {"op": "drop", "indices": [1]},
            {"op": "drop", "indices": [-2]},
        ]

        # Apply both to a prompt body and verify end-to-end behavior.
        body = {
            "messages": [
                {"role": "system", "content": "System."},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "u3"},
            ],
            "_gitv_messages_for_hash": json.loads(
                json.dumps([{"role": "user", "content": "anchor"}])
            ),
        }
        init_debug(body, [])

        # First keep_recent(2) -> [System., a2, u3]
        # Then drop([1]) -> drops a2 -> [System., u3]
        # Then drop([-2]) -> drops System. -> [u3]
        summary = apply_history_ops(body, result.history_ops)
        assert summary["ops_applied"] == 3
        assert [m["content"] for m in body["messages"]] == ["u3"]
        assert body["_gitv_messages_for_hash"] == [{"role": "user", "content": "anchor"}]

        entries = [
            MatchedEntry(content=inj["text"], position=inj["position"], insertion_order=1000, name="")
            for inj in result.injections
        ]
        body["messages"] = inject_entries(body["messages"], entries)
        assert body["messages"] == [{"role": "user", "content": "u3\n\nRemember the lantern."}]


class TestSequenceTesterHistoryOps:
    """The Sequence Tester's contract is "per-turn pipeline order matches the
    proxy" -- a context.history cantrip must trim there too, or the tester
    silently diverges and the 0.45.0 token counters misreport."""

    @pytest.mark.asyncio
    async def test_run_sequence_applies_history_ops_each_turn(self):
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.services.sequence_tester import run_sequence

        class FakeSession:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *args):
                return False

        entry_lengths: list[int] = []
        stale_ops_at_entry: list[bool] = []

        async def fake_resolve_group_tags(db, user_id, tags):
            return tags, [], {}

        async def fake_process_cantrips(body_json, user_id, req_headers, **kwargs):
            entry_lengths.append(len(body_json["messages"]))
            stale_ops_at_entry.append("_gitv_history_ops" in body_json)
            # A pre-driver cantrip asking to keep only the newest message.
            body_json["_gitv_history_ops"] = [{"op": "keep_recent", "count": 1}]

        async def fake_post_driver(content, body_json, user_id, req_headers, **kwargs):
            return content

        async def fake_capture(response_data, user_id, body_json):
            return None

        with (
            patch("app.services.cantrip.async_session", side_effect=lambda: FakeSession()),
            patch("app.services.group_resolver.resolve_group_tags", side_effect=fake_resolve_group_tags),
            patch("app.services.sequence_tester.process_cantrips", side_effect=fake_process_cantrips),
            patch("app.services.sequence_tester.process_cantrips_post_driver", side_effect=fake_post_driver),
            patch("app.services.response_tags.process_response_capture", side_effect=fake_capture),
            patch("app.services.sequence_tester.flush_metrics", new=AsyncMock()),
        ):
            res = await run_sequence(
                "u1",
                turns=[
                    {"user_message": "one", "reply": "r1"},
                    {"user_message": "two", "reply": "r2"},
                    {"user_message": "three", "reply": "r3"},
                ],
            )

        assert len(res["turns"]) == 3
        # Each turn STARTS from the client's view (the tester's own history is
        # untrimmed, like a real client resending full history)...
        assert entry_lengths == [1, 3, 5]
        # ...but the list forwarded after the pre-driver window is trimmed:
        # tokens_after counts only the message the cantrip kept. Without the
        # tester applying history ops, tokens_after == tokens_before and the
        # delta ignores the trim entirely.
        from app.services.budget import estimate_messages_tokens

        for turn, text in zip(res["turns"], ["one", "two", "three"]):
            assert turn["tokens_after"] == estimate_messages_tokens(
                [{"role": "user", "content": text}]
            )
        assert res["turns"][1]["token_delta"] < 0
        assert res["turns"][2]["token_delta"] < 0
        # The ops key is consumed each turn, never carried stale into the next.
        assert stale_ops_at_entry == [False, False, False]


def test_history_drop_indices_are_capped():
    from app.services.deno_runner import MAX_HISTORY_DROP_INDICES

    ops = _parse_history_ops([{"op": "drop", "indices": list(range(500))}])
    assert len(ops) == 1
    assert len(ops[0]["indices"]) == MAX_HISTORY_DROP_INDICES
