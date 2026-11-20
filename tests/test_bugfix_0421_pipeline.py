"""Regression tests for the 0.42.1 pipeline bugfix batch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.models.verification import VerificationRule
from app.services.llm_params import ParamDef, resolve
from app.services.map_pipeline import run_map_pipeline
from app.services.pipeline_order import run_pre_forward_ordered
from app.services.proxy import _forward_streaming_with_memory, _forward_with_failover
from app.services.routing import FailoverEndpoint
from app.services.skills import inject_skills
from app.services.summarization import maybe_summarize, summarize_messages
from app.services.verification import (
    VerificationJudgment,
    _apply_resubmission_strategy,
    check_response,
)


def _make_cantrip(cid: str, name: str, tag: str = "", is_active: bool = True):
    return SimpleNamespace(
        id=cid,
        name=name,
        tag=tag,
        is_active=is_active,
        is_public=False,
        user_id="user-1",
        execution_order=10,
        code="// noop",
        budget_weight=1.0,
        run_driver_callable=False,
    )


def _make_lorebook(lid: str, name: str, content: str = "Lore entry"):
    entry = SimpleNamespace(
        id=f"{lid}-e1",
        lorebook_id=lid,
        name="Entry 1",
        keys='["dragon"]',
        secondary_keys="[]",
        content=content,
        position="system_start",
        insertion_order=10,
        is_constant=True,
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
        entries=[entry],
    )


class TestPipelineOrderBugfixes:
    async def test_card_mutation_preserves_live_lorebook_and_cantrip_injections(self):
        """Passing stale pre-loop `messages` to apply_cantrip_result_to_messages
        used to wipe out lorebooks and cantrip injections."""
        lb = _make_lorebook("lb-1", "Book1", content="LORE_CONTENT")
        cantrip = _make_cantrip("c-1", "Mutator")
        plan = {
            "enabled": True,
            "items": [
                {"type": "lorebook", "id": "lb-1"},
                {"type": "cantrip", "id": "c-1"},
            ],
        }
        body_json = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "<personality>Old</personality>"},
                {"role": "user", "content": "Hello dragon"},
            ],
            "_gitv_replay": True,
        }

        async def fake_execute_step(obj, **kwargs):
            ctx = kwargs["context"]
            ctx["character"]["personality"] = "NewPersonality"
            kwargs["state"]["accumulated_memories"]["k"] = "v"
            return SimpleNamespace(success=True, prompt_strips=[]), []

        with (
            patch("app.services.cantrip._load_active_cantrips", AsyncMock(return_value=[cantrip])),
            patch("app.services.lorebook.load_active_lorebooks", AsyncMock(return_value=[lb])),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._load_user_data", AsyncMock(return_value={})),
            patch("app.services.cantrip._prepare_lorebook_seeding", AsyncMock(return_value=({}, {}, ""))),
            patch("app.services.cantrip._save_user_data", AsyncMock()),
            patch("app.services.cantrip._execute_cantrip_step", side_effect=fake_execute_step),
            patch("app.services.conversation.load_memories_for_chat", AsyncMock(return_value={})),
            patch("app.services.conversation.save_memories_for_chat", AsyncMock()) as mock_save_mem,
            patch(
                "app.services.cantrip.apply_cantrip_result_to_messages",
                side_effect=lambda msgs, p, s, e: [
                    {"role": "system", "content": f"{msgs[0]['content']}\n{p}"},
                    *msgs[1:],
                ],
            ),
        ):
            await run_pre_forward_ordered(
                plan, body_json, "user-1", {}, internal_chat_id="chat-1"
            )

        sys_content = body_json["messages"][0]["content"]
        assert "LORE_CONTENT" in sys_content
        assert "NewPersonality" in sys_content
        mock_save_mem.assert_not_awaited()


class TestFailoverIsolation:
    async def test_failover_isolates_auth_and_candidate0_params(self):
        c0 = FailoverEndpoint(
            endpoint_id="ep-0",
            endpoint_name="Primary",
            base_url="https://ep0.test",
            api_key="secret-0",
            model="model-0",
            parameters=[ParamDef(name="temperature", type="float", value=0.3)],
        )
        c1 = FailoverEndpoint(
            endpoint_id="ep-1",
            endpoint_name="BackupKeyless",
            base_url="https://ep1.test",
            api_key="",
            model="model-1",
            parameters=[],
        )
        body_json = {
            "model": "model-0",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.3,
            "top_p": 0.9,
            "_gitv_user_llm_params": {"top_p": 0.9},
            "_gitv_llm_params": {"top_p": 0.9, "temperature": 0.3},
        }
        body_bytes = json.dumps({
            "model": "model-0",
            "messages": body_json["messages"],
            "temperature": 0.3,
            "top_p": 0.9,
        }).encode()
        calls = []

        async def fake_forward(method, url, headers, body, timeout, **kwargs):
            calls.append((url, dict(headers), json.loads(body.decode())))
            if len(calls) == 1:
                return {"error": "down"}, 500
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("app.services.proxy._do_forward", side_effect=fake_forward):
            data, status = await _forward_with_failover(
                body_json,
                [c0, c1],
                "POST",
                "/v1/chat/completions",
                {"Content-Type": "application/json", "Authorization": "Bearer secret-0"},
                httpx.Timeout(10.0),
                body_bytes,
            )

        assert status == 200
        assert len(calls) == 2
        assert "Authorization" not in calls[1][1]
        assert "authorization" not in {k.lower() for k in calls[1][1]}
        assert calls[1][2].get("top_p") == 0.9
        assert "temperature" not in calls[1][2]


class TestStreamingParityAndMemoryOff:
    async def test_streaming_bypass_decode_and_memory_off_memstore_strip(self):
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "_gitv_chat_id": "chat-1",
            "_gitv_command_overrides": {"memory": False},
            "_gitv_bypass_method": "base64",
            "_gitv_pending_notices": ["[OOC: GitV: Notice]"],
        }
        upstream_resp = {
            "choices": [{"message": {"content": '<memstore key="k">v</memstore>Decoded'}}]
        }

        with (
            patch("app.services.proxy._do_forward", AsyncMock(return_value=(upstream_resp, 200))),
            patch(
                "app.services.proxy._load_ux_settings",
                AsyncMock(return_value={"streaming_post_processing": False}),
            ),
            patch("app.services.response_tags.process_response_capture", AsyncMock(side_effect=lambda d, *_: d)) as mock_capture,
            patch("app.services.proxy._extract_response_memories", AsyncMock()) as mock_extract,
            patch("app.services.proxy._record_conversation_anchor", AsyncMock()) as mock_anchor,
            patch("app.services.bypass.apply_bypass_decode", side_effect=lambda d, m: d) as mock_bypass,
            patch("app.services.metrics.flush_metrics", AsyncMock()),
        ):
            resp = await _forward_streaming_with_memory(
                "POST",
                "https://ep.test/v1/chat/completions",
                {},
                b"{}",
                httpx.Timeout(10.0),
                "user-1",
                body_json,
            )

        assert resp.status_code == 200
        mock_capture.assert_awaited_once()
        mock_extract.assert_not_awaited()
        mock_anchor.assert_awaited_once()
        mock_bypass.assert_called_once()
        content = upstream_resp["choices"][0]["message"]["content"]
        assert "<memstore" not in content
        assert "[OOC: GitV: Notice]" in content


class TestMapPipelineBugfixes:
    async def test_all_stages_skipped_returns_fallback(self):
        stage = SimpleNamespace(
            id="s-1",
            map_id="m-1",
            stage_order=0,
            name="Skipped",
            condition_json='{"key": "flag", "op": "eq", "value": true}',
        )
        map_obj = SimpleNamespace(
            id="m-1", user_id="user-1", name="TestMap", stages=[stage], global_llm_instructions=""
        )
        body_json = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.services.map_pipeline.async_session", return_value=mock_session),
            patch("app.services.cantrip._load_chat_data", AsyncMock(return_value={"flag": False})),
            patch("app.services.map_pipeline.evaluate_stage_condition", return_value=(False, "skipped")),
            patch("app.services.admin.get_caps", AsyncMock(return_value={"max_map_stages": 10, "max_driver_callable_turns": 5})),
        ):
            result = await run_map_pipeline(
                body_json, "user-1", {}, map_obj, httpx.Timeout(10.0)
            )

        assert result["choices"][0]["message"]["content"] == ""

    async def test_stage_verification_retry_regenerates_output(self):
        stage = SimpleNamespace(
            id="s-1",
            map_id="m-1",
            stage_order=0,
            name="Draft",
            condition_json="",
            base_prompt_mode="default",
            system_prompt="",
            user_prompt_prefix="",
            user_prompt_suffix="",
            driver_callable_turns=0,
            verification_enabled=True,
            verification_max_retries=1,
            verification_instructions="Check",
            output_mode="persist",
            resources=[],
        )
        map_obj = SimpleNamespace(
            id="m-1", user_id="user-1", name="TestMap", stages=[stage], global_llm_instructions=""
        )
        ep = Endpoint(
            id="ep-1",
            user_id="user-1",
            name="EP",
            base_url="https://ep.test",
            api_key="k",
            default_model="gpt-4",
            enabled=True,
        )
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            # Debug on so the regeneration capture is recorded.
            "_gitv_debug": {"stages": []},
        }

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        stage_calls = [
            ({"choices": [{"message": {"content": "bad draft"}}]}, 200),
            ({"choices": [{"message": {"content": "good draft"}}]}, 200),
        ]
        judge_calls = [
            ({"choices": [{"message": {"content": '{"violation": true, "reason": "bad"}'}}]}, 200),
            ({"choices": [{"message": {"content": '{"violation": false, "reason": ""}'}}]}, 200),
        ]

        with (
            patch("app.services.map_pipeline.async_session", return_value=mock_session),
            patch("app.services.map_pipeline._resolve_stage_endpoint", AsyncMock(return_value=([ep], "gpt-4"))),
            patch("app.services.map_pipeline._resolve_verification_endpoint", AsyncMock(return_value=(ep, "gpt-4"))),
            patch("app.services.map_pipeline.stage_parameters", AsyncMock(return_value=({}, {}))),
            patch("app.services.map_pipeline._stage_verification_parameters", AsyncMock(return_value={})),
            patch("app.services.map_pipeline._forward_stage_llm", AsyncMock(side_effect=stage_calls)) as mock_stage_llm,
            patch("app.services.proxy._do_forward", AsyncMock(side_effect=judge_calls)),
            patch("app.services.admin.get_caps", AsyncMock(return_value={"max_map_stages": 10, "max_driver_callable_turns": 5, "max_verification_retries": 3})),
            patch("app.services.map_pipeline._inject_stage_instructions", AsyncMock(side_effect=lambda b, *_: b)),
            patch("app.services.map_pipeline._inject_stage_lorebooks", AsyncMock(side_effect=lambda b, *_: b)),
            patch("app.services.map_pipeline._inject_stage_skills", AsyncMock(side_effect=lambda b, *_: b)),
        ):
            result = await run_map_pipeline(
                body_json, "user-1", {}, map_obj, httpx.Timeout(10.0)
            )

        assert mock_stage_llm.await_count == 2
        assert result["choices"][0]["message"]["content"] == "good draft"

        # Rule 16: the regeneration is its own response-side step and must
        # appear in the Debug timeline, or the trace shows only the rejected
        # output while the pipeline proceeds with the corrected one.
        regen_captures = [
            s for s in body_json["_gitv_debug"]["stages"]
            if s["name"] == "map_stage_regenerated"
        ]
        assert regen_captures, body_json["_gitv_debug"]["stages"]
        assert regen_captures[0]["content_after"] == "good draft"
        assert regen_captures[0]["metadata"]["violation_reason"] == "bad"

    async def test_stage_empty_choices_response_does_not_crash(self):
        """A 200 stage response with an empty ``choices`` list must not
        IndexError the pipeline (the same class as the verification and
        summarizer empty-choices fixes; this extraction site was missed)."""
        stage = SimpleNamespace(
            id="s-1",
            map_id="m-1",
            stage_order=0,
            name="Draft",
            condition_json="",
            base_prompt_mode="default",
            system_prompt="",
            user_prompt_prefix="",
            user_prompt_suffix="",
            driver_callable_turns=0,
            verification_enabled=False,
            output_mode="persist",
            resources=[],
        )
        map_obj = SimpleNamespace(
            id="m-1", user_id="user-1", name="TestMap", stages=[stage], global_llm_instructions=""
        )
        ep = Endpoint(
            id="ep-1",
            user_id="user-1",
            name="EP",
            base_url="https://ep.test",
            api_key="k",
            default_model="gpt-4",
            enabled=True,
        )
        body_json = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("app.services.map_pipeline.async_session", return_value=mock_session),
            patch("app.services.map_pipeline._resolve_stage_endpoint", AsyncMock(return_value=([ep], "gpt-4"))),
            patch("app.services.map_pipeline.stage_parameters", AsyncMock(return_value=({}, {}))),
            patch("app.services.admin.get_caps", AsyncMock(return_value={"max_map_stages": 10, "max_driver_callable_turns": 5, "max_verification_retries": 3})),
            patch("app.services.map_pipeline._inject_stage_instructions", AsyncMock(side_effect=lambda b, *_: b)),
            patch("app.services.map_pipeline._inject_stage_lorebooks", AsyncMock(side_effect=lambda b, *_: b)),
            patch("app.services.map_pipeline._inject_stage_skills", AsyncMock(side_effect=lambda b, *_: b)),
            patch("app.services.map_pipeline._forward_stage_llm", AsyncMock(return_value=({"choices": []}, 200))) as mock_stage_llm,
        ):
            result = await run_map_pipeline(
                body_json, "user-1", {}, map_obj, httpx.Timeout(10.0)
            )

        assert mock_stage_llm.await_count == 1
        # The raw upstream response passes through untouched; nothing raised.
        assert result["choices"] == []


class TestVerificationAndSummarizationFixes:
    def test_rewrite_strategy_uses_violating_draft_not_previous_turn(self):
        body_json = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "turn 1"},
                {"role": "assistant", "content": "previous turn reply"},
                {"role": "user", "content": "turn 2"},
            ],
        }
        violations = [VerificationJudgment(violation=True, reason="bad word", severity="high", rule_name="r1")]
        retry_body = _apply_resubmission_strategy(
            body_json, violations, "rewrite", current_content="violating draft"
        )
        assistant_msgs = [m["content"] for m in retry_body["messages"] if m["role"] == "assistant"]
        assert assistant_msgs[-1] == "violating draft"

    async def test_verification_and_summarization_handle_empty_choices(self):
        ep = Endpoint(
            id="ep-1", user_id="user-1", name="EP", base_url="https://ep.test", api_key="k", enabled=True
        )
        rule = VerificationRule(
            id="r-1", user_id="user-1", name="R1", prompt="check", is_active=True, max_retries=1, execution_order=1
        )
        with patch("app.services.proxy._do_forward", AsyncMock(return_value=({"choices": []}, 200))):
            vres = await check_response("hello", [rule], ep, "gpt-4")
            assert len(vres.errors) == 1

            body_json: dict = {}
            sres = await summarize_messages(
                [{"role": "user", "content": "hello"}],
                ep,
                "gpt-4",
                "summarize",
                existing_summary="old",
                body_json=body_json,
            )
            assert sres == "old"
            assert body_json.get("_gitv_summarizer_degraded") is True

    async def test_summarization_degraded_does_not_poison_cache(self):
        ep = Endpoint(
            id="ep-1", user_id="user-1", name="EP", base_url="https://ep.test", api_key="k", enabled=True
        )
        us = UserSettings(
            user_id="user-1",
            summarization_enabled=True,
            summarization_token_threshold=1,
            summarization_keep_recent=1,
            summarization_endpoint_id="ep-1",
            summarization_model="gpt-4",
        )
        existing = SimpleNamespace(boundary_hash="old-hash", summary="old summary")
        body_json = {
            "messages": [
                {"role": "user", "content": "msg 1 " * 20},
                {"role": "assistant", "content": "msg 2 " * 20},
                {"role": "user", "content": "msg 3"},
            ]
        }

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def fake_summarize(*args, **kwargs):
            kwargs["body_json"]["_gitv_summarizer_degraded"] = True
            return "old summary"

        with (
            patch("app.services.summarization.async_session", return_value=mock_session),
            patch("app.services.summarization.load_summarization_config", AsyncMock(return_value=(us, ep))),
            patch("app.services.summarization.resolve_memory_rule", AsyncMock(return_value=None)),
            patch("app.services.summarization.load_summary", AsyncMock(return_value=existing)),
            patch("app.services.summarization.summarize_messages", side_effect=fake_summarize),
            patch("app.services.summarization.save_summary", AsyncMock()) as mock_save,
        ):
            await maybe_summarize(body_json, "user-1", "chat-1")

        mock_save.assert_not_awaited()


class TestLlmParamsAndSkills:
    def test_option_matching_bool_and_float(self):
        resolved = resolve([
            (
                "test",
                [
                    ParamDef(name="flag", type="boolean", value=True, options=("true", "false")),
                    ParamDef(name="temp", type="float", value="1", options=("1", "0.5")),
                ],
            )
        ])
        assert resolved.values["flag"] is True
        assert resolved.values["temp"] == 1.0

    def test_inject_skills_handles_multimodal_list_content(self):
        msgs = [{"role": "system", "content": [{"type": "text", "text": "base"}]}]
        out = inject_skills(msgs, ["be concise"])
        assert isinstance(out[0]["content"], list)
        assert len(out[0]["content"]) == 2
        assert "<skills>" in out[0]["content"][1]["text"]
