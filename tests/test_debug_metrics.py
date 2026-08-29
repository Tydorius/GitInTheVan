"""Tests for per-run token, latency and cantrip accounting (Phase 22 Stage A).

These are pure-unit: the trace is a plain dict and none of debug_metrics touches
the database, so no client or session fixture is needed.
"""

from __future__ import annotations

import pytest

from app.services.debug import SCHEMA_VERSION, debug_capture, init_debug
from app.services.debug_metrics import (
    TOKENS_ESTIMATED,
    TOKENS_MIXED,
    TOKENS_UPSTREAM,
    code_fingerprint,
    extract_usage,
    record_cantrip,
    record_llm_call,
    stage_injection_costs,
    summarize_run,
)


def _trace(content: str = "hello there friend") -> tuple[dict, dict]:
    body = {"messages": [{"role": "user", "content": content}]}
    init_debug(body, [])
    return body, body["_gitv_debug"]


class TestTraceEnvelope:
    def test_init_debug_carries_schema_and_run_block(self):
        _, trace = _trace()
        assert trace["schema_version"] == SCHEMA_VERSION
        assert trace["run"]["source"] == "live"
        assert trace["run"]["llm_calls"] == []
        assert trace["run"]["cantrips"] == []

    def test_replay_is_marked_on_the_run(self):
        body = {"messages": [], "_gitv_replay": True, "_gitv_replay_of": "abc"}
        init_debug(body, [])
        assert body["_gitv_debug"]["run"]["source"] == "replay"
        assert body["_gitv_debug"]["run"]["replay_of"] == "abc"

    def test_every_recorder_no_ops_without_a_trace(self):
        """Call sites carry no `if debug_on` guard, so this must never raise."""
        bare = {"messages": []}
        record_llm_call(bare, purpose="main", latency_ms=1.0, status_code=200)
        record_cantrip(bare, cantrip_id="x", name="y", position="pre_driver", triggered=True)
        assert "_gitv_debug" not in bare


