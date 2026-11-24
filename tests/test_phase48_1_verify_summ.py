"""Tests for 0.48.1 (Phase B): regex-only verification without a judge endpoint,
summarization degradation / empty-delta hardening, and thinking_level validation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.services.llm_params import ParameterDef, validate_params
from app.services.summarization import compute_boundary_hash, maybe_summarize
from app.services.verification import check_response


class TestRegexOnlyVerificationWithoutJudge:
    @pytest.mark.asyncio
    async def test_regex_rule_passes_cleanly_without_judge_endpoint(self):
        rule = SimpleNamespace(
            id="r1",
            name="No Slop",
            prompt="Reject slop",
            regex_pattern=r"\bshivers down (?:his|her|their) spine\b",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("The knight drew his sword quietly.", [rule], None, "")
        assert res.approved is True
        assert res.has_violations is False
        assert res.errors == []
        assert len(res.judgments) == 1
        assert res.judgments[0].errored is False
        assert "Regex pre-check passed" in res.judgments[0].reason

    @pytest.mark.asyncio
    async def test_regex_rule_rejects_without_judge_endpoint(self):
        rule = SimpleNamespace(
            id="r1",
            name="No Slop",
            prompt="Reject slop",
            regex_pattern=r"\bshivers down (?:his|her|their) spine\b",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("It sent shivers down his spine.", [rule], None, "")
        assert res.approved is False
        assert res.has_violations is True
        assert len(res.violations) == 1
        assert "Regex pre-check matched" in res.violations[0].reason

    @pytest.mark.asyncio
    async def test_non_regex_rule_without_judge_endpoint_still_records_error(self):
        rule = SimpleNamespace(
            id="r2",
            name="LLM Only Rule",
            prompt="Check tone",
            regex_pattern="",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("Some text.", [rule], None, "")
        assert res.approved is True
        assert len(res.errors) == 1
        assert res.errors[0].errored is True


class TestSummarizationDegradationAndEmptyDelta:
    @pytest.mark.asyncio
    async def test_degraded_incremental_preserves_newly_aged_messages(self):
        msgs = [
            {"role": "system", "content": "System card"},
            {"role": "user", "content": "Msg 1 " + "a" * 200},
            {"role": "assistant", "content": "Msg 2 " + "b" * 200},
            {"role": "user", "content": "Msg 3 newly aged " + "c" * 200},
            {"role": "assistant", "content": "Msg 4 newly aged " + "d" * 200},
            {"role": "user", "content": "Msg 5 recent"},
            {"role": "assistant", "content": "Msg 6 recent"},
        ]
        prior_prefix = msgs[1:3]
        existing = SimpleNamespace(
            summary="Summary of msgs 1-2",
            boundary_hash=compute_boundary_hash(prior_prefix),
            message_count=2,
        )
        user_settings = SimpleNamespace(
            summarization_enabled=True,
            summarization_token_threshold=10,
            summarization_keep_recent=2,
            summarization_block_size=0,
            summarization_model="mock-model",
            summarization_prompt="Summarize",
            summarization_parameters_json="[]",
        )
        endpoint = SimpleNamespace(
            id="ep1", name="Mock", base_url="http://mock", api_base_path="",
            api_key="", provider="", parameters_json="[]",
        )

        async def fake_summarize(messages_for_llm, *args, body_json=None, **kwargs):
            if body_json is not None:
                body_json["_gitv_summarizer_degraded"] = True
            return existing.summary

        body_json = {"messages": list(msgs)}
        with (
            patch("app.services.summarization.load_summarization_config", AsyncMock(return_value=(user_settings, endpoint))),
            patch("app.services.summarization.resolve_memory_rule", AsyncMock(return_value=None)),
            patch("app.services.summarization.load_summary", AsyncMock(return_value=existing)),
            patch("app.services.summarization.summarize_messages", side_effect=fake_summarize),
            patch("app.services.summarization.save_summary", AsyncMock()) as mock_save,
        ):
            out = await maybe_summarize(body_json, "u1", "chat1")

        # Never saved a promoted boundary on degradation
        mock_save.assert_not_awaited()
        # Newly aged messages 3 & 4 must NOT be dropped under stale summary!
        contents = [m["content"] for m in out["messages"]]
        assert any("Summary of msgs 1-2" in c for c in contents)
        assert any("Msg 3 newly aged" in c for c in contents)
        assert any("Msg 4 newly aged" in c for c in contents)
        assert any("Msg 5 recent" in c for c in contents)
        # Covered messages 1 & 2 remain compressed under the prior summary
        assert not any("Msg 1 " in c for c in contents)

    @pytest.mark.asyncio
    async def test_empty_delta_does_not_promote_stored_boundary(self):
        msgs = [
            {"role": "system", "content": "System card"},
            {"role": "user", "content": "Msg 1 " + "a" * 200},
            {"role": "assistant", "content": "Msg 2 " + "b" * 200},
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "Msg 5 recent"},
            {"role": "assistant", "content": "Msg 6 recent"},
        ]
        prior_prefix = msgs[1:3]
        existing = SimpleNamespace(
            summary="Summary of msgs 1-2",
            boundary_hash=compute_boundary_hash(prior_prefix),
            message_count=2,
        )
        user_settings = SimpleNamespace(
            summarization_enabled=True,
            summarization_token_threshold=10,
            summarization_keep_recent=2,
            summarization_block_size=0,
            summarization_model="mock-model",
            summarization_prompt="Summarize",
            summarization_parameters_json="[]",
        )
        endpoint = SimpleNamespace(
            id="ep1", name="Mock", base_url="http://mock", api_base_path="",
            api_key="", provider="", parameters_json="[]",
        )

        body_json = {"messages": list(msgs)}
        with (
            patch("app.services.summarization.load_summarization_config", AsyncMock(return_value=(user_settings, endpoint))),
            patch("app.services.summarization.resolve_memory_rule", AsyncMock(return_value=None)),
            patch("app.services.summarization.load_summary", AsyncMock(return_value=existing)),
            patch("app.services.summarization.save_summary", AsyncMock()) as mock_save,
        ):
            out = await maybe_summarize(body_json, "u1", "chat1")

        # Empty delta must not promote stored boundary_hash / message_count
        mock_save.assert_not_awaited()
        contents = [m["content"] for m in out["messages"]]
        assert any("Summary of msgs 1-2" in c for c in contents)


class TestThinkingLevelValidation:
    @pytest.mark.parametrize("val", ["minimal", "low", "medium", "high", "xhigh", "disable", "none", "HIGH"])
    def test_valid_thinking_levels_accepted(self, val):
        p = ParameterDef(name="thinking_level", type="string", value=val)
        assert p.value == val

    def test_blank_thinking_level_allowed_when_not_required(self):
        p = ParameterDef(name="thinking_level", type="string", value="")
        assert p.value == ""

    def test_typo_thinking_level_rejected_with_validation_error(self):
        with pytest.raises(ValidationError, match="thinking_level"):
            ParameterDef(name="thinking_level", type="string", value="meduim")

    def test_validate_params_rejects_invalid_thinking_level(self):
        with pytest.raises(ValidationError, match="thinking_level"):
            validate_params([{"name": "thinking_level", "type": "string", "value": "turbo"}])
