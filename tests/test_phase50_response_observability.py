"""Phase 50 (N12) tests: OpenRouter/nano-GPT reasoning extraction, empty-content
diagnostics in Debug, and explicit Navigator-cantrips-skipped Debug capture.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.responses import JSONResponse

from app.services.debug import init_debug
from app.services.response_pipeline import _save_debug_exchange, finalize_response
from app.services.upstream import _convert_to_sse, _do_forward_litellm, _StreamAccumulator


def _make_debug_body(model: str = "openrouter/test-model") -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
    }
    init_debug(body, [])
    return body


@pytest.mark.asyncio
class TestSaveDebugExchangeReasoningAndDiagnostics:
    async def test_extracts_reasoning_field_when_reasoning_content_and_thinking_absent(
        self, monkeypatch
    ):
        captured: dict[str, Any] = {}

        async def fake_capture_exchange(**kwargs: Any) -> None:
            captured.update(kwargs)

        monkeypatch.setattr("app.services.debug.capture_exchange", fake_capture_exchange)

        body = _make_debug_body()
        response_data = {
            "id": "chatcmpl-or-1",
            "object": "chat.completion",
            "model": "openrouter/test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Final answer.",
                        "reasoning": "Step 1: consider options. Step 2: answer.",
                    },
                    "finish_reason": "stop",
                }
            ],
        }

        await _save_debug_exchange(response_data, body, user_id="user-1")

        stages = body["_gitv_debug"]["stages"]
        llm_stage = next(s for s in stages if s["name"] == "llm_response")
        assert llm_stage["metadata"]["thinking"] == "Step 1: consider options. Step 2: answer."
        assert "empty_content_diagnostics" not in llm_stage["metadata"]
        assert llm_stage["detail"] == "Final response from upstream LLM"
        assert captured["response_content"] == "Final answer."

    async def test_attaches_empty_content_diagnostics_when_content_empty(self, monkeypatch):
        async def fake_capture_exchange(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr("app.services.debug.capture_exchange", fake_capture_exchange)

        body = _make_debug_body()
        response_data = {
            "id": "chatcmpl-empty-1",
            "object": "chat.completion",
            "model": "openrouter/test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": "Ran out of tokens thinking...",
                    },
                    "finish_reason": "length",
                }
            ],
        }

        await _save_debug_exchange(response_data, body, user_id="user-1")

        stages = body["_gitv_debug"]["stages"]
        llm_stage = next(s for s in stages if s["name"] == "llm_response")
        assert llm_stage["metadata"]["thinking"] == "Ran out of tokens thinking..."
        diag = llm_stage["metadata"]["empty_content_diagnostics"]
        assert diag["finish_reason"] == "length"
        assert diag["choice_keys"] == ["finish_reason", "index", "message"]
        assert diag["message_keys"] == ["content", "reasoning", "role"]
        assert "chatcmpl-empty-1" in diag["raw_snippet"]
        assert len(diag["raw_snippet"]) <= 800
        assert "finish_reason=length" in llm_stage["detail"]
        assert "message_keys=['content', 'reasoning', 'role']" in llm_stage["detail"]

    async def test_empty_content_diagnostics_truncates_raw_snippet_at_800(self, monkeypatch):
        async def fake_capture_exchange(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr("app.services.debug.capture_exchange", fake_capture_exchange)

        body = _make_debug_body()
        response_data = {
            "id": "chatcmpl-long",
            "extra_blob": "x" * 2000,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "content_filter"}],
        }

        await _save_debug_exchange(response_data, body, user_id="user-1")

        llm_stage = next(s for s in body["_gitv_debug"]["stages"] if s["name"] == "llm_response")
        diag = llm_stage["metadata"]["empty_content_diagnostics"]
        assert diag["finish_reason"] == "content_filter"
        assert len(diag["raw_snippet"]) == 800


@pytest.mark.asyncio
class TestFinalizeResponseNavigatorSkipped:
    async def test_emits_navigator_cantrips_skipped_when_empty_and_both_requested(self, monkeypatch):
        async def fake_save_debug(*args: Any, **kwargs: Any) -> None:
            return None

        async def fake_flush_metrics(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("app.services.response_pipeline._save_debug_exchange", fake_save_debug)
        monkeypatch.setattr("app.services.metrics.flush_metrics", fake_flush_metrics)

        body = _make_debug_body()
        response_data = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "length"}]
        }

        await finalize_response(
            response_data,
            200,
            body,
            user_id="user-1",
            run_post_driver=True,
            run_verification=False,
            run_response_capture=False,
            run_post_navigator=True,
            run_memories=False,
            append_notices=False,
            run_bypass_decode=False,
            save_debug=True,
            flush_metrics=True,
        )

        stages = body["_gitv_debug"]["stages"]
        skipped = [s for s in stages if s["name"] == "navigator_cantrips_skipped"]
        assert len(skipped) == 1
        stage = skipped[0]
        assert stage["label"] == "Navigator Cantrips Skipped"
        assert "empty" in stage["detail"].lower()
        assert stage["metadata"] == {
            "reason": "empty_response_content",
            "run_pre_navigator": True,
            "run_post_navigator": True,
        }

    async def test_emits_navigator_cantrips_skipped_when_only_post_navigator_requested(
        self, monkeypatch
    ):
        async def fake_flush_metrics(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr("app.services.metrics.flush_metrics", fake_flush_metrics)

        body = _make_debug_body()
        response_data = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]
        }

        await finalize_response(
            response_data,
            200,
            body,
            user_id="user-1",
            run_post_driver=False,
            run_verification=False,
            run_response_capture=False,
            run_post_navigator=True,
            run_memories=False,
            append_notices=False,
            run_bypass_decode=False,
            save_debug=False,
            flush_metrics=True,
        )

        skipped = [s for s in body["_gitv_debug"]["stages"] if s["name"] == "navigator_cantrips_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["metadata"] == {
            "reason": "empty_response_content",
            "run_pre_navigator": False,
            "run_post_navigator": True,
        }

    async def test_does_not_emit_navigator_cantrips_skipped_when_content_non_empty(self, monkeypatch):
        async def fake_noop_processing(response_data: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
            return response_data

        async def fake_flush_metrics(*args: Any, **kwargs: Any) -> None:
            return None

        monkeypatch.setattr(
            "app.services.response_pipeline._apply_post_driver_processing", fake_noop_processing
        )
        monkeypatch.setattr(
            "app.services.response_pipeline._apply_post_navigator_processing", fake_noop_processing
        )
        monkeypatch.setattr("app.services.metrics.flush_metrics", fake_flush_metrics)

        body = _make_debug_body()
        response_data = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}]
        }

        await finalize_response(
            response_data,
            200,
            body,
            user_id="user-1",
            run_post_driver=True,
            run_verification=False,
            run_response_capture=False,
            run_post_navigator=True,
            run_memories=False,
            append_notices=False,
            run_bypass_decode=False,
            save_debug=False,
            flush_metrics=True,
        )

        skipped = [s for s in body["_gitv_debug"]["stages"] if s["name"] == "navigator_cantrips_skipped"]
        assert skipped == []


class TestUpstreamStreamingReasoningExtraction:
    def test_stream_accumulator_captures_delta_reasoning(self):
        acc = _StreamAccumulator()
        chunks = [
            {"id": "c1", "model": "nano-gpt", "choices": [{"delta": {"reasoning": "First thought. "}}]},
            {"id": "c1", "model": "nano-gpt", "choices": [{"delta": {"reasoning": "Second thought."}}]},
            {"id": "c1", "model": "nano-gpt", "choices": [{"delta": {"content": "Answer."}, "finish_reason": "stop"}]},
        ]
        for chunk in chunks:
            acc.feed(f"data: {json.dumps(chunk)}\n\n".encode())
        acc.feed(b"data: [DONE]\n\n")

        resp = acc.as_response("fallback-model")
        msg = resp["choices"][0]["message"]
        assert msg["content"] == "Answer."
        assert msg["reasoning_content"] == "First thought. Second thought."

    @pytest.mark.asyncio
    async def test_litellm_streaming_captures_delta_reasoning(self, monkeypatch):
        async def fake_stream():
            yield {
                "id": "chatcmpl-lite-1",
                "model": "openrouter/deepseek-r1",
                "choices": [{"index": 0, "delta": {"reasoning": "Thinking A. "}, "finish_reason": None}],
            }
            yield {
                "id": "chatcmpl-lite-1",
                "model": "openrouter/deepseek-r1",
                "choices": [{"index": 0, "delta": {"reasoning": "Thinking B."}, "finish_reason": None}],
            }
            yield {
                "id": "chatcmpl-lite-1",
                "model": "openrouter/deepseek-r1",
                "choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": "stop"}],
            }

        async def fake_acompletion(**kwargs: Any):
            return fake_stream()

        fake_litellm = SimpleNamespace(
            acompletion=fake_acompletion,
            exceptions=SimpleNamespace(),
        )
        monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

        body = json.dumps(
            {
                "model": "deepseek-r1",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
        ).encode()

        result, status = await _do_forward_litellm(
            body,
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test",
            timeout=httpx.Timeout(30.0),
        )
        assert status == 200
        msg = result["choices"][0]["message"]
        assert msg["content"] == "Done."
        assert msg["reasoning_content"] == "Thinking A. Thinking B."

    @pytest.mark.asyncio
    async def test_convert_to_sse_preserves_message_reasoning(self):
        payload = {
            "id": "chatcmpl-sse-1",
            "created": 12345,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Visible reply",
                        "reasoning": "Hidden chain of thought",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        sse_resp = _convert_to_sse(
            JSONResponse(status_code=200, content=payload),
            model="openrouter/test",
            preserve_thinking=True,
        )
        collected: list[str] = []
        async for chunk in sse_resp.body_iterator:
            collected.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        joined = "".join(collected)
        assert "Hidden chain of thought" in joined
        assert "Visible reply" in joined
