"""Phase 48 (0.48.0): Cache-friendly injection (A3) and lorebook budget enforcement
on map stages & manual pre-forward ordering (A5)."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.services import map_pipeline
from app.services.cantrip_context import apply_cantrip_result_to_messages
from app.services.lorebook import (
    MatchedEntry,
    append_user_tail,
    inject_entries,
    lorebook_budget_chars,
    match_book_entries,
    match_entries,
)
from app.services.map_pipeline import _inject_stage_lorebooks
from app.services.pipeline_order import _inject_book_now

# ============================================================================
# append_user_tail
# ============================================================================


class TestAppendUserTail:
    def test_appends_to_newest_string_user_message(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "latest"},
        ]
        snapshot = copy.deepcopy(msgs)
        out = append_user_tail(msgs, "TAIL")

        assert msgs == snapshot
        assert out[1]["content"] == "first"
        assert out[3]["content"] == "latest\n\nTAIL"

    def test_appends_to_multimodal_list_without_mutating_caller(self):
        parts = [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
        ]
        msgs = [{"role": "user", "content": parts}]
        snapshot = copy.deepcopy(msgs)
        out = append_user_tail(msgs, "EXTRA")

        assert msgs == snapshot
        assert len(parts) == 2
        assert out[0]["content"][-1] == {"type": "text", "text": "\n\nEXTRA"}
        assert len(out[0]["content"]) == 3

    def test_empty_user_content_sets_text_without_leading_newlines(self):
        msgs = [{"role": "user", "content": ""}]
        out = append_user_tail(msgs, "ONLY")
        assert out[0]["content"] == "ONLY"

    def test_no_user_message_appends_fallback_user_message(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "hi"},
        ]
        snapshot = copy.deepcopy(msgs)
        out = append_user_tail(msgs, "FALLBACK")

        assert msgs == snapshot
        assert len(out) == 3
        assert out[-1] == {"role": "user", "content": "FALLBACK"}

    def test_empty_text_returns_non_mutated_copy(self):
        msgs = [{"role": "user", "content": "hello"}]
        out = append_user_tail(msgs, "")
        assert out == msgs
        assert out is not msgs
        assert out[0] is not msgs[0]


# ============================================================================
# inject_entries(..., cache_friendly=True) & MatchedEntry.is_constant
# ============================================================================


class TestCacheFriendlyLorebookInjection:
    def test_match_entries_populates_is_constant(self):
        msgs = [{"role": "user", "content": "dragon"}]
        entries = [
            {
                "name": "world",
                "keys": "[]",
                "content": "Constant world info",
                "position": "before_last_message",
                "insertion_order": 1,
                "is_constant": True,
                "is_selective": False,
                "is_disabled": False,
            },
            {
                "name": "dragon",
                "keys": '["dragon"]',
                "content": "Dragon lore",
                "position": "before_last_message",
                "insertion_order": 2,
                "is_constant": False,
                "is_selective": False,
                "is_disabled": False,
            },
        ]
        matched = match_entries(msgs, entries)
        by_name = {m.name: m for m in matched}
        assert by_name["world"].is_constant is True
        assert by_name["dragon"].is_constant is False

        matched_book = match_book_entries(msgs, entries, scan_depth=5)
        by_name_book = {m.name: m for m in matched_book}
        assert by_name_book["world"].is_constant is True
        assert by_name_book["dragon"].is_constant is False

    def test_cache_friendly_redirects_non_constant_dynamic_entries_to_user_tail(self):
        msgs = [
            {"role": "system", "content": "BASE SYS"},
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Ack"},
            {"role": "user", "content": "Turn 2"},
        ]
        matched = [
            MatchedEntry(
                content="SYS_START_DYN",
                position="system_start",
                insertion_order=1,
                name="s1",
                is_constant=False,
            ),
            MatchedEntry(
                content="CONST_BEFORE",
                position="before_last_message",
                insertion_order=2,
                name="c1",
                is_constant=True,
            ),
            MatchedEntry(
                content="DYN_BEFORE",
                position="before_last_message",
                insertion_order=3,
                name="d1",
                is_constant=False,
            ),
            MatchedEntry(
                content="CONST_IN_CHAT",
                position="in_chat_system",
                insertion_order=4,
                name="c2",
                is_constant=True,
            ),
            MatchedEntry(
                content="DYN_IN_CHAT",
                position="in_chat_system",
                insertion_order=5,
                name="d2",
                is_constant=False,
            ),
        ]

        out = inject_entries(msgs, matched, cache_friendly=True)

        # system_start stays at the top
        assert "SYS_START_DYN" in out[0]["content"]
        # constant before_last_message stays inserted before the last user message
        last_user_idx = max(i for i, m in enumerate(out) if m["role"] == "user")
        assert out[last_user_idx - 1] == {"role": "system", "content": "CONST_BEFORE"}
        # non-constant before_last_message and in_chat_system land in user_tail
        assert "DYN_BEFORE" in out[last_user_idx]["content"]
        assert "DYN_IN_CHAT" in out[last_user_idx]["content"]
        # constant in_chat_system remains at the very end as its own system message
        assert out[-1] == {"role": "system", "content": "CONST_IN_CHAT"}
        # no extra system messages containing the redirected entries
        sys_contents = [m["content"] for m in out if m["role"] == "system"]
        assert all("DYN_BEFORE" not in c and "DYN_IN_CHAT" not in c for c in sys_contents)

    def test_cache_friendly_false_preserves_legacy_positions(self):
        msgs = [
            {"role": "system", "content": "BASE SYS"},
            {"role": "user", "content": "Hello"},
        ]
        matched = [
            MatchedEntry(
                content="DYN_BEFORE",
                position="before_last_message",
                insertion_order=1,
                name="d1",
                is_constant=False,
            ),
            MatchedEntry(
                content="DYN_IN_CHAT",
                position="in_chat_system",
                insertion_order=2,
                name="d2",
                is_constant=False,
            ),
        ]
        out = inject_entries(msgs, matched, cache_friendly=False)
        assert out[1] == {"role": "system", "content": "DYN_BEFORE"}
        assert out[2] == {"role": "user", "content": "Hello"}
        assert out[3] == {"role": "system", "content": "DYN_IN_CHAT"}


# ============================================================================
# apply_cantrip_result_to_messages(..., cache_friendly=True)
# ============================================================================


class TestCacheFriendlyCantripDeltas:
    CARD_SYSTEM = (
        "Preset text.\n"
        "<Alice's Persona>Kind and brave.</Alice's Persona>\n"
        "<Scenario>A quiet tavern.</Scenario>\n"
        "<UserPersona>Traveler.</UserPersona>\n"
    )

    def test_appended_scenario_delta_goes_to_user_tail_leaving_system_byte_identical(self):
        msgs = [
            {"role": "system", "content": self.CARD_SYSTEM},
            {"role": "user", "content": "I order an ale."},
        ]
        seeded = {
            "personality": "Kind and brave.",
            "scenario": "A quiet tavern.",
            "example_dialogs": "",
        }
        out = apply_cantrip_result_to_messages(
            msgs,
            result_personality="Kind and brave.",
            result_scenario="A quiet tavern.\nHP: 42\nGold: 10",
            result_example_dialogs="",
            cache_friendly=True,
            seeded_character=seeded,
        )

        # System message is byte-identical (prefix cache preserved!)
        assert out[0]["content"] == self.CARD_SYSTEM
        assert len(out) == 2
        assert out[1]["role"] == "user"
        assert out[1]["content"] == "I order an ale.\n\nHP: 42\nGold: 10"

    def test_full_replacement_still_updates_system_message(self):
        msgs = [
            {"role": "system", "content": self.CARD_SYSTEM},
            {"role": "user", "content": "I step through the portal."},
        ]
        seeded = {
            "personality": "Kind and brave.",
            "scenario": "A quiet tavern.",
            "example_dialogs": "",
        }
        out = apply_cantrip_result_to_messages(
            msgs,
            result_personality="Kind and brave.",
            result_scenario="A burning dungeon.",
            result_example_dialogs="",
            cache_friendly=True,
            seeded_character=seeded,
        )

        assert "<Scenario>A burning dungeon.</Scenario>" in out[0]["content"]
        assert out[1]["content"] == "I step through the portal."

    def test_cache_friendly_false_preserves_legacy_in_place_splice(self):
        msgs = [
            {"role": "system", "content": self.CARD_SYSTEM},
            {"role": "user", "content": "I order an ale."},
        ]
        seeded = {
            "personality": "Kind and brave.",
            "scenario": "A quiet tavern.",
            "example_dialogs": "",
        }
        out = apply_cantrip_result_to_messages(
            msgs,
            result_personality="Kind and brave.",
            result_scenario="A quiet tavern.\nHP: 42",
            result_example_dialogs="",
            cache_friendly=False,
            seeded_character=seeded,
        )

        assert "<Scenario>A quiet tavern.\nHP: 42</Scenario>" in out[0]["content"]
        assert out[1]["content"] == "I order an ale."

    def test_non_janitor_client_appends_delta_to_user_tail_when_cache_friendly(self):
        msgs = [
            {"role": "system", "content": "Plain system prompt."},
            {"role": "user", "content": "Status?"},
        ]
        seeded = {"personality": "", "scenario": "", "example_dialogs": ""}
        out = apply_cantrip_result_to_messages(
            msgs,
            result_personality="",
            result_scenario="HP: 99",
            result_example_dialogs="",
            cache_friendly=True,
            seeded_character=seeded,
        )

        assert len(out) == 2
        assert out[0]["content"] == "Plain system prompt."
        assert out[1]["content"] == "Status?\n\nHP: 99"


# ============================================================================
# Lorebook total_budget enforcement (A5) in pipeline_order & map_pipeline
# ============================================================================


def _fake_book(
    book_id: str,
    entries: list[SimpleNamespace],
    *,
    budget_weight: float = 1.0,
    reserved_tokens: int = 0,
    scan_depth: int = 0,
    word_boundary_keys: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=book_id,
        name=f"Book-{book_id}",
        entries=entries,
        budget_weight=budget_weight,
        reserved_tokens=reserved_tokens,
        scan_depth=scan_depth,
        word_boundary_keys=word_boundary_keys,
        inject_after_cantrips=False,
    )


def _fake_entry(entry_id: str, name: str, content: str, order: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=entry_id,
        lorebook_id="",
        lorebook_name="",
        name=name,
        keys='["trigger"]',
        secondary_keys="[]",
        content=content,
        position="before_last_message",
        insertion_order=order,
        is_constant=False,
        is_selective=False,
        is_disabled=False,
    )


class TestLorebookBudgetEnforcement:
    def test_lorebook_budget_chars_matches_proxy_formula(self):
        lb_weighted = _fake_book("lb-1", [], budget_weight=2.0, reserved_tokens=0)
        lb_reserved = _fake_book("lb-2", [], budget_weight=0.0, reserved_tokens=100)
        lb_uncapped = _fake_book("lb-3", [], budget_weight=0.0, reserved_tokens=0)
        allocs = {
            "allocations": {
                "lb-1": {"share": 50},
                "lb-2": {"share": 25},
                "lb-3": {"share": 100},
            }
        }
        assert lorebook_budget_chars(lb_weighted, allocs) == 200
        assert lorebook_budget_chars(lb_reserved, allocs) == 100
        assert lorebook_budget_chars(lb_uncapped, allocs) == 0
        assert lorebook_budget_chars(lb_weighted, None) == 0

    @pytest.mark.asyncio
    async def test_pipeline_order_inject_book_now_enforces_total_budget_and_cache_friendly(self):
        # Two entries of 40 chars each. Share=10 tokens -> 40 chars budget -> only first fits.
        e1 = _fake_entry("e1", "First", "A" * 40, order=1)
        e2 = _fake_entry("e2", "Second", "B" * 40, order=2)
        lb = _fake_book("lb-budget-1", [e1, e2], budget_weight=1.0)

        body_json = {
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "trigger"},
            ],
            "_gitv_budget_allocations": {
                "allocations": {"lb-budget-1": {"share": 10}},
            },
            "_gitv_cache_friendly": True,
        }

        injected_count = await _inject_book_now(body_json, "user-1", lb)

        assert injected_count == 1
        # Cache-friendly redirected the non-constant before_last_message entry to user_tail
        assert len(body_json["messages"]) == 2
        assert "A" * 40 in body_json["messages"][1]["content"]
        assert "B" * 40 not in body_json["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_map_pipeline_inject_stage_lorebooks_enforces_total_budget_and_cache_friendly(
        self, admin_client
    ):
        client, _, _ = admin_client
        me_resp = await client.get("/api/auth/me")
        assert me_resp.status_code == 200
        user_id = me_resp.json()["id"]

        async with map_pipeline.async_session() as db:
            lb = Lorebook(
                user_id=user_id,
                name="Stage Budget Book",
                budget_weight=1.0,
                reserved_tokens=0,
                scan_depth=0,
                word_boundary_keys=False,
                inject_after_cantrips=False,
            )
            db.add(lb)
            await db.flush()
            db.add(
                LorebookEntry(
                    lorebook_id=lb.id,
                    name="First",
                    keys='["trigger"]',
                    secondary_keys="[]",
                    content="X" * 40,
                    position="before_last_message",
                    insertion_order=1,
                    is_constant=False,
                    is_selective=False,
                    is_disabled=False,
                )
            )
            db.add(
                LorebookEntry(
                    lorebook_id=lb.id,
                    name="Second",
                    keys='["trigger"]',
                    secondary_keys="[]",
                    content="Y" * 40,
                    position="before_last_message",
                    insertion_order=2,
                    is_constant=False,
                    is_selective=False,
                    is_disabled=False,
                )
            )
            await db.commit()
            lb_id = lb.id

        resource = SimpleNamespace(
            resource_id=lb_id,
            resource_type="lorebook",
            position="pre_driver",
        )
        body_json = {
            "messages": [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "trigger"},
            ],
            "_gitv_budget_allocations": {
                "allocations": {lb_id: {"share": 10}},
            },
            "_gitv_cache_friendly": True,
        }

        await _inject_stage_lorebooks(body_json, [resource], "Stage 1", user_id)

        # Share=10 tokens -> 40 chars budget -> only "X"*40 injected, and redirected to user_tail
        assert len(body_json["messages"]) == 2
        assert "X" * 40 in body_json["messages"][1]["content"]
        assert "Y" * 40 not in body_json["messages"][1]["content"]
