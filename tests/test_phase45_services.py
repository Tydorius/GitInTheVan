"""Phase 0.45.0 service tests:
- Fixed token reservation alongside weighted shares (budget.py, proxy.py)
- N6 opt-in user_data prefix scoping (cantrip.py, driver_callable.py)
- Sequence tester token counter (sequence_tester.py)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.budget import allocate_budget, estimate_messages_tokens
from app.services.cantrip import (
    _merge_scoped_user_data,
    parse_user_data_prefixes,
    process_cantrips,
    process_cantrips_post_driver,
    select_user_data_seed,
)
from app.services.deno_runner import DENO_PATH, CantripResult
from app.services.sequence_tester import run_sequence


class TestAllocateBudgetReservations:
    def test_pure_weighted_unchanged(self):
        cantrips = [{"id": "c1", "name": "C1", "weight": 1.0}]
        lorebooks = [{"id": "l1", "name": "L1", "weight": 3.0}]
        result = allocate_budget(4000, cantrips, lorebooks)

        assert result["total"] == 4000
        assert result["remaining"] == 4000
        assert result["allocations"]["c1"]["share"] == 1000
        assert result["allocations"]["c1"]["detail_level"] == "bullets"
        assert result["allocations"]["c1"]["reserved_tokens"] == 0
        assert result["allocations"]["l1"]["share"] == 3000
        assert result["allocations"]["l1"]["detail_level"] == "summary"
        assert result["allocations"]["l1"]["reserved_tokens"] == 0

    def test_pure_weighted_zero_weights(self):
        cantrips = [{"id": "c1", "name": "C1", "weight": 0.0}]
        result = allocate_budget(4000, cantrips, [])
        assert result["allocations"]["c1"]["share"] == 0
        assert result["allocations"]["c1"]["detail_level"] == "bullets"
        assert result["allocations"]["c1"]["reserved_tokens"] == 0

    def test_fixed_reservation_plus_weighted_pool(self):
        cantrips = [
            {"id": "c_res", "name": "Reserved", "weight": 0.0, "reserved_tokens": 4500},
            {"id": "c_w1", "name": "W1", "weight": 1.0, "reserved_tokens": 0},
        ]
        lorebooks = [
            {"id": "l_w2", "name": "W2", "weight": 2.0, "reserved_tokens": 0},
        ]
        # Total budget = 9000 -> 4500 reserved, 4500 pool split 1:2 -> 1500 and 3000
        result = allocate_budget(9000, cantrips, lorebooks)
        allocs = result["allocations"]

        assert allocs["c_res"]["share"] == 4500
        assert allocs["c_res"]["reserved_tokens"] == 4500
        assert allocs["c_res"]["detail_level"] == "full"

        assert allocs["c_w1"]["share"] == 1500
        assert allocs["c_w1"]["detail_level"] == "summary"

        assert allocs["l_w2"]["share"] == 3000
        assert allocs["l_w2"]["detail_level"] == "summary"

    def test_fixed_reservation_with_zero_weighted_total(self):
        cantrips = [
            {"id": "c_res", "name": "Reserved", "weight": 0.0, "reserved_tokens": 2000},
            {"id": "c_w0", "name": "ZeroWeight", "weight": 0.0, "reserved_tokens": 0},
        ]
        result = allocate_budget(5000, cantrips, [])
        assert result["allocations"]["c_res"]["share"] == 2000
        assert result["allocations"]["c_res"]["detail_level"] == "summary"
        assert result["allocations"]["c_w0"]["share"] == 0
        assert result["allocations"]["c_w0"]["detail_level"] == "bullets"

    def test_oversubscribed_reservations_scale_proportionally(self):
        cantrips = [
            {"id": "r1", "name": "R1", "weight": 1.0, "reserved_tokens": 3000},
            {"id": "r2", "name": "R2", "weight": 1.0, "reserved_tokens": 3000},
            {"id": "r3", "name": "R3", "weight": 1.0, "reserved_tokens": 4000},
            {"id": "w1", "name": "W1", "weight": 10.0, "reserved_tokens": 0},
        ]
        # Total reserved = 10000 > injection_budget = 5001
        # Scaled: r1 = 1500, r2 = 1500, r3 = 2000 -> sum = 5000, remainder 1 -> r1 gets 1501
        result = allocate_budget(5001, cantrips, [])
        allocs = result["allocations"]

        assert allocs["w1"]["share"] == 0
        assert allocs["w1"]["detail_level"] == "bullets"

        total_allocated = sum(a["share"] for a in allocs.values())
        assert total_allocated == 5001
        assert allocs["r1"]["share"] == 1501
        assert allocs["r1"]["detail_level"] == "summary"
        assert allocs["r2"]["share"] == 1500
        assert allocs["r2"]["detail_level"] == "summary"
        assert allocs["r3"]["share"] == 2000
        assert allocs["r3"]["detail_level"] == "summary"

    def test_detail_level_derived_from_final_share(self):
        # Remainder pushes first item from 3999 -> 4000 ("full")
        cantrips = [
            {"id": "c1", "name": "C1", "weight": 1.0},
            {"id": "c2", "name": "C2", "weight": 1.0},
        ]
        # 7999 // 2 = 3999 each, remainder 1 -> c1 becomes 4000 -> "full", c2 stays 3999 -> "summary"
        result = allocate_budget(7999, cantrips, [])
        assert result["allocations"]["c1"]["share"] == 4000
        assert result["allocations"]["c1"]["detail_level"] == "full"
        assert result["allocations"]["c2"]["share"] == 3999
        assert result["allocations"]["c2"]["detail_level"] == "summary"


class TestUserDataPrefixScoping:
    def test_parse_user_data_prefixes(self):
        assert parse_user_data_prefixes(None) == []
        assert parse_user_data_prefixes("") == []
        assert parse_user_data_prefixes("not-valid-json") == []
        assert parse_user_data_prefixes('{"not": "a list"}') == []
        assert parse_user_data_prefixes('["kok:", "  ", "sys: "]') == ["kok:", "sys:"]
        assert parse_user_data_prefixes([" kok: ", ""]) == ["kok:"]

    def test_select_user_data_seed_skips_when_unused(self):
        seed, is_scoped = select_user_data_seed(
            {"kok:hp": 10, "other:x": 1}, "const x = 1;", ["kok:"]
        )
        assert seed == {}
        assert is_scoped is True

    def test_select_user_data_seed_unrestricted_when_no_prefixes(self):
        data = {"kok:hp": 10, "other:x": 1}
        seed, is_scoped = select_user_data_seed(data, "context.user_data.get('kok:hp')", "[]")
        assert seed == data
        assert seed is not data
        assert is_scoped is False

    def test_select_user_data_seed_filters_by_prefixes(self):
        data = {"kok:hp": 10, "kok": 5, "other:x": 1, "kok_extra": 2}
        seed, is_scoped = select_user_data_seed(
            data, "context.user_data.get('kok:hp')", '["kok:"]'
        )
        assert seed == {"kok:hp": 10}
        assert is_scoped is True

    def test_merge_scoped_confinement(self):
        """Scope is enforced on writes, not just reads: out-of-scope writes
        and deletes are dropped (the sandbox records deletes for keys the
        script never saw); in-scope new keys are legitimate writes."""
        existing = {"kok:hp": 10, "other:ledger": 99}
        seed = {"kok:hp": 10}
        merged = _merge_scoped_user_data(
            existing,
            seed,
            {"kok:hp": 20, "kok:new": 1, "other:ledger": 0, "rogue": 7},
            ["other:ledger", "rogue", "kok:gone"],
            prefixes=["kok:"],
            cantrip_name="ScopedCantrip",
        )
        assert merged["kok:hp"] == 20
        assert merged["kok:new"] == 1
        assert "kok:gone" not in merged
        # Out of scope: untouched even when explicitly named.
        assert merged["other:ledger"] == 99
        assert "rogue" not in merged

    @pytest.mark.asyncio
    async def test_driver_callable_scoping_confines_writes_and_deletes(self):
        """The driver-callable pass enforces the same confinement, including
        on the deletes set handed to the saver (a raw out-of-scope delete
        would delete the row despite the merged dict preserving it)."""
        from app.models.cantrip import Cantrip
        from app.services.driver_callable import ToolCall, execute_tool_call

        tool = Cantrip(
            id="c-scoped-tool",
            user_id="u1",
            name="ScopedTool",
            code="context.user_data.get('kok:x');",
            timeout_ms=1000,
        )
        tool.user_data_prefixes = '["kok:"]'
        call = ToolCall(name="ScopedTool", args={})

        fake_result = MagicMock(
            has_error=False,
            error="",
            chat_data={},
            chat_data_deletes=[],
            user_data={"kok:x": 2, "evil:set": 5},
            user_data_deletes=["kok:old", "evil:del"],
            cantrip_data={},
            cantrip_data_deletes=[],
            tool_result="ok",
            scenario="",
            personality="",
        )

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = mock_session
        mock_ctx.__aexit__.return_value = False

        with (
            patch("app.services.driver_callable.async_session", return_value=mock_ctx),
            patch("app.services.cantrip._load_chat_data", new_callable=AsyncMock, return_value={}),
            patch(
                "app.services.cantrip._load_user_data",
                new_callable=AsyncMock,
                return_value={"kok:x": 1, "kok:old": 9, "evil:del": 1, "evil:set": 0},
            ),
            patch("app.services.cantrip._load_cantrip_data", new_callable=AsyncMock, return_value={}),
            patch("app.services.cantrip._save_chat_data", new_callable=AsyncMock),
            patch("app.services.cantrip._save_user_data", new_callable=AsyncMock) as mock_save_ud,
            patch("app.services.cantrip._save_cantrip_data", new_callable=AsyncMock),
            patch("app.services.deno_runner.run_cantrip", new_callable=AsyncMock, return_value=fake_result),
        ):
            out = await execute_tool_call(
                tool=tool,
                call=call,
                messages=[{"role": "user", "content": "hi"}],
                request_headers={},
                user_id="u1",
                conversation_id="conv-1",
                persist_shared=True,
            )

        assert out == "ok"
        mock_save_ud.assert_awaited_once()
        saved = mock_save_ud.call_args.args[2]
        # In-scope: write applied, delete removed from the dict.
        assert saved["kok:x"] == 2
        assert "kok:old" not in saved
        # Out of scope: untouched in the dict, and excluded from the
        # deletes set handed to the saver.
        assert saved["evil:set"] == 0
        assert "evil:del" in saved
        assert mock_save_ud.call_args.kwargs["deletes"] == {"kok:old"}

    def test_map_transfer_coerces_bad_reserved_tokens(self):
        """Hand-edited map JSON with a non-numeric reserved_tokens coerces
        to 0 instead of raising on import."""
        from app.services.map_transfer import _coerce_reserved_tokens

        assert _coerce_reserved_tokens("lots") == 0
        assert _coerce_reserved_tokens("2.5") == 0
        assert _coerce_reserved_tokens(None) == 0
        assert _coerce_reserved_tokens(-3) == 0
        assert _coerce_reserved_tokens(1500) == 1500
        assert _coerce_reserved_tokens("1500") == 1500

    @pytest.mark.asyncio
    async def test_pre_and_post_driver_scoping_preserves_unseeded_keys(self):
        cantrip_obj = SimpleNamespace(
            id="c-scoped",
            name="ScopedCantrip",
            code="context.user_data.get('kok:hp');",
            tag="",
            is_active=True,
            is_public=False,
            user_id="u1",
            execution_order=1,
            timeout_ms=1000,
            run_pre_driver=True,
            run_post_driver=False,
            run_pre_navigator=False,
            run_post_navigator=True,
            run_on_manual=False,
            run_on_auto=True,
            inject_position="before_last",
            user_data_prefixes='["kok:"]',
            budget_weight=0.0,
            reserved_tokens=0,
            memory_types=[],
            lorebook_reads="[]",
        )

        seen_seeds: list[dict] = []

        async def fake_run_cantrip(*, code, context, chat_data, user_data, cantrip_data, timeout_ms, passenger_handler=None):
            seen_seeds.append(dict(user_data))
            # Cantrip mutates kok:hp, adds kok:mp, deletes kok:old
            new_ud = dict(user_data)
            new_ud["kok:hp"] = 99
            new_ud["kok:mp"] = 50
            new_ud.pop("kok:old", None)
            return CantripResult(
                personality=context["character"]["personality"],
                scenario=context["character"]["scenario"],
                example_dialogs=context["character"]["example_dialogs"],
                chat_data=dict(chat_data),
                user_data=new_ud,
                user_data_deletes={"kok:old"},
                cantrip_data=dict(cantrip_data),
            )

        class FakeSession:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *args):
                return False

        seeds = {
            "chat_data": {},
            "user_data": {"kok:hp": 10, "kok:old": 1, "other:secret": "keep-me"},
            "cantrip_data": {},
        }
        body_json = {
            "messages": [{"role": "user", "content": "hi"}],
            "_gitv_chat_id": "chat-1",
        }

        with (
            patch("app.services.cantrip.async_session", side_effect=lambda: FakeSession()),
            patch("app.services.cantrip._load_active_cantrips", new=AsyncMock(return_value=[cantrip_obj])),
            patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip),
        ):
            await process_cantrips(
                body_json, "u1", {}, tags=[], position="pre_driver", store_seeds=seeds
            )

        # Cantrip only ever saw kok:* keys
        assert seen_seeds[0] == {"kok:hp": 10, "kok:old": 1}
        # Writes & deletes applied, unseeded other:secret untouched
        assert seeds["user_data"] == {
            "kok:hp": 99,
            "kok:mp": 50,
            "other:secret": "keep-me",
        }

        # Now test post_driver scoping as well
        async def fake_post_run(*, code, context, chat_data, user_data, cantrip_data, timeout_ms, passenger_handler=None):
            seen_seeds.append(dict(user_data))
            new_ud = dict(user_data)
            new_ud["kok:hp"] = 100
            return CantripResult(
                response_content=context["response"]["content"],
                chat_data=dict(chat_data),
                user_data=new_ud,
                cantrip_data=dict(cantrip_data),
            )

        with (
            patch("app.services.cantrip.async_session", side_effect=lambda: FakeSession()),
            patch("app.services.cantrip._load_active_cantrips", new=AsyncMock(return_value=[cantrip_obj])),
            patch("app.services.cantrip._prepare_lorebook_seeding", new=AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip.run_cantrip", side_effect=fake_post_run),
        ):
            await process_cantrips_post_driver(
                "reply", body_json, "u1", {}, tags=[], position="post_navigator", store_seeds=seeds
            )

        assert seen_seeds[1] == {"kok:hp": 99, "kok:mp": 50}
        assert seeds["user_data"] == {
            "kok:hp": 100,
            "kok:mp": 50,
            "other:secret": "keep-me",
        }

    @pytest.mark.asyncio
    @pytest.mark.skipif(not DENO_PATH, reason="Deno not available")
    async def test_e2e_deno_cantrip_with_user_data_prefixes(self, admin_client):
        client, _, _ = admin_client
        code = (
            "const seen = context.user_data.keys();"
            "context.chat_data.set('seen_keys', seen);"
            "context.user_data.set('kok:gold', 42);"
            "context.user_data.delete('kok:old');"
        )
        resp = await client.post("/api/cantrips", json={
            "name": "ScopedDeno",
            "code": code,
            "user_data_prefixes": ["kok:"],
        })
        assert resp.status_code == 201, resp.text

        result = await run_sequence(
            (await client.get("/api/auth/me")).json()["id"],
            seed_user_data={"kok:hp": 10, "kok:old": "bye", "other:secret": "intact"},
            turns=[{"user_message": "go", "reply": "ok"}],
        )
        assert sorted(result["turns"][0]["chat_data"]["seen_keys"]) == ["kok:hp", "kok:old"]
        assert result["final_user_data"] == {
            "kok:hp": 10,
            "kok:gold": 42,
            "other:secret": "intact",
        }


class TestSequenceTesterTokenCounter:
    @pytest.mark.asyncio
    async def test_run_sequence_tracks_per_turn_and_total_token_delta(self):
        class FakeSession:
            async def __aenter__(self):
                return MagicMock()

            async def __aexit__(self, *args):
                return False

        async def fake_resolve_group_tags(db, user_id, tags):
            return tags, [], {}

        async def fake_process_cantrips(body_json, user_id, req_headers, **kwargs):
            # Simulate a pre_driver cantrip injecting a 40-char system message (+10 tokens)
            body_json["messages"] = [
                {"role": "system", "content": "x" * 40},
                *body_json["messages"],
            ]

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
                    {"user_message": "hello world!", "reply": "hi there"},
                    {"user_message": "next turn!!", "reply": "ok"},
                ],
            )

        assert len(res["turns"]) == 2
        for turn in res["turns"]:
            assert turn["tokens_before"] > 0
            assert turn["tokens_after"] == turn["tokens_before"] + estimate_messages_tokens(
                [{"role": "system", "content": "x" * 40}]
            )
            assert turn["token_delta"] == turn["tokens_after"] - turn["tokens_before"]
            assert turn["token_delta"] == 10

        assert res["total_token_delta"] == 20
        assert res["total_token_delta"] == sum(t["token_delta"] for t in res["turns"])
