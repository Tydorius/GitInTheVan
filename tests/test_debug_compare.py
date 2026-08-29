"""Tests for run comparison and export (Phase 22 Stages B and D).

Pure-unit over serialized-exchange dicts; neither service touches the database.
"""

from __future__ import annotations

import json

import pytest

from app.services.debug_compare import compare_runs
from app.services.debug_export import render_comparison, render_exchange


def make_run(
    run_id: str,
    *,
    label: str = "",
    model: str = "gpt-4o",
    resolved: str = "gpt-4o",
    endpoint: str = "Primary",
    cantrips: list[dict] | None = None,
    stages: list[dict] | None = None,
    totals: dict | None = None,
    response: str = "The dragon roars.",
    verification: dict | None = None,
) -> dict:
    return {
        "id": run_id,
        "chat_id": "chat-1",
        "model": model,
        "label": label,
        "saved": False,
        "saved_at": "",
        "created_at": "2026-08-24T10:00:00+00:00",
        "response_content": response,
        "verification_data": verification if verification is not None else {},
        "pipeline_data": {
            "schema_version": 2,
            "tags": [],
            "original_messages": json.dumps([{"role": "user", "content": "hi"}]),
            "stages": stages or [],
            "run": {
                "started_at": "2026-08-24T10:00:00+00:00",
                "source": "live",
                "replay_of": "",
                "cantrips": cantrips or [],
                "llm_calls": [{
                    "purpose": "main",
                    "endpoint_name": endpoint,
                    "model_requested": model,
                    "model_resolved": resolved,
                    "provider": "openai",
                    "latency_ms": 1000.0,
                    "prompt_tokens": 500,
                    "completion_tokens": 100,
                    "tokens_source": "upstream",
                    "status_code": 200,
                }],
                "totals": totals if totals is not None else {
                    "llm_call_count": 1,
                    "prompt_tokens": 500,
                    "completion_tokens": 100,
                    "total_tokens": 600,
                    "injected_tokens": 200,
                    "tokens_source": "upstream",
                    "llm_latency_ms": 1000.0,
                    "total_latency_ms": 1200.0,
                    "overhead_ms": 200.0,
                },
            },
        },
    }


def cantrip(cid: str, name: str, *, triggered=True, code="A", output=None, reason="") -> dict:
    entry = {
        "id": cid, "name": name, "position": "pre_driver",
        "triggered": triggered, "reason": reason,
    }
    if triggered:
        entry.update({
            "code": code,
            "code_hash": f"hash-{code}",
            "output": output if output is not None else {"personality": "bold"},
            "fields_changed": sorted(k for k, v in (output or {"personality": "bold"}).items() if v),
            "debug_logs": [],
            "error": "",
            "duration_ms": 5.0,
        })
    return entry


class TestBaselineSelection:
    def test_baseline_is_moved_to_the_front_of_the_order(self):
        """Selecting a new baseline re-columns the view without re-running."""
        runs = [make_run("a"), make_run("b"), make_run("c")]
        result = compare_runs(runs, "c")
        assert result["order"][0] == "c"
        assert set(result["order"]) == {"a", "b", "c"}
        assert set(result["diffs"]) == {"a", "b"}

    def test_every_non_baseline_run_is_diffed_against_the_baseline_only(self):
        runs = [make_run("a"), make_run("b"), make_run("c"), make_run("d")]
        result = compare_runs(runs, "a")
        assert set(result["diffs"]) == {"b", "c", "d"}


class TestIdentityDiff:
    def test_model_change_is_reported(self):
        runs = [make_run("a", resolved="gpt-4o"), make_run("b", resolved="claude-sonnet-4")]
        identity = compare_runs(runs, "a")["diffs"]["b"]["identity"]
        assert not identity["same"]
        assert identity["changed"]["models_resolved"]["other"] == ["claude-sonnet-4"]

    def test_endpoint_change_is_reported(self):
        runs = [make_run("a", endpoint="Primary"), make_run("b", endpoint="Backup")]
        identity = compare_runs(runs, "a")["diffs"]["b"]["identity"]
        assert identity["changed"]["endpoints"]["other"] == ["Backup"]

    def test_identical_configuration_reports_same(self):
        runs = [make_run("a"), make_run("b")]
        assert compare_runs(runs, "a")["diffs"]["b"]["identity"]["same"]


