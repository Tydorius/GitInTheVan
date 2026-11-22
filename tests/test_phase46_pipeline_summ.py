"""Tests for Phase 46 (0.46.0): Map history_window + stage strips/history_ops,
incremental + block-aligned summarization, and Gemini thinking_level mapping."""
from __future__ import annotations

import copy
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.services.cantrip as cantrip_mod
import app.services.map_pipeline as map_pipeline_mod
import app.services.summarization as summ_mod
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.services.map_pipeline import _apply_history_window, run_map_pipeline
from app.services.summarization import compute_boundary_hash, maybe_summarize
from app.services.upstream import _do_forward_litellm


def _make_endpoint(
    ep_id: str = "ep-1",
    name: str = "Test Endpoint",
    provider: str = "",
) -> Endpoint:
    return Endpoint(
        id=ep_id,
        user_id="user-1",
        name=name,
        base_url="https://llm.test",
        api_key="sk-test",
        api_base_path="/v1",
        default_model="test-model",
        provider=provider,
        enabled=True,
    )


def _make_stage(
    *,
    stage_id: str = "stage-1",
    name: str = "Stage 1",
    stage_order: int = 0,
    base_prompt_mode: str = "full",
    history_window: int = 0,
    output_mode: str = "replace",
    resources: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=stage_id,
        name=name,
        stage_order=stage_order,
        base_prompt_mode=base_prompt_mode,
        history_window=history_window,
        output_mode=output_mode,
        output_tag="",
        condition_json="",
        system_instructions="",
        endpoint_id="ep-1",
        endpoint_tag="",
        model_override="",
        parameters_json="{}",
        verification_enabled=False,
        driver_callable_turns=0,
        resources=resources or [],
    )


def _make_map(stages: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        id="map-1",
        name="Test Map",
        stages=stages,
        global_llm_instructions="",
        final_verification_enabled=False,
    )


def _make_dialogue(count: int, content_len: int = 80) -> list[dict]:
    msgs = [{"role": "system", "content": "System persona."}]
    pad = "x" * content_len
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"msg-{i} {pad}"})
    return msgs


# ============================================================================
# N8: Map Stage history_window + Stage Cantrip Strips / History Ops
# ============================================================================


class TestMapHistoryWindowHelper:
    def test_zero_keeps_all(self):
        msgs = _make_dialogue(6, content_len=5)
        assert _apply_history_window(msgs, 0) == msgs

    def test_trims_non_system_preserves_system(self):
        msgs = _make_dialogue(6, content_len=5)
        trimmed = _apply_history_window(msgs, 2)
        assert [m["role"] for m in trimmed] == ["system", "user", "assistant"]
        assert trimmed[0]["content"] == "System persona."
        assert trimmed[1]["content"].startswith("msg-4")
        assert trimmed[2]["content"].startswith("msg-5")

    def test_preserves_newest_user_when_tail_is_assistant(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
        ]
        trimmed = _apply_history_window(msgs, 2)
        contents = [m["content"] for m in trimmed]
        assert contents == ["sys", "u1", "a2"]

    def test_preserves_newest_user_with_window_one(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u0"},
            {"role": "assistant", "content": "a0"},
        ]
        trimmed = _apply_history_window(msgs, 1)
        assert [m["content"] for m in trimmed] == ["sys", "u0"]


