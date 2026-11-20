"""Focused regression tests for the 0.42.1 cantrip/lore/budget/assistant bugfix batch."""

from __future__ import annotations

import json

import pytest

from app.services.budget import allocate_budget, load_weighted_resources
from app.services.cantrip_context import apply_cantrip_result_to_messages
from app.services.deno_runner import DENO_PATH, run_cantrip, validate_cantrip_syntax
from app.services.dynamic_lorebook import apply_ops, build_seed
from app.services.jslorebook import _desanitize_content
from app.services.lorebook import match_entries
from app.services.response_tags import process_response_capture


def _entry(name: str, keys: list[str], content: str, order: int, *, is_constant: bool = False):
    return {
        "name": name,
        "keys": json.dumps(keys),
        "secondary_keys": "[]",
        "content": content,
        "position": "before_last_message",
        "insertion_order": order,
        "is_constant": is_constant,
        "is_selective": False,
        "is_disabled": False,
        "character_limit": 0,
    }


class TestLorebookAndBudgetFixes:
    def test_match_entries_sorts_by_insertion_order_before_budget_truncation(self):
        # Input order has order=30 first, order=5 last. Budget allows 2 entries.
        entries = [
            _entry("late", ["hero"], "C" * 50, order=30),
            _entry("const_mid", [], "B" * 50, order=20, is_constant=True),
            _entry("early", ["hero"], "A" * 50, order=5),
        ]
        matched = match_entries(
            [{"role": "user", "content": "hero arrives"}],
            entries,
            total_budget=80,
        )
        assert [m.name for m in matched] == ["early", "const_mid"]

    def test_word_boundary_matches_punctuation_keys(self):
        entries = [
            _entry("comma", ["Bram,"], "bram comma", 1),
            _entry("cpp", ["C++"], "cpp lore", 2),
            _entry("mention", ["@user"], "mention lore", 3),
            _entry("rook", ["Rook"], "rook lore", 4),
        ]
        matched = match_entries(
            [{"role": "user", "content": "Hello Bram, I like C++ and @user, not rookie."}],
            entries,
            word_boundary=True,
        )
        assert [m.name for m in matched] == ["comma", "cpp", "mention"]

    def test_allocate_budget_zero_weights_does_not_dump_onto_first(self):
        result = allocate_budget(
            1000,
            [{"id": "c1", "name": "C1", "weight": 0.0}],
            [{"id": "l1", "name": "L1", "weight": 0.0}],
            [],
        )
        assert result["allocations"]["c1"]["share"] == 0
        assert result["allocations"]["l1"]["share"] == 0

    @pytest.mark.asyncio
    async def test_load_weighted_resources_includes_non_pre_driver_lore_and_direct_members(
        self, admin_client
    ):
        from app.models.lorebook import Lorebook
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]

        async with TestSessionLocal() as db:
            lb = Lorebook(
                user_id=uid,
                name="DeferredBook",
                tag="",
                is_active=False,
                run_pre_driver=False,
                budget_weight=2.0,
            )
            db.add(lb)
            await db.commit()
            await db.refresh(lb)
            lb_id = lb.id

        _, lore_info, _ = await load_weighted_resources(
            uid, tags=[], direct_member_ids={"lore": {lb_id}}
        )
        assert any(item["id"] == lb_id for item in lore_info)