class TestCantripDiff:
    def test_a_cantrip_present_in_only_one_run_is_added_or_removed(self):
        runs = [
            make_run("a", cantrips=[cantrip("c1", "Dice")]),
            make_run("b", cantrips=[cantrip("c1", "Dice"), cantrip("c2", "Weather")]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["cantrips"]
        assert [c["name"] for c in d["added"]] == ["Weather"]
        assert d["removed"] == []

    def test_same_cantrip_firing_in_one_run_only_is_a_trigger_change(self):
        """The resource is the same and its behaviour is not; reporting it as
        added/removed would hide the interesting case."""
        runs = [
            make_run("a", cantrips=[cantrip("c1", "Dice", triggered=True)]),
            make_run("b", cantrips=[cantrip("c1", "Dice", triggered=False, reason="tag absent")]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["cantrips"]
        assert d["added"] == [] and d["removed"] == []
        assert len(d["trigger_changed"]) == 1
        change = d["trigger_changed"][0]
        assert change["baseline_triggered"] is True
        assert change["other_triggered"] is False
        assert change["other_reason"] == "tag absent"

    def test_changed_code_is_reported_with_a_diff(self):
        runs = [
            make_run("a", cantrips=[cantrip("c1", "Dice", code="line one\nline two")]),
            make_run("b", cantrips=[cantrip("c1", "Dice", code="line one\nline CHANGED")]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["cantrips"]
        assert len(d["code_changed"]) == 1
        diff = d["code_changed"][0]["diff"]
        assert not diff["identical"]
        assert any("CHANGED" in line for h in diff["hunks"] for line in h["other"])

    def test_changed_output_is_reported(self):
        runs = [
            make_run("a", cantrips=[cantrip("c1", "Dice", output={"personality": "bold"})]),
            make_run("b", cantrips=[cantrip("c1", "Dice", output={"personality": "timid"})]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["cantrips"]
        assert len(d["output_changed"]) == 1

    def test_identical_cantrips_report_same(self):
        runs = [
            make_run("a", cantrips=[cantrip("c1", "Dice")]),
            make_run("b", cantrips=[cantrip("c1", "Dice")]),
        ]
        assert compare_runs(runs, "a")["diffs"]["b"]["cantrips"]["same"]


class TestMetricsDiff:
    def test_deltas_and_percentages_are_computed(self):
        runs = [
            make_run("a"),
            make_run("b", totals={"total_tokens": 900, "llm_latency_ms": 500.0,
                                  "tokens_source": "upstream"}),
        ]
        m = compare_runs(runs, "a")["diffs"]["b"]["metrics"]
        assert m["total_tokens"]["delta"] == 300
        assert m["total_tokens"]["pct"] == 50.0
        assert m["total_tokens"]["lower_is_better"] is True
        assert m["llm_latency_ms"]["delta"] == -500.0

    def test_mixing_measured_and_estimated_tokens_is_flagged_incomparable(self):
        """A delta between an estimate and a measurement implies precision it
        does not have."""
        runs = [
            make_run("a"),
            make_run("b", totals={"total_tokens": 600, "tokens_source": "estimated"}),
        ]
        m = compare_runs(runs, "a")["diffs"]["b"]["metrics"]
        assert m["_comparable"] is False
        assert m["_tokens_source"] == {"baseline": "upstream", "other": "estimated"}

    def test_matching_sources_are_comparable(self):
        runs = [make_run("a"), make_run("b")]
        assert compare_runs(runs, "a")["diffs"]["b"]["metrics"]["_comparable"] is True


class TestStageAlignment:
    def test_stages_are_matched_on_identity_not_position(self):
        """A stage inserted in the middle must not shift every later stage into
        a mismatch."""
        common = [
            {"name": "map_stage", "label": "S1", "item_id": "s1", "detail": "one"},
            {"name": "map_stage", "label": "S3", "item_id": "s3", "detail": "three"},
        ]
        inserted = [
            common[0],
            {"name": "map_stage", "label": "S2", "item_id": "s2", "detail": "two"},
            common[1],
        ]
        runs = [make_run("a", stages=common), make_run("b", stages=inserted)]
        d = compare_runs(runs, "a")["diffs"]["b"]["stages"]

        assert [s["label"] for s in d["only_in_other"]] == ["S2"]
        assert d["only_in_baseline"] == []
        assert d["changed"] == []

    def test_a_changed_stage_detail_is_reported(self):
        runs = [
            make_run("a", stages=[{"name": "lorebook_injection", "label": "Lore",
                                   "item_id": None, "detail": "2 entries matched"}]),
            make_run("b", stages=[{"name": "lorebook_injection", "label": "Lore",
                                   "item_id": None, "detail": "5 entries matched"}]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["stages"]
        assert len(d["changed"]) == 1
        assert d["changed"][0]["other_detail"] == "5 entries matched"


class TestInjectionDiff:
    def _with_entries(self, run_id, entries):
        return make_run(run_id, stages=[{
            "name": "lorebook_injection", "label": "Lorebook Injection",
            "item_id": None, "detail": "", "metadata": {"entries": entries},
        }])

    def test_added_and_removed_entries_are_reported_with_tokens(self):
        a = self._with_entries("a", [
            {"entry_id": "e1", "entry_name": "Ashfall", "lorebook_name": "World", "tokens": 100},
        ])
        b = self._with_entries("b", [
            {"entry_id": "e1", "entry_name": "Ashfall", "lorebook_name": "World", "tokens": 100},
            {"entry_id": "e2", "entry_name": "Dragons", "lorebook_name": "World", "tokens": 250},
        ])
        d = compare_runs([a, b], "a")["diffs"]["b"]["lorebooks"]
        assert [e["name"] for e in d["added"]] == ["Dragons"]
        assert d["baseline_tokens"] == 100
        assert d["other_tokens"] == 350


class TestVerificationDiff:
    def test_a_pass_becoming_a_fail_is_reported(self):
        a = make_run("a", verification={"approved": True, "retries_used": 0, "check_history": []})
        b = make_run("b", verification={
            "approved": False, "retries_used": 2,
            "check_history": [{"approved": False, "violations": [{"detail": "too short"}], "thinking": ""}],
        })
        d = compare_runs([a, b], "a")["diffs"]["b"]["verification"]
        assert d["approval_changed"] is True
        assert d["other_violations"] == ["too short"]
        assert d["other_retries"] == 2

    def test_legacy_repr_violations_are_still_comparable(self):
        """Exchanges stored before Phase 22 hold Python reprs, not dicts."""
        a = make_run("a", verification={
            "approved": False, "retries_used": 1,
            "check_history": [{"approved": False, "violations": ["VerificationJudgment(...)"], "thinking": ""}],
        })
        b = make_run("b", verification={"approved": True, "retries_used": 0, "check_history": []})
        d = compare_runs([a, b], "a")["diffs"]["b"]["verification"]
        assert d["baseline_violations"] == ["VerificationJudgment(...)"]

    def test_no_verification_in_either_run_reports_not_run(self):
        d = compare_runs([make_run("a"), make_run("b")], "a")["diffs"]["b"]["verification"]
        assert d["ran"] is False


class TestTextDiff:
    def test_identical_response_is_reported_as_identical(self):
        d = compare_runs([make_run("a"), make_run("b")], "a")["diffs"]["b"]["response"]
        assert d["identical"] is True
        assert d["hunks"] == []

    def test_changed_response_produces_hunks_and_a_similarity(self):
        runs = [
            make_run("a", response="line one\nline two\nline three\nline four\nline five"),
            make_run("b", response="line one\nline TWO\nline three\nline four\nline five"),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["response"]
        assert d["identical"] is False
        assert d["granularity"] == "line"
        assert 0 < d["similarity"] < 1
        assert len(d["hunks"]) == 1

    def test_short_prose_is_diffed_by_word_not_by_line(self):
        """A one-line response diffed by line is a whole-block replace, reported
        as 0% similar — which tells the reader nothing about what changed."""
        runs = [
            make_run("a", response="The dragon wheels overhead, catching the last light."),
            make_run("b", response="The dragon wheels overhead, catching the first light."),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["response"]
        assert d["granularity"] == "word"
        assert d["similarity"] > 0.9
        # The hunk names the changed word, not the whole sentence.
        assert any("first" in line for h in d["hunks"] for line in h["other"])
        assert not any("dragon" in line for h in d["hunks"] for line in h["other"])

    def test_multi_line_code_still_uses_line_granularity(self):
        runs = [
            make_run("a", cantrips=[cantrip("c1", "D", code="a\nb\nc\nd\ne\nf")]),
            make_run("b", cantrips=[cantrip("c1", "D", code="a\nb\nZ\nd\ne\nf")]),
        ]
        d = compare_runs(runs, "a")["diffs"]["b"]["cantrips"]["code_changed"][0]["diff"]
        assert d["granularity"] == "line"


class TestExport:
    def test_json_export_is_lossless(self):
        run = make_run("a", label="Baseline", cantrips=[cantrip("c1", "Dice")])
        content, media, filename = render_exchange(run, "json")
        payload = json.loads(content)
        assert media == "application/json"
        assert filename == "gitv-debug-baseline.json"
        assert payload["run"]["pipeline_data"]["run"]["cantrips"][0]["code"] == "A"

    def test_markdown_export_preserves_reasoning_in_full(self):
        """Truncated reasoning was the thing that made the old debug view
        useless for comparison; the export must not reintroduce it."""
        reasoning = "R" * 5000
        run = make_run("a", stages=[{
            "name": "llm_response", "label": "LLM Response", "item_id": None,
            "detail": "", "metadata": {"thinking": reasoning}, "content_after": "hi",
        }])
        content, media, filename = render_exchange(run, "markdown")
        assert media.startswith("text/markdown")
        assert reasoning in content

    def test_markdown_export_reports_the_cantrips_that_did_not_fire(self):
        run = make_run("a", cantrips=[
            cantrip("c1", "Dice"),
            cantrip("c2", "Weather", triggered=False, reason="tag absent"),
        ])
        content, _, _ = render_exchange(run, "markdown")
        assert "Did not fire" in content
        assert "Weather (tag absent)" in content

    def test_markdown_labels_estimated_token_counts(self):
        run = make_run("a", totals={"total_tokens": 100, "tokens_source": "estimated"})
        content, _, _ = render_exchange(run, "markdown")
        assert "**estimated**" in content

    def test_content_containing_a_fence_does_not_break_the_document(self):
        """Cantrip code and LLM output routinely contain fenced blocks; a plain
        three-backtick fence would be closed early by them."""
        run = make_run("a", response="Here is code:\n```js\nconst x = 1\n```\ndone")
        content, _, _ = render_exchange(run, "markdown")
        assert "````" in content

    def test_comparison_markdown_names_the_baseline_and_the_deltas(self):
        runs = [
            make_run("a", label="Original", cantrips=[cantrip("c1", "Dice", code="A")]),
            make_run("b", label="Variant", cantrips=[cantrip("c1", "Dice", code="B")],
                     totals={"total_tokens": 900, "tokens_source": "upstream"}),
        ]
        result = compare_runs(runs, "a")
        content, media, filename = render_comparison(runs, result, "markdown")
        assert filename == "gitv-comparison.md"
        assert "Baseline: **Original**" in content
        assert "code changed" in content
        assert "Variant vs baseline" in content

    def test_comparison_json_carries_both_the_diff_and_the_runs(self):
        runs = [make_run("a"), make_run("b")]
        result = compare_runs(runs, "a")
        content, _, _ = render_comparison(runs, result, "json")
        payload = json.loads(content)
        assert payload["kind"] == "debug_comparison"
        assert set(payload["runs"]) == {"a", "b"}
        assert payload["comparison"]["baseline_id"] == "a"


class TestSchemaOneTolerance:
    def test_a_run_with_no_run_block_still_compares(self):
        """Exchanges captured before Phase 22 have no run block; comparing one
        against a new run must not raise."""
        legacy = make_run("a")
        del legacy["pipeline_data"]["run"]
        legacy["pipeline_data"]["schema_version"] = 1

        result = compare_runs([legacy, make_run("b")], "a")
        d = result["diffs"]["b"]
        assert d["cantrips"]["same"] is False or d["cantrips"]["added"] == []
        assert result["runs"]["a"]["totals"] == {}

    def test_a_legacy_run_exports_without_a_metrics_section(self):
        legacy = make_run("a")
        del legacy["pipeline_data"]["run"]
        content, _, _ = render_exchange(legacy, "markdown")
        assert "## Metrics" not in content
        assert "## Final Response" in content


@pytest.mark.parametrize("fmt", ["json", "markdown"])
def test_export_handles_an_empty_run(fmt):
    """A run captured from a failed request has almost nothing in it."""
    empty = {
        "id": "x", "chat_id": "", "model": "", "label": "", "saved": False,
        "saved_at": "", "created_at": "", "response_content": "",
        "verification_data": {},
        "pipeline_data": {"stages": [], "tags": [], "run": {}},
    }
    content, _, _ = render_exchange(empty, fmt)
    assert content
