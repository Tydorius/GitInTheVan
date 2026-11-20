"""Tests for 0.43.0 pipeline refactor: upstream.py, response_pipeline.py, and proxy.py parity."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import proxy as proxy_mod
from app.services.llm_params import ParamDef
from app.services.response_pipeline import finalize_response
from app.services.routing import FailoverEndpoint
from app.services.upstream import extract_response_text, forward_llm_chain


class TestExtractResponseText:
    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not-a-dict",
            123,
            [],
            {},
            {"choices": None},
            {"choices": []},
            {"choices": [None]},
            {"choices": ["bad"]},
            {"choices": [{}]},
            {"choices": [{"message": None}]},
            {"choices": [{"message": "bad"}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": None}}]},
        ],
    )
    def test_handles_missing_empty_and_malformed_shapes(self, payload):
        assert extract_response_text(payload) == ""

    def test_extracts_valid_content(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
        assert extract_response_text(payload) == "Hello!"


class TestForwardLlmChain:
    @pytest.mark.asyncio
    async def test_failover_resolves_per_candidate_url_headers_params_and_records_calls(self):
        c0 = FailoverEndpoint(
            endpoint_id="ep-0",
            endpoint_name="Primary",
            base_url="https://ep0.test",
            api_key="key-0",
            api_base_path="/custom",
            provider="",
            model="model-0",
            parameters=[ParamDef(name="temperature", type="float", value=0.2)],
            model_parameters={},
        )
        c1 = FailoverEndpoint(
            endpoint_id="ep-1",
            endpoint_name="Fallback",
            base_url="https://ep1.test",
            api_key="",
            api_base_path="",
            provider="openai",
            model="model-1",
            parameters=[ParamDef(name="top_k", type="integer", value=40)],
            model_parameters={},
        )
        body_json = {
            "model": "model-0",
            "messages": [{"role": "user", "content": "hi"}],
            "_gitv_user_llm_params": {"top_p": 0.9},
            "_gitv_llm_params": {"top_p": 0.9, "temperature": 0.2},
        }
        body_bytes = json.dumps({
            "model": "model-0",
            "messages": body_json["messages"],
            "temperature": 0.2,
            "top_p": 0.9,
        }).encode()

        calls: list[dict] = []
        recorded: list[dict] = []

        async def fake_forward(method, url, headers, body, timeout, **kwargs):
            calls.append({
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body.decode()),
                **kwargs,
            })
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        def fake_record(*args, **kwargs):
            recorded.append(kwargs)

        with (
            patch("app.services.proxy._do_forward", side_effect=fake_forward),
            patch("app.services.debug_metrics.record_llm_call", side_effect=fake_record),
        ):
            data, status = await forward_llm_chain(
                body_json,
                [c0, c1],
                "POST",
                "/v1/chat/completions",
                {"Content-Type": "application/json", "Authorization": "Bearer secret"},
                httpx.Timeout(10.0),
                body_bytes,
            )

        assert status == 200
        assert extract_response_text(data) == "ok"
        assert len(calls) == 2

        # Candidate 0: custom api_base_path and Bearer key-0
        assert calls[0]["url"] == "https://ep0.test/custom/chat/completions"
        assert calls[0]["headers"]["Authorization"] == "Bearer key-0"

        # Candidate 1: keyless -> no Authorization header, fresh params resolved
        assert calls[1]["url"] == "https://ep1.test/v1/chat/completions"
        assert "authorization" not in {k.lower() for k in calls[1]["headers"]}
        assert calls[1]["provider"] == "openai"
        assert calls[1]["body"]["model"] == "model-1"
        assert calls[1]["body"]["top_p"] == 0.9
        assert calls[1]["body"]["top_k"] == 40
        assert "temperature" not in calls[1]["body"]

        # record_llm_call invoked for both exception (status 0) and success (status 200)
        assert len(recorded) == 2
        assert recorded[0]["status_code"] == 0
        assert "boom" in recorded[0]["error"]
        assert recorded[0]["failover_attempt"] == 0
        assert recorded[1]["status_code"] == 200
        assert recorded[1]["failover_attempt"] == 1

    @pytest.mark.asyncio
    async def test_empty_chain_returns_503(self):
        data, status = await forward_llm_chain({}, [])
        assert status == 503
        assert "error" in data


class TestFinalizeResponse:
    @pytest.mark.asyncio
    async def test_memory_off_and_bypass_decode_parity(self):
        body_json = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hi"}],
            "_gitv_chat_id": "chat-1",
            "_gitv_command_overrides": {"memory": False},
            "_gitv_bypass_method": "space_separation",
            "_gitv_pending_notices": ["[OOC: GitV: Notice]"],
            "_gitv_debug": {"run": {}},
        }
        response_data = {
            "choices": [{"message": {"content": '<memstore key="k">v</memstore>H\u200bi'}}],
            "_gitv_forbidden_summary": "leak",
        }

        with (
            patch("app.services.proxy._apply_post_driver_processing", AsyncMock(side_effect=lambda d, *_: d)) as mock_pd,
            patch("app.services.proxy._apply_post_navigator_processing", AsyncMock(side_effect=lambda d, *_: d)) as mock_pn,
            patch("app.services.response_tags.process_response_capture", AsyncMock(side_effect=lambda d, *_: d)) as mock_cap,
            patch("app.services.proxy._extract_response_memories", AsyncMock()) as mock_extract,
            patch("app.services.proxy._record_conversation_anchor", AsyncMock()) as mock_anchor,
            patch("app.services.proxy._save_debug_exchange", AsyncMock()) as mock_dbg,
            patch("app.services.metrics.flush_metrics", AsyncMock()) as mock_flush,
        ):
            out = await finalize_response(
                response_data,
                200,
                body_json,
                "user-1",
                {},
                run_post_driver=False,
                run_verification=False,
                run_post_navigator=False,
            )

        mock_pd.assert_not_awaited()
        mock_pn.assert_not_awaited()
        mock_cap.assert_awaited_once()
        mock_extract.assert_not_awaited()
        mock_anchor.assert_awaited_once()
        mock_dbg.assert_awaited_once()
        mock_flush.assert_awaited_once()
        assert "_gitv_forbidden_summary" not in out
        text = extract_response_text(out)
        assert "<memstore" not in text
        assert "\u200b" not in text
        assert "Hi" in text
        assert "[OOC: GitV: Notice]" in text

    @pytest.mark.asyncio
    async def test_non_200_skips_transforms_but_flushes_debug_and_metrics(self):
        body_json = {"_gitv_debug": {"run": {}}, "_gitv_pending_notices": ["notice"]}
        response_data = {"error": {"message": "bad"}}

        with (
            patch("app.services.proxy._apply_post_driver_processing", AsyncMock()) as mock_pd,
            patch("app.services.proxy._save_debug_exchange", AsyncMock()) as mock_dbg,
            patch("app.services.metrics.flush_metrics", AsyncMock()) as mock_flush,
        ):
            out = await finalize_response(response_data, 502, body_json, "user-1")

        mock_pd.assert_not_awaited()
        mock_dbg.assert_awaited_once()
        mock_flush.assert_awaited_once()
        assert out == {"error": {"message": "bad"}}

    def test_proxy_reexports_backward_compatible_symbols(self):
        expected = [
            "_do_forward",
            "_do_forward_litellm",
            "_build_upstream_url",
            "auth_headers",
            "_StreamAccumulator",
            "_convert_to_sse",
            "_forward_with_failover",
            "_save_debug_exchange",
            "_extract_response_memories",
            "_apply_post_driver_processing",
            "_apply_post_navigator_processing",
            "_handle_response_memories",
            "_record_conversation_anchor",
            "_append_pending_notices",
        ]
        for name in expected:
            assert hasattr(proxy_mod, name), f"proxy.py missing re-export: {name}"