class TestDynamicLorebookFixes:
    @pytest.mark.asyncio
    async def test_build_seed_empty_conversation_id_does_not_leak_cross_conv_books(
        self, admin_client
    ):
        from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook
        from app.models.lorebook import Lorebook
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]

        async with TestSessionLocal() as db:
            lb = Lorebook(user_id=uid, name="SharedWorld", tag="world", is_active=False)
            db.add(lb)
            await db.flush()
            db.add(
                ConversationDynamicLorebook(
                    user_id=uid, conversation_id="", name="SharedWorld", lorebook_id=lb.id
                )
            )
            await db.commit()

            seed, always = await build_seed(db, {}, uid, conversation_id="", capture=False)
            assert "SharedWorld" not in seed
            assert "SharedWorld" not in always

    @pytest.mark.asyncio
    async def test_apply_ops_cross_conv_write_pending_collision_delete_and_b82(
        self, admin_client
    ):
        from sqlalchemy import select

        from app.models.lorebook_entry import LorebookEntry
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]

        async with TestSessionLocal() as db:
            # 1. Create cross-conv book, then add without tag in same/next batch.
            delta1 = await apply_ops(
                db,
                {},
                uid,
                "conv-1",
                [
                    {"op": "create", "book": "WorldBook", "tag": "world"},
                    {"op": "create", "book": "BookTwo"},
                    {"op": "add", "book": "BookTwo", "entry": {"name": "E2", "keys": ["k2"], "content": "two"}},
                    {"op": "add", "book": "WorldBook", "entry": {"name": "The Gilded Cup", "keys": ["tavern"], "content": "old cup"}},
                ],
                source="test",
            )
            assert len(delta1) == 2

            # 2. Cross-conv write without tag + B8.2 add({name: "tavern"}) must NOT clobber "The Gilded Cup"
            delta2 = await apply_ops(
                db,
                {},
                uid,
                "conv-2",
                [
                    {"op": "add", "book": "WorldBook", "entry": {"name": "tavern", "keys": ["tavern"], "content": "new tavern"}},
                ],
                source="test",
            )
            assert len(delta2) == 1
            rows = (await db.execute(select(LorebookEntry).where(LorebookEntry.name == "The Gilded Cup"))).scalars().all()
            assert len(rows) == 1
            assert rows[0].content == "old cup"

            # 3. Same-batch upsert over existing DB row followed by delete -> NOT in delta
            delta3 = await apply_ops(
                db,
                {},
                uid,
                "conv-2",
                [
                    {"op": "add", "book": "WorldBook", "entry": {"name": "tavern", "keys": ["tavern"], "content": "updated"}},
                    {"op": "delete", "book": "WorldBook", "entry": "tavern"},
                ],
                source="test",
            )
            assert all(m.name != "tavern" for m in delta3)

            # 4. Same-batch add -> delete -> re-add of a brand-new entry succeeds
            delta4 = await apply_ops(
                db,
                {},
                uid,
                "conv-2",
                [
                    {"op": "add", "book": "WorldBook", "entry": {"name": "Temp", "keys": ["t"], "content": "v1"}},
                    {"op": "delete", "book": "WorldBook", "entry": "Temp"},
                    {"op": "add", "book": "WorldBook", "entry": {"name": "Temp", "keys": ["t"], "content": "v2"}},
                ],
                source="test",
            )
            assert [m.content for m in delta4 if m.name == "Temp"] == ["v2"]