class TestMapPipelineHistoryWindowAndCantripOps:
    @pytest.mark.asyncio
    async def test_history_window_full_and_blank_modes_and_hash_anchor_intact(self):
        ep = _make_endpoint()
        stage_full = _make_stage(
            stage_id="st-1",
            name="Window Full",
            stage_order=0,
            base_prompt_mode="full",
            history_window=2,
            output_mode="discard",
        )
        stage_blank = _make_stage(
            stage_id="st-2",
            name="Window Blank",
            stage_order=1,
            base_prompt_mode="blank",
            history_window=2,
            output_mode="replace",
        )
        map_obj = _make_map([stage_full, stage_blank])

        initial_messages = _make_dialogue(6, content_len=5)
        hash_anchor = copy.deepcopy(initial_messages)
        body_json = {
            "model": "test-model",
            "messages": copy.deepcopy(initial_messages),
            "_gitv_messages_for_hash": hash_anchor,
            "_gitv_debug": {"stages": [], "run": {"cantrips": []}},
        }

        forwarded_messages: list[list[dict]] = []

        async def fake_forward(stage_body, endpoints, model, timeout, **kwargs):
            forwarded_messages.append(copy.deepcopy(stage_body["messages"]))
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, 200

        with (
            patch("app.services.admin.get_caps", new=AsyncMock(return_value={"max_map_stages": 5, "max_driver_callable_turns": 3})),
            patch.object(map_pipeline_mod, "_resolve_stage_endpoint", new=AsyncMock(return_value=([ep], "test-model"))),
            patch.object(map_pipeline_mod, "stage_parameters", new=AsyncMock(return_value=({}, {}))),
            patch.object(map_pipeline_mod, "_forward_stage_llm", side_effect=fake_forward),
        ):
            result = await run_map_pipeline(
                body_json, "user-1", {}, map_obj, httpx.Timeout(30.0)
            )

        assert result["choices"][0]["message"]["content"] == "ok"
        assert len(forwarded_messages) == 2

        # Stage 1 (full + history_window=2): system + newest 2 non-system
        assert [m["role"] for m in forwarded_messages[0]] == ["system", "user", "assistant"]
        assert forwarded_messages[0][1]["content"].startswith("msg-4")
        assert forwarded_messages[0][2]["content"].startswith("msg-5")

        # Stage 2 (blank + history_window=2): no system + newest 2 non-system
        assert [m["role"] for m in forwarded_messages[1]] == ["user", "assistant"]
        assert forwarded_messages[1][0]["content"].startswith("msg-4")
        assert forwarded_messages[1][1]["content"].startswith("msg-5")

        # Hash anchor was never mutated
        assert body_json["_gitv_messages_for_hash"] == initial_messages

        # Debug captures include history_window stage + metadata
        stage_names = [s["name"] for s in body_json["_gitv_debug"]["stages"]]
        assert "map_stage_history_window" in stage_names
        map_stage_captures = [
            s for s in body_json["_gitv_debug"]["stages"] if s["name"] == "map_stage"
        ]
        assert all(c["metadata"]["history_window"] == 2 for c in map_stage_captures)

    @pytest.mark.asyncio
    async def test_stage_cantrip_prompt_strips_and_history_ops_applied(self):
        ep = _make_endpoint()
        stage = _make_stage(
            stage_id="st-cantrip",
            name="Cantrip Stage",
            resources=[
                SimpleNamespace(resource_id="c-1", resource_type="cantrip", sticky=False),
            ],
        )
        map_obj = _make_map([stage])
        body_json = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "sys SECRET_TOKEN"},
                {"role": "user", "content": "hello SECRET_TOKEN"},
            ],
            "_gitv_messages_for_hash": [
                {"role": "system", "content": "sys SECRET_TOKEN"},
                {"role": "user", "content": "hello SECRET_TOKEN"},
            ],
        }

        async def fake_process_cantrips(bj, *args, **kwargs):
            bj["_gitv_prompt_strips"] = [{"pattern": "SECRET_TOKEN", "scope": "all"}]
            bj["_gitv_history_ops"] = [{"op": "drop", "index": 0}]
            return bj

        history_ops_seen: list[list[dict]] = []

        def fake_apply_history_ops(bj, ops=None):
            queued = bj.pop("_gitv_history_ops", [])
            history_ops_seen.append(queued)
            bj["messages"].append({"role": "user", "content": "from-history-op"})
            return bj

        forwarded: list[list[dict]] = []

        async def fake_forward(stage_body, endpoints, model, timeout, **kwargs):
            forwarded.append(copy.deepcopy(stage_body["messages"]))
            return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}, 200

        with (
            patch("app.services.admin.get_caps", new=AsyncMock(return_value={"max_map_stages": 5, "max_driver_callable_turns": 3})),
            patch.object(map_pipeline_mod, "_resolve_stage_endpoint", new=AsyncMock(return_value=([ep], "test-model"))),
            patch.object(map_pipeline_mod, "stage_parameters", new=AsyncMock(return_value=({}, {}))),
            patch.object(cantrip_mod, "process_cantrips", side_effect=fake_process_cantrips),
            patch.object(cantrip_mod, "apply_history_ops", side_effect=fake_apply_history_ops, create=True),
            patch.object(map_pipeline_mod, "_forward_stage_llm", side_effect=fake_forward),
        ):
            await run_map_pipeline(body_json, "user-1", {}, map_obj, httpx.Timeout(30.0))

        assert len(forwarded) == 1
        # Prompt strip removed SECRET_TOKEN from both messages
        assert "SECRET_TOKEN" not in forwarded[0][0]["content"]
        assert "SECRET_TOKEN" not in forwarded[0][1]["content"]
        # History ops were applied
        assert history_ops_seen == [[{"op": "drop", "index": 0}]]
        assert forwarded[0][-1] == {"role": "user", "content": "from-history-op"}
        assert "_gitv_prompt_strips" not in body_json
        assert "_gitv_history_ops" not in body_json
        # Hash anchor untouched
        assert "SECRET_TOKEN" in body_json["_gitv_messages_for_hash"][1]["content"]


