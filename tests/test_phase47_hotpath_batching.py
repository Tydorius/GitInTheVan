"""Phase 47 (0.47.0): pre-emptive Deno warm-pool scaling and hot-path DB/context batching."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.cantrip_data import CantripData
from app.services import cantrip as cantrip_svc
from app.services import deno_runner
from app.services.cantrip import (
    _load_cantrip_data_batch,
    _save_cantrip_data_batch,
    process_cantrips,
    process_cantrips_post_driver,
)
from app.services.deno_runner import CantripResult
from app.services.pipeline_order import run_pre_forward_ordered
from tests.conftest import TestSessionLocal


def _make_cantrip(
    cid: str,
    name: str,
    *,
    tag: str = "",
    is_active: bool = True,
    execution_order: int = 10,
    code: str = "// noop",
    ephemeral: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid,
        name=name,
        tag=tag,
        is_active=is_active,
        is_public=False,
        user_id="user-1",
        execution_order=execution_order,
        timeout_ms=5000,
        code=code,
        lorebook_reads="[]",
        user_data_prefixes="[]",
        budget_weight=1.0,
        ephemeral=ephemeral,
        created_at=0,
    )


def _make_lorebook(
    lid: str,
    name: str,
    *,
    content: str = "LORE_TEXT",
    is_constant: bool = True,
    keys: str = '["dragon"]',
) -> SimpleNamespace:
    entry = SimpleNamespace(
        id=f"{lid}-e1",
        lorebook_id=lid,
        name="Entry 1",
        keys=keys,
        secondary_keys="[]",
        content=content,
        position="system_start",
        insertion_order=10,
        is_constant=is_constant,
        is_selective=False,
        is_disabled=False,
        character_limit=0,
    )
    return SimpleNamespace(
        id=lid,
        user_id="user-1",
        name=name,
        tag="",
        is_active=True,
        is_public=False,
        scan_depth=0,
        word_boundary_keys=False,
        budget_weight=1.0,
        created_at=0,
        entries=[entry],
    )


def _noop_result(**overrides) -> CantripResult:
    base = dict(
        personality="",
        scenario="",
        example_dialogs="",
        chat_data={},
        user_data={},
        cantrip_data={},
    )
    base.update(overrides)
    return CantripResult(**base)


# ============================================================================
# 1. Pre-emptive Pool Warm-Up (ensure_warm)
# ============================================================================


@pytest.mark.asyncio
class TestPreemptiveWarmPool:
    async def test_process_cantrips_calls_ensure_warm_before_db_loads(self):
        c1 = _make_cantrip("c-1", "Active1")
        c2 = _make_cantrip("c-2", "Active2")
        c_inactive = _make_cantrip("c-3", "Inactive", tag="missing-tag", is_active=False)

        call_order: list[str] = []
        mock_pool = MagicMock()
        mock_pool.ensure_warm.side_effect = lambda n: call_order.append(f"ensure_warm:{n}")

        async def track_load_chat(*_a, **_kw):
            call_order.append("load_chat_data")
            return {}

        async def track_load_user(*_a, **_kw):
            call_order.append("load_user_data")
            return {}

        async def track_load_batch(*_a, **_kw):
            call_order.append("load_cantrip_data_batch")
            return {"c-1": {}, "c-2": {}}

        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with (
            patch.object(deno_runner, "deno_pool", mock_pool, create=True),
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2, c_inactive])),
            patch("app.services.cantrip._load_chat_data", side_effect=track_load_chat),
            patch("app.services.cantrip._load_user_data", side_effect=track_load_user),
            patch("app.services.cantrip._load_cantrip_data_batch", side_effect=track_load_batch),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip.run_cantrip", AsyncMock(return_value=_noop_result())),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
        ):
            await process_cantrips(body_json, "user-1", {"x-conversation-id": "conv-1"})

        mock_pool.ensure_warm.assert_called_once_with(2)
        assert call_order[0] == "ensure_warm:2"
        assert "load_chat_data" in call_order[1:]
        assert "load_cantrip_data_batch" in call_order[1:]

    async def test_process_cantrips_post_driver_calls_ensure_warm_before_db_loads(self):
        c1 = _make_cantrip("c-1", "Post1")
        c2 = _make_cantrip("c-2", "Post2")
        c3 = _make_cantrip("c-3", "Post3")

        call_order: list[str] = []
        mock_pool = MagicMock()
        mock_pool.ensure_warm.side_effect = lambda n: call_order.append(f"ensure_warm:{n}")

        async def track_load_chat(*_a, **_kw):
            call_order.append("load_chat_data")
            return {}

        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with (
            patch.object(deno_runner, "deno_pool", mock_pool, create=True),
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2, c3])),
            patch("app.services.cantrip._load_chat_data", side_effect=track_load_chat),
            patch("app.services.cantrip._load_user_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_cantrip_data_batch", AsyncMock(return_value={"c-1": {}, "c-2": {}, "c-3": {}})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip.run_cantrip", AsyncMock(return_value=_noop_result())),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
        ):
            out = await process_cantrips_post_driver("hello", body_json, "user-1", {})

        assert out == "hello"
        mock_pool.ensure_warm.assert_called_once_with(3)
        assert call_order[:2] == ["ensure_warm:3", "load_chat_data"]

    async def test_run_pre_forward_ordered_calls_ensure_warm_before_db_loads(self):
        c1 = _make_cantrip("c-1", "Ordered1")
        c2 = _make_cantrip("c-2", "Ordered2")
        lb = _make_lorebook("lb-1", "Book1")

        call_order: list[str] = []
        mock_pool = MagicMock()
        mock_pool.ensure_warm.side_effect = lambda n: call_order.append(f"ensure_warm:{n}")

        async def track_load_lorebooks(*_a, **_kw):
            call_order.append("load_active_lorebooks")
            return [lb]

        async def track_load_user(*_a, **_kw):
            call_order.append("load_user_data")
            return {}

        plan = {
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": "c-1"},
                {"type": "lorebook", "id": "lb-1"},
                {"type": "cantrip", "id": "c-2"},
            ],
        }
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with (
            patch.object(deno_runner, "deno_pool", mock_pool, create=True),
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2])),
            patch("app.services.lorebook.load_active_lorebooks", side_effect=track_load_lorebooks),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_user_data", side_effect=track_load_user),
            patch("app.services.cantrip._load_cantrip_data_batch", AsyncMock(return_value={"c-1": {}, "c-2": {}})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip.run_cantrip", AsyncMock(return_value=_noop_result())),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
        ):
            await run_pre_forward_ordered(plan, body_json, "user-1", {})

        mock_pool.ensure_warm.assert_called_once_with(2)
        assert call_order[0] == "ensure_warm:2"
        assert "load_active_lorebooks" in call_order[1:]
        assert "load_user_data" in call_order[1:]


# ============================================================================
# 2. Batch CantripData Load & Save
# ============================================================================


@pytest.mark.asyncio
class TestCantripDataBatching:
    async def test_load_and_save_batch_roundtrip_create_update_delete_noop(self, setup_database):
        user_id = "batch-user-1"
        async with TestSessionLocal() as db:
            # Empty list returns {} with zero queries
            assert await _load_cantrip_data_batch(db, user_id, []) == {}

            # Seed initial rows for two cantrips
            await _save_cantrip_data_batch(
                db,
                user_id,
                {
                    "cid-1": ({"count": 1, "keep": "ok", "drop_me": "bye"}, {}, []),
                    "cid-2": ({"name": "alpha", "reborn": "v1"}, {}, []),
                },
            )

            loaded = await _load_cantrip_data_batch(db, user_id, ["cid-1", "cid-2", "cid-empty"])
            assert loaded == {
                "cid-1": {"count": 1, "keep": "ok", "drop_me": "bye"},
                "cid-2": {"name": "alpha", "reborn": "v1"},
                "cid-empty": {},
            }

            # Apply update + create + delete + delete-then-re-set + untouched key
            await _save_cantrip_data_batch(
                db,
                user_id,
                {
                    "cid-1": (
                        {"count": 2, "keep": "ok", "added": [1, 2]},
                        {"count": 1, "keep": "ok", "drop_me": "bye"},
                        ["drop_me"],
                    ),
                    "cid-2": (
                        {"name": "alpha", "reborn": "v2"},
                        {"name": "alpha", "reborn": "v1"},
                        ["reborn"],  # deleted then re-set: final dict wins
                    ),
                },
            )

            loaded2 = await _load_cantrip_data_batch(db, user_id, ["cid-1", "cid-2"])
            assert loaded2 == {
                "cid-1": {"count": 2, "keep": "ok", "added": [1, 2]},
                "cid-2": {"name": "alpha", "reborn": "v2"},
            }

            # Verify raw DB rows for cid-1 no longer contain drop_me
            rows = (
                await db.execute(
                    select(CantripData).where(
                        CantripData.user_id == user_id,
                        CantripData.cantrip_id == "cid-1",
                    )
                )
            ).scalars().all()
            assert {r.key for r in rows} == {"count", "keep", "added"}

    async def test_save_batch_skips_db_when_all_entries_untouched(self):
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_db.commit = AsyncMock()

        await _save_cantrip_data_batch(
            mock_db,
            "user-1",
            {
                "cid-1": ({"a": 1}, {"a": 1}, []),
                "cid-2": ({"b": "x"}, {"b": "x"}, ["b"]),  # b still present -> not deleted
            },
        )

        mock_db.execute.assert_not_awaited()
        mock_db.commit.assert_not_awaited()

    async def test_process_cantrips_batches_cantrip_data_and_preserves_error_isolation(self, setup_database):
        user_id = "batch-iso-user"
        c1 = _make_cantrip("cid-ok", "First", execution_order=1)
        c2 = _make_cantrip("cid-boom", "Second", execution_order=2)
        c3 = _make_cantrip("cid-third", "Third", execution_order=3)

        # Pre-seed cid-ok with an existing key to be deleted and one to be updated
        async with TestSessionLocal() as db:
            db.add(CantripData(user_id=user_id, cantrip_id="cid-ok", key="old", value_json=json.dumps("gone")))
            db.add(CantripData(user_id=user_id, cantrip_id="cid-ok", key="counter", value_json=json.dumps(1)))
            await db.commit()

        call_num = 0

        async def fake_run_cantrip(**kwargs):
            nonlocal call_num
            call_num += 1
            cd = dict(kwargs["cantrip_data"])
            if call_num == 1:
                assert cd == {"old": "gone", "counter": 1}
                cd["counter"] = 2
                cd.pop("old")
                return _noop_result(cantrip_data=cd, cantrip_data_deletes=["old"])
            if call_num == 2:
                raise RuntimeError("boom in cantrip 2")
            cd["ran"] = True
            return _noop_result(cantrip_data=cd)

        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2, c3])),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip),
            patch("app.services.cantrip._load_cantrip_data", AsyncMock()) as mock_single_load,
            patch("app.services.cantrip._save_cantrip_data", AsyncMock()) as mock_single_save,
        ):
            await process_cantrips(body_json, user_id, {})

        # Per-cantrip load/save were never used
        mock_single_load.assert_not_awaited()
        mock_single_save.assert_not_awaited()

        # Both c1 (before error) and c3 (after error) persisted cleanly
        async with TestSessionLocal() as db:
            loaded = await _load_cantrip_data_batch(db, user_id, ["cid-ok", "cid-boom", "cid-third"])
        assert loaded == {
            "cid-ok": {"counter": 2},
            "cid-boom": {},
            "cid-third": {"ran": True},
        }


# ============================================================================
# 3. pipeline_order.py: before= diff & build_context() dirty tracking
# ============================================================================


@pytest.mark.asyncio
class TestPipelineOrderHotPath:
    async def test_untouched_chat_and_user_data_skip_db_write(self):
        c1 = _make_cantrip("c-1", "Noop")
        plan = {"enabled": True, "items": [{"type": "cantrip", "id": "c-1"}]}
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello"}],
        }

        real_save_kv = cantrip_svc._save_kv_store
        db_commits_for_kv: list[str] = []

        async def spy_save_kv(db, model, filters, row_kwargs, data, deletes=None, before=None):
            assert before is not None, f"before= must be passed for {model.__name__}"
            if before == data and not [k for k in (deletes or ()) if k not in data]:
                # Confirm _save_kv_store returns early without hitting DB
                mock_db = MagicMock()
                mock_db.execute = AsyncMock()
                mock_db.commit = AsyncMock()
                await real_save_kv(mock_db, model, filters, row_kwargs, data, deletes=deletes, before=before)
                mock_db.execute.assert_not_awaited()
                mock_db.commit.assert_not_awaited()
                return
            db_commits_for_kv.append(model.__name__)

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1])),
            patch("app.services.lorebook.load_active_lorebooks", AsyncMock(return_value=[])),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={"turn": 1})),
            patch("app.services.cantrip._load_user_data", AsyncMock(return_value={"pref": "dark"})),
            patch("app.services.cantrip._load_cantrip_data_batch", AsyncMock(return_value={"c-1": {}})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip._save_kv_store", side_effect=spy_save_kv),
            patch(
                "app.services.cantrip.run_cantrip",
                AsyncMock(return_value=_noop_result(chat_data={"turn": 1}, user_data={"pref": "dark"})),
            ),
        ):
            await run_pre_forward_ordered(
                plan, body_json, "user-1", {"x-conversation-id": "conv-1"}
            )

        assert db_commits_for_kv == []

    async def test_skips_redundant_build_context_for_consecutive_non_mutating_cantrips(self):
        c1 = _make_cantrip("c-1", "C1", execution_order=1)
        c2 = _make_cantrip("c-2", "C2", execution_order=2)
        c3 = _make_cantrip("c-3", "C3", execution_order=3)
        lb_nomatch = _make_lorebook("lb-no", "NoMatch", is_constant=False, keys='["unmatched_kw"]')

        plan = {
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": "c-1"},
                {"type": "lorebook", "id": "lb-no"},
                {"type": "cantrip", "id": "c-2"},
                {"type": "cantrip", "id": "c-3"},
            ],
        }
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "plain message"}],
        }

        from app.services.cantrip_context import build_context as real_build_context

        build_calls = 0

        def counting_build_context(*args, **kwargs):
            nonlocal build_calls
            build_calls += 1
            return real_build_context(*args, **kwargs)

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2, c3])),
            patch("app.services.lorebook.load_active_lorebooks", AsyncMock(return_value=[lb_nomatch])),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_user_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_cantrip_data_batch", AsyncMock(return_value={"c-1": {}, "c-2": {}, "c-3": {}})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip_context.build_context", side_effect=counting_build_context),
            patch("app.services.cantrip.run_cantrip", AsyncMock(return_value=_noop_result())),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
        ):
            await run_pre_forward_ordered(plan, body_json, "user-1", {})

        # Only the initial pass-start build_context() call!
        assert build_calls == 1

    async def test_refreshes_context_chat_after_lorebook_prompt_strips_and_history_ops(self):
        c1 = _make_cantrip("c-1", "Stripper", execution_order=1)
        lb = _make_lorebook("lb-1", "LoreBook", content="SECRET_LORE_ENTRY", is_constant=True)
        c2 = _make_cantrip("c-2", "HistoryTrimmer", execution_order=2)
        c3 = _make_cantrip("c-3", "Observer", execution_order=3)

        plan = {
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": "c-1"},
                {"type": "lorebook", "id": "lb-1"},
                {"type": "cantrip", "id": "c-2"},
                {"type": "cantrip", "id": "c-3"},
            ],
        }
        body_json = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "OLD_TURN_TO_DROP"},
                {"role": "assistant", "content": "OLD_REPLY"},
                {"role": "user", "content": "Hello STRIP_ME world"},
            ],
        }

        from app.services.cantrip_context import build_context as real_build_context

        build_calls = 0
        seen_by_cantrip: dict[str, list[str]] = {}

        def counting_build_context(*args, **kwargs):
            nonlocal build_calls
            build_calls += 1
            return real_build_context(*args, **kwargs)

        step_idx = 0

        async def fake_run_cantrip(**kwargs):
            nonlocal step_idx
            step_idx += 1
            msgs = [m["message"] for m in kwargs["context"]["chat"]["last_messages"]]
            if step_idx == 1:
                seen_by_cantrip["c-1"] = msgs
                return _noop_result(
                    prompt_strips=[{"pattern": "STRIP_ME ", "scope": "all"}]
                )
            if step_idx == 2:
                seen_by_cantrip["c-2"] = msgs
                return _noop_result(
                    history_ops=[{"op": "keep_recent", "count": 1}]
                )
            seen_by_cantrip["c-3"] = msgs
            return _noop_result()

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[c1, c2, c3])),
            patch("app.services.lorebook.load_active_lorebooks", AsyncMock(return_value=[lb])),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_user_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_cantrip_data_batch", AsyncMock(return_value={"c-1": {}, "c-2": {}, "c-3": {}})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, set(), ""))),
            patch("app.services.cantrip_context.build_context", side_effect=counting_build_context),
            patch("app.services.cantrip.run_cantrip", side_effect=fake_run_cantrip),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
        ):
            await run_pre_forward_ordered(plan, body_json, "user-1", {})

        # 1 initial + 1 before c-2 (after c-1 strips & lb-1 inject) + 1 before c-3 (after c-2 history_ops)
        assert build_calls == 3

        # c-1 saw original unstripped message
        assert any("STRIP_ME" in m for m in seen_by_cantrip["c-1"])
        # c-2 saw stripped message AND injected lorebook entry (in system message / messages)
        assert all("STRIP_ME" not in m for m in seen_by_cantrip["c-2"])
        assert any("Hello world" in m for m in seen_by_cantrip["c-2"])
        assert any("SECRET_LORE_ENTRY" in m["content"] for m in body_json["messages"])
        # c-3 saw trimmed history (OLD_TURN_TO_DROP dropped)
        assert all("OLD_TURN_TO_DROP" not in m for m in seen_by_cantrip["c-3"])
        assert any("Hello world" in m for m in seen_by_cantrip["c-3"])