class TestUsageExtraction:
    def test_upstream_usage_is_preferred_and_labelled(self):
        body, trace = _trace()
        record_llm_call(
            body, purpose="main", latency_ms=1200.0, status_code=200,
            response_data={
                "model": "gpt-4o-2024-11-20",
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 150,
                    "completion_tokens_details": {"reasoning_tokens": 40},
                },
                "choices": [{"message": {"content": "hi"}}],
            },
            messages=body["messages"],
            model_requested="gpt-4o",
        )
        call = trace["run"]["llm_calls"][0]
        assert call["tokens_source"] == TOKENS_UPSTREAM
        assert call["prompt_tokens"] == 900
        assert call["reasoning_tokens"] == 40

    def test_resolved_model_is_recorded_separately_from_the_request(self):
        """An endpoint override or a provider alias makes these differ, and the
        comparison view highlights exactly that."""
        body, trace = _trace()
        record_llm_call(
            body, purpose="main", latency_ms=10.0, status_code=200,
            response_data={"model": "gpt-4o-2024-11-20", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            model_requested="gpt-4o",
        )
        call = trace["run"]["llm_calls"][0]
        assert call["model_requested"] == "gpt-4o"
        assert call["model_resolved"] == "gpt-4o-2024-11-20"

    def test_missing_usage_falls_back_to_estimation(self):
        body, trace = _trace()
        record_llm_call(
            body, purpose="main", latency_ms=300.0, status_code=200,
            response_data={"choices": [{"message": {"content": "x" * 400}}]},
            messages=body["messages"],
        )
        call = trace["run"]["llm_calls"][0]
        assert call["tokens_source"] == TOKENS_ESTIMATED
        assert call["completion_tokens"] == 100  # 400 chars / 4

    @pytest.mark.parametrize("payload", [
        None,
        {},
        {"usage": None},
        {"usage": {}},
        {"usage": {"prompt_tokens": None, "completion_tokens": None}},
    ])
    def test_unusable_usage_blocks_are_treated_as_absent(self, payload):
        """Some providers emit the key with nulls on a streamed response; that
        must fall through to estimation rather than reporting zero tokens."""
        assert extract_usage(payload) is None


class TestRunTotals:
    def test_overhead_is_wall_clock_minus_upstream(self):
        body, trace = _trace()
        record_llm_call(body, purpose="main", latency_ms=1200.0, status_code=200,
                        response_data={"usage": {"prompt_tokens": 900, "completion_tokens": 150}})
        record_llm_call(body, purpose="verification_judge", latency_ms=300.0, status_code=200,
                        response_data={"usage": {"prompt_tokens": 100, "completion_tokens": 50}})

        totals = summarize_run(trace, total_latency_ms=2000.0)
        assert totals["llm_latency_ms"] == 1500.0
        assert totals["overhead_ms"] == 500.0
        assert totals["prompt_tokens"] == 1000
        assert totals["llm_call_count"] == 2

    def test_overhead_never_goes_negative(self):
        """Map stages can run concurrently, so summed upstream time may exceed
        wall clock. A negative overhead would read as a bug."""
        body, trace = _trace()
        record_llm_call(body, purpose="map_stage", latency_ms=5000.0, status_code=200,
                        response_data={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        assert summarize_run(trace, total_latency_ms=1000.0)["overhead_ms"] == 0.0

    def test_mixed_token_sources_are_reported_as_mixed(self):
        """An estimate compared against a measurement without saying so would
        make the comparison view lie."""
        body, trace = _trace()
        record_llm_call(body, purpose="main", latency_ms=10.0, status_code=200,
                        response_data={"usage": {"prompt_tokens": 900, "completion_tokens": 150}})
        record_llm_call(body, purpose="summarizer", latency_ms=10.0, status_code=200,
                        response_data={"choices": [{"message": {"content": "x" * 40}}]},
                        messages=body["messages"])
        assert summarize_run(trace)["tokens_source"] == TOKENS_MIXED

    def test_injected_tokens_net_out_summarization(self):
        """Measured against the final snapshot, not by summing stage deltas, so
        a stage that removes tokens cancels one that adds them."""
        body, trace = _trace()
        body["messages"].append({"role": "system", "content": "L" * 4000})
        debug_capture(body, "lorebook_injection", "Lorebook Injection", detail="1")
        body["messages"] = [{"role": "user", "content": "short"}]
        debug_capture(body, "conversation_summarization", "Summarization", detail="compressed")

        assert summarize_run(trace)["injected_tokens"] == 0

    def test_empty_run_produces_usable_totals(self):
        _, trace = _trace()
        totals = summarize_run(trace, total_latency_ms=50.0)
        assert totals["llm_call_count"] == 0
        assert totals["total_tokens"] == 0
        assert totals["overhead_ms"] == 50.0


class TestCantripRecording:
    def test_a_cantrip_that_did_not_fire_is_still_recorded(self):
        """"Fired in run A, silent in run B" is the most useful cantrip diff and
        is invisible if only executions are stored."""
        body, trace = _trace()
        record_cantrip(body, cantrip_id="c2", name="Weather", position="pre_driver",
                       triggered=False, reason="tag not present")
        entry = trace["run"]["cantrips"][0]
        assert entry["triggered"] is False
        assert entry["reason"] == "tag not present"
        assert "code" not in entry

    def test_execution_records_code_output_and_changed_fields(self):
        body, trace = _trace()
        record_cantrip(
            body, cantrip_id="c1", name="Dice", position="pre_driver", triggered=True,
            code="export function x(){}", duration_ms=12.34,
            output={"personality": "bold", "scenario": "", "memories": {}},
            debug_logs=["rolled 17"],
        )
        entry = trace["run"]["cantrips"][0]
        assert entry["fields_changed"] == ["personality"]
        assert entry["debug_logs"] == ["rolled 17"]
        assert entry["duration_ms"] == 12.3
        assert entry["code_hash"] == code_fingerprint("export function x(){}")

    def test_code_fingerprint_is_stable_and_distinguishes_versions(self):
        assert code_fingerprint("a") == code_fingerprint("a")
        assert code_fingerprint("a") != code_fingerprint("b")
        assert code_fingerprint("") == ""


class TestInjectionCosts:
    def test_stages_are_ranked_by_absolute_token_delta(self):
        body, trace = _trace()
        body["messages"].append({"role": "system", "content": "S" * 400})
        debug_capture(body, "skills_injection", "Skills", detail="1 skill")
        body["messages"].append({"role": "system", "content": "L" * 4000})
        debug_capture(body, "lorebook_injection", "Lorebook", detail="1 entry")

        costs = stage_injection_costs(trace)
        assert [c["name"] for c in costs] == ["lorebook_injection", "skills_injection"]
        assert costs[0]["delta_tokens"] == 1000

    def test_a_stage_that_removed_tokens_is_kept_with_a_negative_delta(self):
        body, trace = _trace("x" * 4000)
        body["messages"] = [{"role": "user", "content": "short"}]
        debug_capture(body, "conversation_summarization", "Summarization", detail="compressed")

        costs = stage_injection_costs(trace)
        assert len(costs) == 1
        assert costs[0]["delta_tokens"] < 0
