from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.cantrip import (
    _execute_cantrip_step,
    _load_cantrip_data,
    _load_chat_data,
    _load_user_data,
    _save_cantrip_data,
    _save_chat_data,
    _save_user_data,
    process_cantrips_post_driver,
)
from app.services.deno_runner import CantripResult
from app.services.lorebook import book_entries_to_dicts
from app.services.messages import (
    extract_text,
    inject_into_system_message,
    strip_from_messages,
)
from tests.conftest import TestSessionLocal


class TestMessagesHelpers:
    def test_extract_text_variants(self):
        assert extract_text("hello") == "hello"
        assert extract_text(None) == ""
        assert extract_text("") == ""
        assert extract_text([]) == ""
        assert extract_text(42) == "42"
        multimodal = [
            {"type": "text", "text": "Look at this"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "text", "text": "carefully"},
        ]
        assert extract_text(multimodal) == "Look at this carefully"

    def test_inject_into_system_message_string_and_missing(self):
        msgs = [{"role": "system", "content": "Base"}, {"role": "user", "content": "Hi"}]
        out = inject_into_system_message(msgs, "Extra")
        assert out[0]["content"] == "Base\n\nExtra"
        assert msgs[0]["content"] == "Base"

        no_sys = [{"role": "user", "content": "Hi"}]
        out2 = inject_into_system_message(no_sys, "Extra")
        assert out2[0] == {"role": "system", "content": "Extra"}
        assert len(no_sys) == 1

        assert inject_into_system_message(msgs, "") is msgs

    def test_inject_into_system_message_multimodal(self):
        parts = [
            {"type": "text", "text": "Base"},
            {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
        ]
        msgs = [{"role": "system", "content": parts}, {"role": "user", "content": "Hi"}]
        out = inject_into_system_message(msgs, "Injected")
        assert isinstance(out[0]["content"], list)
        assert out[0]["content"][-1] == {"type": "text", "text": "\n\nInjected"}
        assert len(parts) == 2

    def test_strip_from_messages_multimodal(self):
        msgs = [
            {"role": "user", "content": "hello [TAG] world"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "keep [TAG]"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            },
        ]
        cleaned = strip_from_messages(msgs, lambda s: s.replace(" [TAG]", ""))
        assert cleaned[0]["content"] == "hello world"
        assert cleaned[1]["content"][0]["text"] == "keep"
        assert cleaned[1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"


class TestLorebookEntryDicts:
    def test_book_entries_to_dicts(self):
        book = SimpleNamespace(id="lb-1", name="World")
        entry = SimpleNamespace(
            id="e-1",
            name="Town",
            keys='["town"]',
            secondary_keys="[]",
            content="A quiet town.",
            position="before_last_message",
            insertion_order=10,
            is_constant=False,
            is_selective=False,
            is_disabled=False,
            character_limit=500,
        )
        dicts = book_entries_to_dicts([entry], book=book)
        assert dicts == [
            {
                "lorebook_id": "lb-1",
                "lorebook_name": "World",
                "entry_id": "e-1",
                "name": "Town",
                "keys": '["town"]',
                "secondary_keys": "[]",
                "content": "A quiet town.",
                "position": "before_last_message",
                "insertion_order": 10,
                "is_constant": False,
                "is_selective": False,
                "is_disabled": False,
                "character_limit": 500,
            }
        ]


@pytest.mark.asyncio
class TestKvStorePersistence:
    async def test_dirty_writeback_and_deletes_across_stores(self):
        uid = "kv-test-user"
        conv = "kv-test-conv"
        cid = "kv-test-cantrip"

        async with TestSessionLocal() as db:
            await _save_chat_data(db, uid, conv, {"a": 1, "b": 2, "c": 3})
            before = await _load_chat_data(db, uid, conv)
            assert before == {"a": 1, "b": 2, "c": 3}

            # Modify 'a', delete 'b', delete-then-re-set 'c'
            await _save_chat_data(
                db, uid, conv, {"a": 10, "c": 30}, deletes={"b", "c"}, before=before
            )
            assert await _load_chat_data(db, uid, conv) == {"a": 10, "c": 30}

            await _save_user_data(db, uid, {"theme": "dark", "temp": "x"})
            u_before = await _load_user_data(db, uid)
            await _save_user_data(
                db, uid, {"theme": "light"}, deletes={"temp"}, before=u_before
            )
            assert await _load_user_data(db, uid) == {"theme": "light"}

            await _save_cantrip_data(db, uid, cid, {"count": 1, "old": 2})
            c_before = await _load_cantrip_data(db, uid, cid)
            await _save_cantrip_data(
                db, uid, cid, {"count": 2}, deletes={"old"}, before=c_before
            )
            assert await _load_cantrip_data(db, uid, cid) == {"count": 2}


@pytest.mark.asyncio
class TestUserDataSeedingOptimization:
    async def test_execute_cantrip_step_skips_user_data_when_unused(self):
        seen_user_data: list[dict] = []

        async def fake_run_cantrip(**kwargs):
            seen_user_data.append(dict(kwargs["user_data"]))
            return CantripResult(
                personality="P",
                scenario="S",
                example_dialogs="E",
                chat_data=dict(kwargs["chat_data"]),
                user_data=dict(kwargs["user_data"]),
                cantrip_data=dict(kwargs["cantrip_data"]),
            )

        cantrip_no_ud = SimpleNamespace(
            id="c1",
            name="no-ud",
            code="context.character.personality = 'P';",
            timeout_ms=5000,
            tag="",
            execution_order=0,
            lorebook_reads="[]",
            budget_weight=0.0,
            ephemeral=True,
        )
        cantrip_uses_ud = SimpleNamespace(
            id="c2",
            name="uses-ud",
            code="context.user_data.set('seen', true);",
            timeout_ms=5000,
            tag="",
            execution_order=1,
            lorebook_reads="[]",
            budget_weight=0.0,
            ephemeral=True,
        )

        state = {
            "chat_data": {},
            "user_data": {"big_blob": "x" * 100},
            "accumulated_memories": {},
            "memory_deletes": set(),
            "lorebook_seed": {},
            "lorebook_always": set(),
        }
        context = {
            "character": {"personality": "", "scenario": "", "example_dialogs": ""},
        }
        body_json: dict = {"messages": [{"role": "user", "content": "hi"}]}

        async with TestSessionLocal() as db:
            with patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip):
                await _execute_cantrip_step(
                    cantrip_no_ud,
                    db=db,
                    body_json=body_json,
                    user_id="u1",
                    request_headers={},
                    position="pre_driver",
                    context=context,
                    state=state,
                    binding_conv_id="",
                    internal_chat_id="",
                    isolated=True,
                    store_seeds=None,
                    passenger_handler=None,
                    budget_allocations=None,
                    cantrips_for_seeding=[],
                )
                assert seen_user_data[0] == {}
                assert state["user_data"] == {"big_blob": "x" * 100}

                await _execute_cantrip_step(
                    cantrip_uses_ud,
                    db=db,
                    body_json=body_json,
                    user_id="u1",
                    request_headers={},
                    position="pre_driver",
                    context=context,
                    state=state,
                    binding_conv_id="",
                    internal_chat_id="",
                    isolated=True,
                    store_seeds=None,
                    passenger_handler=None,
                    budget_allocations=None,
                    cantrips_for_seeding=[],
                )
                assert seen_user_data[1] == {"big_blob": "x" * 100}

    async def test_unnamed_user_data_writes_still_propagate(self, caplog):
        """The seeding gate is a substring scan of the source; a script that
        reaches the store without the literal name (introspection, a
        runtime-built key) must not have its writes silently dropped."""
        import logging

        async def fake_run_cantrip(**kwargs):
            assert kwargs["user_data"] == {}
            return CantripResult(
                personality="P",
                scenario="S",
                example_dialogs="E",
                chat_data=dict(kwargs["chat_data"]),
                user_data={"dyn": 1},
                user_data_deletes={"old"},
                cantrip_data=dict(kwargs["cantrip_data"]),
            )

        # No contiguous "user_data" substring anywhere in the source.
        cantrip_dynamic = SimpleNamespace(
            id="c3",
            name="dynamic-ud",
            code="const k = 'user' + '_data';",
            timeout_ms=5000,
            tag="",
            execution_order=0,
            lorebook_reads="[]",
            budget_weight=0.0,
            ephemeral=True,
        )

        state = {
            "chat_data": {},
            "user_data": {"big_blob": "x" * 100},
            "accumulated_memories": {},
            "memory_deletes": set(),
            "lorebook_seed": {},
            "lorebook_always": set(),
        }
        context = {
            "character": {"personality": "", "scenario": "", "example_dialogs": ""},
        }
        body_json: dict = {"messages": [{"role": "user", "content": "hi"}]}

        with caplog.at_level(logging.WARNING, logger="app.services.cantrip"):
            async with TestSessionLocal() as db:
                with patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip):
                    await _execute_cantrip_step(
                        cantrip_dynamic,
                        db=db,
                        body_json=body_json,
                        user_id="u1",
                        request_headers={},
                        position="pre_driver",
                        context=context,
                        state=state,
                        binding_conv_id="",
                        internal_chat_id="",
                        isolated=True,
                        store_seeds=None,
                        passenger_handler=None,
                        budget_allocations=None,
                        cantrips_for_seeding=[],
                    )

        # Seeded empty (the optimization), but the writes propagated and a
        # warning named the cantrip.
        assert state["user_data"] == {"dyn": 1}
        assert "old" in state.get("user_data_deletes", set())
        assert any("without naming it" in r.getMessage() for r in caplog.records)

    async def test_post_driver_skips_user_data_when_unused(self):
        seen_user_data: list[dict] = []

        async def fake_run_cantrip(**kwargs):
            seen_user_data.append(dict(kwargs["user_data"]))
            return CantripResult(
                response_content="modified",
                chat_data=dict(kwargs["chat_data"]),
                user_data=dict(kwargs["user_data"]),
                cantrip_data=dict(kwargs["cantrip_data"]),
            )

        cantrip_no_ud = SimpleNamespace(
            id="c-post",
            name="post-no-ud",
            code="context.response.content = 'modified';",
            timeout_ms=5000,
            tag="",
            is_active=True,
            is_public=False,
            user_id="u1",
            execution_order=0,
            lorebook_reads="[]",
            budget_weight=0.0,
            ephemeral=True,
        )
        store_seeds = {"chat_data": {}, "user_data": {"big": "payload"}, "cantrip_data": {}}
        body_json = {"messages": [{"role": "user", "content": "hi"}]}

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[cantrip_no_ud])),
            patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip),
        ):
            out = await process_cantrips_post_driver(
                "original",
                body_json,
                "u1",
                {},
                store_seeds=store_seeds,
            )

        assert out == "modified"
        assert seen_user_data == [{}]
        assert store_seeds["user_data"] == {"big": "payload"}