# ============================================================================
# N9: Incremental + Block-Aligned Conversation Summarization
# ============================================================================


def _make_user_settings(
    *,
    threshold: int = 10,
    keep_recent: int = 2,
    block_size: int = 0,
) -> UserSettings:
    us = UserSettings(
        user_id="user-1",
        summarization_enabled=True,
        summarization_endpoint_id="ep-sum",
        summarization_model="sum-model",
        summarization_token_threshold=threshold,
        summarization_keep_recent=keep_recent,
        summarization_prompt="Summarize concisely.",
    )
    setattr(us, "summarization_block_size", block_size)
    return us


class TestIncrementalAndBlockAlignedSummarization:
    @pytest.mark.asyncio
    async def test_incremental_delta_when_prefix_matches(self):
        ep = _make_endpoint(ep_id="ep-sum", name="Summarizer")
        us = _make_user_settings(threshold=10, keep_recent=2, block_size=0)
        messages = _make_dialogue(8, content_len=40)
        # First 4 dialogue messages (indices 1..4) were already summarized
        prefix_msgs = messages[1:5]
        existing = ConversationSummary(
            user_id="user-1",
            internal_chat_id="chat-inc",
            summary="Old summary of turns 0..3.",
            boundary_hash=compute_boundary_hash(prefix_msgs),
            message_count=4,
            token_estimate=100,
        )

        calls: list[dict] = []

        async def fake_summarize(msgs, endpoint, model, prompt, existing_summary="", **kwargs):
            calls.append({"messages": msgs, "existing_summary": existing_summary})
            return "Updated summary of turns 0..5."

        saved: list[dict] = []

        async def fake_save(db, uid, cid, summary, boundary_hash, msg_count, tok_est):
            saved.append(
                {"summary": summary, "boundary_hash": boundary_hash, "message_count": msg_count}
            )

        body_json = {"messages": copy.deepcopy(messages)}
        with (
            patch.object(summ_mod, "load_summarization_config", new=AsyncMock(return_value=(us, ep))),
            patch.object(summ_mod, "resolve_memory_rule", new=AsyncMock(return_value=None)),
            patch.object(summ_mod, "load_summary", new=AsyncMock(return_value=existing)),
            patch.object(summ_mod, "summarize_messages", side_effect=fake_summarize),
            patch.object(summ_mod, "save_summary", side_effect=fake_save),
        ):
            out = await maybe_summarize(body_json, "user-1", "chat-inc")

        assert len(calls) == 1
        # Only delta messages (msg-4 and msg-5) sent, with existing.summary!
        assert [m["content"][:5] for m in calls[0]["messages"]] == ["msg-4", "msg-5"]
        assert calls[0]["existing_summary"] == "Old summary of turns 0..3."
        assert len(saved) == 1
        assert saved[0]["message_count"] == 6
        assert saved[0]["boundary_hash"] == compute_boundary_hash(messages[1:7])
        assert any("Updated summary of turns 0..5." in m["content"] for m in out["messages"])

    @pytest.mark.asyncio
    async def test_prefix_diverged_falls_back_to_full_with_empty_existing_summary(self):
        ep = _make_endpoint(ep_id="ep-sum", name="Summarizer")
        us = _make_user_settings(threshold=10, keep_recent=2, block_size=0)
        messages = _make_dialogue(8, content_len=40)
        # Existing summary was computed on different earlier history (edited turn)
        existing = ConversationSummary(
            user_id="user-1",
            internal_chat_id="chat-div",
            summary="Stale branch summary.",
            boundary_hash="deadbeef000000000000000000000000",
            message_count=4,
            token_estimate=100,
        )

        calls: list[dict] = []

        async def fake_summarize(msgs, endpoint, model, prompt, existing_summary="", **kwargs):
            calls.append({"messages": msgs, "existing_summary": existing_summary})
            return "Fresh full summary."

        body_json = {"messages": copy.deepcopy(messages)}
        with (
            patch.object(summ_mod, "load_summarization_config", new=AsyncMock(return_value=(us, ep))),
            patch.object(summ_mod, "resolve_memory_rule", new=AsyncMock(return_value=None)),
            patch.object(summ_mod, "load_summary", new=AsyncMock(return_value=existing)),
            patch.object(summ_mod, "summarize_messages", side_effect=fake_summarize),
            patch.object(summ_mod, "save_summary", new=AsyncMock()),
        ):
            await maybe_summarize(body_json, "user-1", "chat-div")

        assert len(calls) == 1
        # Full to_compress (6 messages: msg-0..msg-5) and empty existing_summary
        assert len(calls[0]["messages"]) == 6
        assert calls[0]["messages"][0]["content"].startswith("msg-0")
        assert calls[0]["existing_summary"] == ""

    @pytest.mark.asyncio
    async def test_block_aligned_summarization_lifecycle(self):
        ep = _make_endpoint(ep_id="ep-sum", name="Summarizer")
        us = _make_user_settings(threshold=10, keep_recent=2, block_size=4)

        stored: dict[str, ConversationSummary] = {}
        calls: list[dict] = []

        async def fake_load(db, uid, cid):
            return stored.get(cid)

        async def fake_save(db, uid, cid, summary, boundary_hash, msg_count, tok_est):
            stored[cid] = ConversationSummary(
                user_id=uid,
                internal_chat_id=cid,
                summary=summary,
                boundary_hash=boundary_hash,
                message_count=msg_count,
                token_estimate=tok_est,
            )

        async def fake_summarize(msgs, endpoint, model, prompt, existing_summary="", **kwargs):
            calls.append({"messages": msgs, "existing_summary": existing_summary})
            return f"Summary#{len(calls)}"

        with (
            patch.object(summ_mod, "load_summarization_config", new=AsyncMock(return_value=(us, ep))),
            patch.object(summ_mod, "resolve_memory_rule", new=AsyncMock(return_value=None)),
            patch.object(summ_mod, "load_summary", side_effect=fake_load),
            patch.object(summ_mod, "save_summary", side_effect=fake_save),
            patch.object(summ_mod, "summarize_messages", side_effect=fake_summarize),
        ):
            # 3 eligible (5 dialogue - 2 keep_recent): aligned_count = 0 -> skip
            msgs_3 = _make_dialogue(5, content_len=40)
            out_3 = await maybe_summarize({"messages": copy.deepcopy(msgs_3)}, "user-1", "chat-blk")
            assert len(calls) == 0
            assert out_3["messages"] == msgs_3

            # 4 eligible (6 dialogue - 2 keep_recent): compresses exactly 4 (msg-0..msg-3)
            msgs_4 = _make_dialogue(6, content_len=40)
            out_4 = await maybe_summarize({"messages": copy.deepcopy(msgs_4)}, "user-1", "chat-blk")
            assert len(calls) == 1
            assert [m["content"][:5] for m in calls[0]["messages"]] == [
                "msg-0", "msg-1", "msg-2", "msg-3",
            ]
            assert calls[0]["existing_summary"] == ""
            # system + summary + 2 recent = 4 messages
            assert len(out_4["messages"]) == 4

            # 5, 6, 7 eligible: boundary stays frozen at 4, cached summary reused (no new LLM calls)
            for eligible in (5, 6, 7):
                msgs_n = _make_dialogue(eligible + 2, content_len=40)
                out_n = await maybe_summarize(
                    {"messages": copy.deepcopy(msgs_n)}, "user-1", "chat-blk"
                )
                assert len(calls) == 1
                # system + summary + (eligible + 2 - 4) uncompressed messages
                assert len(out_n["messages"]) == 1 + 1 + (eligible + 2 - 4)

            # 8 eligible (10 dialogue - 2 keep_recent): incremental delta of 4 (msg-4..msg-7)
            msgs_8 = _make_dialogue(10, content_len=40)
            out_8 = await maybe_summarize({"messages": copy.deepcopy(msgs_8)}, "user-1", "chat-blk")
            assert len(calls) == 2
            assert [m["content"][:5] for m in calls[1]["messages"]] == [
                "msg-4", "msg-5", "msg-6", "msg-7",
            ]
            assert calls[1]["existing_summary"] == "Summary#1"
            assert stored["chat-blk"].message_count == 8
            # system + summary + 2 recent = 4 messages
            assert len(out_8["messages"]) == 4