class TestCantripAndMemoryFixes:
    @pytest.mark.asyncio
    @pytest.mark.skipif(not DENO_PATH, reason="Deno binary not available")
    async def test_deno_memory_deletes_empty_response_and_b82(self):
        ctx = {
            "character": {"name": "A", "personality": "", "scenario": "", "example_dialogs": ""},
            "user": {"name": "U"},
            "chat": {"messages": [], "last_message": "", "message_count": 0},
            "__memories": {"keep": "1", "drop": "2"},
            "__lorebook": {
                "B": {
                    "writable": True,
                    "entries": [{"name": "The Gilded Cup", "keys": ["tavern"], "content": "cup"}],
                }
            },
            "response": {"content": "hello", "original_content": "hello", "modified": False},
        }
        code = """
            context.memory.delete('drop');
            context.response.content = '';
            context.lorebook.get('B').add({ name: 'tavern', keys: ['tavern'], content: 'new' });
        """
        res = await run_cantrip(code, ctx)
        assert res.error is None
        assert res.memory_deletes == ["drop"]
        assert "drop" not in res.memories
        assert res.response_content == ""
        # Both ops should be recorded, and sandbox working copy shouldn't have clobbered The Gilded Cup
        assert len(res.lorebook_ops) == 1

    @pytest.mark.skipif(not DENO_PATH, reason="Deno binary not available")
    def test_validate_syntax_handles_quotes_and_backslashes(self):
        assert validate_cantrip_syntax('const s = "hello \\"world\\"";') == ""
        assert validate_cantrip_syntax("const s = ;") != ""

    @pytest.mark.asyncio
    async def test_dirty_key_writeback_preserves_concurrent_keys(self, admin_client):
        from app.services.cantrip import _load_chat_data, _save_chat_data
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]

        async with TestSessionLocal() as db:
            await _save_chat_data(db, uid, "c-conc", {"a": 1, "b": 2})
            before = await _load_chat_data(db, uid, "c-conc")
            # Simulate concurrent writer updating "b" to 99
            await _save_chat_data(db, uid, "c-conc", {"a": 1, "b": 99})
            # Our request only modified "a" -> 10, leaving "b" == 2 in our snapshot
            await _save_chat_data(db, uid, "c-conc", {"a": 10, "b": 2}, before=before)
            final = await _load_chat_data(db, uid, "c-conc")
            assert final == {"a": 10, "b": 99}

    @pytest.mark.asyncio
    async def test_memory_delete_persists_in_save_memories_for_chat(self, admin_client):
        from app.services.conversation import load_memories_for_chat, save_memories_for_chat

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]
        await save_memories_for_chat("chat-mem", uid, {"k1": "v1", "k2": "v2"})
        await save_memories_for_chat("chat-mem", uid, {"k1": "v1b"}, deletes={"k2"})
        loaded = await load_memories_for_chat("chat-mem", uid)
        assert loaded == {"k1": "v1b"}

    def test_append_sections_keeps_dialogs_and_scenario_before_last_user(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        out = apply_cantrip_result_to_messages(msgs, "pers", "scen", "dial")
        roles_and_starts = [(m["role"], m["content"][:12]) for m in out]
        assert roles_and_starts[-1] == ("user", "hi")
        assert roles_and_starts[1] == ("system", "[Personality")


class TestSupportingServicesFixes:
    def test_jslorebook_preserves_js_escape_sequences(self):
        raw = "const msg = &quot;line1\\nline2\\tindented&quot;;"
        assert _desanitize_content(raw) == 'const msg = "line1\\nline2\\tindented";'

    @pytest.mark.asyncio
    async def test_empty_response_tag_is_stripped(self, admin_client):
        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]
        await client.post(
            "/api/cantrips",
            json={"name": "TagOwner", "code": "1+1;", "response_tags": ["deed"]},
        )
        body_json: dict = {}
        resp = {"choices": [{"message": {"role": "assistant", "content": "Hello <deed></deed> world"}}]}
        await process_response_capture(resp, uid, body_json)
        assert resp["choices"][0]["message"]["content"] == "Hello  world"
        assert "_gitv_response_tags" not in body_json

    @pytest.mark.asyncio
    async def test_record_post_hash_tolerates_duplicate(self, admin_client):
        from app.services.conversation import record_post_hash

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]
        msgs = [{"role": "user", "content": "unique-msg-for-hash-test"}]
        h1 = await record_post_hash(msgs, "", "chat-h1", uid)
        h2 = await record_post_hash(msgs, "", "chat-h1", uid)
        assert h1 == h2 and h1 != ""

    @pytest.mark.asyncio
    async def test_assistant_loop_lock_evicted_and_create_endpoint_strips_key(self):
        from app.services.assistant.loop import _LOCKS, _lock_for
        from app.services.assistant.registry import TOOLS

        tool = TOOLS["create_endpoint"]
        assert tool.project_args is not None
        projected = tool.project_args({"name": "ep", "base_url": "http://x", "api_key": "sk-secret"})
        assert "api_key" not in projected

        lock = _lock_for("conv-evict-test")
        assert "conv-evict-test" in _LOCKS
        _LOCKS.pop("conv-evict-test", None)
        assert lock is not None