# ============================================================================
# G3: upstream.py _do_forward_litellm thinking_level mapping
# ============================================================================


class TestLitellmThinkingLevel:
    @pytest.mark.asyncio
    async def test_configured_thinking_level_maps_to_reasoning_effort_on_gemini(self):
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

        fake_litellm = types.ModuleType("litellm")
        fake_litellm.acompletion = fake_acompletion
        fake_litellm.exceptions = SimpleNamespace()

        body = json.dumps({
            "model": "gemini-3-pro",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()

        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            data, status = await _do_forward_litellm(
                body,
                provider="gemini",
                base_url="",
                api_key="sk-gem",
                timeout=httpx.Timeout(30.0),
                configured={"thinking_level": "MEDIUM"},
            )

        assert status == 200
        assert captured["reasoning_effort"] == "medium"
        assert "thinking_level" not in captured
        assert "thinking_level" not in (captured.get("extra_body") or {})

    @pytest.mark.asyncio
    async def test_body_thinking_level_maps_on_vertex_ai(self):
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

        fake_litellm = types.ModuleType("litellm")
        fake_litellm.acompletion = fake_acompletion
        fake_litellm.exceptions = SimpleNamespace()

        body = json.dumps({
            "model": "gemini-3-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "thinking_level": "High",
        }).encode()

        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            _, status = await _do_forward_litellm(
                body,
                provider="vertex_ai",
                base_url="",
                api_key="sk-vtx",
                timeout=httpx.Timeout(30.0),
            )

        assert status == 200
        assert captured["reasoning_effort"] == "high"
        assert "extra_body" not in captured

    @pytest.mark.asyncio
    async def test_explicit_reasoning_effort_takes_precedence(self):
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

        fake_litellm = types.ModuleType("litellm")
        fake_litellm.acompletion = fake_acompletion
        fake_litellm.exceptions = SimpleNamespace()

        body = json.dumps({
            "model": "gemini-3-pro",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "low",
        }).encode()

        with patch.dict(sys.modules, {"litellm": fake_litellm}):
            _, status = await _do_forward_litellm(
                body,
                provider="gemini",
                base_url="",
                api_key="sk-gem",
                timeout=httpx.Timeout(30.0),
                configured={"thinking_level": "high"},
            )

        assert status == 200
        assert captured["reasoning_effort"] == "low"
        assert "thinking_level" not in (captured.get("extra_body") or {})
