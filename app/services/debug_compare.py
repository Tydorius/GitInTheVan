"""Diff two to four debug runs against a chosen baseline.

Diffing lives on the server, not in the browser, for three reasons: one
implementation feeds the screen, the Markdown export and the JSON export;
``difflib`` is in the standard library while the frontend would need a new npm
dependency; and a pure function over two dicts is straightforward to test.

The output shape is deliberately flat. Each section reports, per non-baseline
run, what was **added**, **removed** and **changed** relative to the baseline,
so the UI renders chips without re-deriving anything.

Two rules shape everything here:

* **Absence is a finding.** A cantrip that fired in the baseline and stayed
  silent in run B is the single most useful thing a comparison can surface, so
  the considered-but-not-triggered set is compared, not just the executions.
* **Alignment is explicit.** Stages are matched on identity, and anything that
  matches nothing is reported as present-in-one-run rather than being paired
  positionally with an unrelated stage.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Text longer than this is diffed but not echoed in full inside each hunk, to
# keep a comparison payload from ballooning on a long reasoning block.
MAX_HUNK_CHARS = 2000


def compare_runs(runs: list[dict[str, Any]], baseline_id: str) -> dict[str, Any]:
    """Compare runs against ``baseline_id``.

    ``runs`` are serialized exchanges as ``debug.get_exchange`` returns them.
    The baseline is moved to the front of ``order`` so the UI can render columns
    directly from it.
    """
    by_id = {r["id"]: r for r in runs}
    baseline = by_id[baseline_id]
    others = [r for r in runs if r["id"] != baseline_id]

    return {
        "baseline_id": baseline_id,
        "order": [baseline_id] + [r["id"] for r in others],
        "runs": {r["id"]: _run_summary(r) for r in runs},
        "diffs": {r["id"]: _diff_pair(baseline, r) for r in others},
    }


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """The per-column header data: identity, models, endpoints, totals."""
    pipeline = run.get("pipeline_data", {})
    block = pipeline.get("run", {})
    calls = block.get("llm_calls", [])

    return {
        "id": run["id"],
        "label": run.get("label", ""),
        "created_at": run.get("created_at", ""),
        "saved": run.get("saved", False),
        "source": block.get("source", "live"),
        "replay_of": block.get("replay_of", ""),
        "model_requested": run.get("model", ""),
        "models_resolved": _unique(c.get("model_resolved", "") for c in calls),
        "endpoints": _unique(c.get("endpoint_name", "") for c in calls),
        "providers": _unique(c.get("provider", "") for c in calls),
        "totals": block.get("totals", {}),
        "stage_count": len(pipeline.get("stages", [])),
        "cantrips_fired": sorted(
            c["name"] for c in block.get("cantrips", []) if c.get("triggered")
        ),
    }


def _diff_pair(baseline: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity": _diff_identity(baseline, other),
        "metrics": _diff_metrics(baseline, other),
        "cantrips": _diff_cantrips(baseline, other),
        "lorebooks": _diff_injections(baseline, other, "lorebook_injection", "entries", "entry_id", "entry_name"),
        "skills": _diff_injections(baseline, other, "skills_injection", "skills", "id", "name"),
        "samples": _diff_injections(baseline, other, "skills_injection", "samples", "id", "name"),
        "stages": _diff_stages(baseline, other),
        "verification": _diff_verification(baseline, other),
        "response": _diff_text(
            baseline.get("response_content", ""), other.get("response_content", "")
        ),
        "reasoning": _diff_text(_reasoning(baseline), _reasoning(other)),
    }


# --------------------------------------------------------------------------
# Identity and metrics
# --------------------------------------------------------------------------

def _diff_identity(baseline: dict, other: dict) -> dict[str, Any]:
    """Model, endpoint and provider differences.

    Kept separate from metrics because "you changed the model" explains a token
    or latency delta, and a user reading a comparison should see the cause next
    to the effect.
    """
    b, o = _run_summary(baseline), _run_summary(other)
    fields = {}
    for key in ("model_requested", "models_resolved", "endpoints", "providers", "source"):
        if b[key] != o[key]:
            fields[key] = {"baseline": b[key], "other": o[key]}
    return {"changed": fields, "same": not fields}


# Lower is better for these, so a negative delta is an improvement. Everything
# else is reported without a judgement.
_LOWER_IS_BETTER = {
    "total_tokens", "prompt_tokens", "completion_tokens", "injected_tokens",
    "llm_latency_ms", "total_latency_ms", "overhead_ms", "injection_overhead_pct",
}


def _diff_metrics(baseline: dict, other: dict) -> dict[str, Any]:
    b = baseline.get("pipeline_data", {}).get("run", {}).get("totals", {})
    o = other.get("pipeline_data", {}).get("run", {}).get("totals", {})

    out: dict[str, Any] = {}
    for key in sorted(set(b) | set(o)):
        if key == "tokens_source":
            continue
        bv, ov = b.get(key), o.get(key)
        if not isinstance(bv, (int, float)) or not isinstance(ov, (int, float)):
            continue
        delta = ov - bv
        out[key] = {
            "baseline": bv,
            "other": ov,
            "delta": round(delta, 2),
            "pct": round(delta / bv * 100.0, 1) if bv else None,
            "lower_is_better": key in _LOWER_IS_BETTER,
        }

    # An estimate compared against a measurement is not a like-for-like number.
    # Say so rather than letting the delta imply more precision than it has.
    sources = {b.get("tokens_source", ""), o.get("tokens_source", "")}
    out["_comparable"] = not (sources - {""}) or len(sources - {""}) == 1
    out["_tokens_source"] = {
        "baseline": b.get("tokens_source", ""),
        "other": o.get("tokens_source", ""),
    }
    return out


# --------------------------------------------------------------------------
# Cantrips
# --------------------------------------------------------------------------

def _cantrip_index(run: dict) -> dict[str, dict]:
    block = run.get("pipeline_data", {}).get("run", {})
    return {c["id"]: c for c in block.get("cantrips", []) if c.get("id")}


def _diff_cantrips(baseline: dict, other: dict) -> dict[str, Any]:
    """What ran, what did not, and whose code changed.

    A cantrip present in both but silent in one is reported under
    ``trigger_changed`` rather than as added or removed -- the resource is the
    same, its behaviour is not, and conflating the two hides the interesting
    case.
    """
    b, o = _cantrip_index(baseline), _cantrip_index(other)

    added, removed, code_changed, output_changed, trigger_changed = [], [], [], [], []
    data_changed: list[dict[str, Any]] = []

    for cid in sorted(set(b) | set(o)):
        bc, oc = b.get(cid), o.get(cid)
        if bc is None:
            added.append(_cantrip_ref(oc))
            continue
        if oc is None:
            removed.append(_cantrip_ref(bc))
            continue

        if bool(bc.get("triggered")) != bool(oc.get("triggered")):
            trigger_changed.append({
                **_cantrip_ref(oc),
                "baseline_triggered": bool(bc.get("triggered")),
                "other_triggered": bool(oc.get("triggered")),
                "baseline_reason": bc.get("reason", ""),
                "other_reason": oc.get("reason", ""),
            })
            continue

        if not bc.get("triggered"):
            continue

        if bc.get("code_hash") != oc.get("code_hash"):
            code_changed.append({
                **_cantrip_ref(oc),
                "baseline_hash": bc.get("code_hash", ""),
                "other_hash": oc.get("code_hash", ""),
                "diff": _diff_text(bc.get("code", ""), oc.get("code", "")),
            })

        if bc.get("data_changes") != oc.get("data_changes"):
            data_changed.append({
                **_cantrip_ref(oc),
                "baseline": bc.get("data_changes", {}),
                "other": oc.get("data_changes", {}),
                "stores": sorted(
                    set(bc.get("data_changes", {})) | set(oc.get("data_changes", {}))
                ),
            })

        if bc.get("output") != oc.get("output"):
            output_changed.append({
                **_cantrip_ref(oc),
                "fields": sorted(
                    set(bc.get("fields_changed", [])) ^ set(oc.get("fields_changed", []))
                ) or ["(same fields, different content)"],
                "diff": _diff_text(
                    json.dumps(bc.get("output", {}), indent=2, sort_keys=True, default=str),
                    json.dumps(oc.get("output", {}), indent=2, sort_keys=True, default=str),
                ),
            })

    return {
        "added": added,
        "removed": removed,
        "code_changed": code_changed,
        "output_changed": output_changed,
        "trigger_changed": trigger_changed,
        # A cantrip whose persistent writes changed between runs -- it stopped
        # setting a key, or set a different value. Invisible in `output`, which
        # only carries what the cantrip returned to the prompt.
        "data_changed": data_changed,
        "same": not (
            added or removed or code_changed or output_changed
            or trigger_changed or data_changed
        ),
    }


def _cantrip_ref(c: dict) -> dict[str, Any]:
    return {
        "id": c.get("id", ""),
        "name": c.get("name", ""),
        "position": c.get("position", ""),
        "triggered": bool(c.get("triggered")),
    }


# --------------------------------------------------------------------------
# Injections (lorebook entries, skills, samples)
# --------------------------------------------------------------------------

def _stage_metadata(run: dict, stage_name: str) -> dict:
    for stage in run.get("pipeline_data", {}).get("stages", []):
        if stage.get("name") == stage_name:
            return stage.get("metadata", {}) or {}
    return {}


def _diff_injections(
    baseline: dict, other: dict, stage_name: str,
    key: str, id_field: str, name_field: str,
) -> dict[str, Any]:
    """Set difference over one injection stage's item list."""
    b = {i.get(id_field, ""): i for i in _stage_metadata(baseline, stage_name).get(key, [])}
    o = {i.get(id_field, ""): i for i in _stage_metadata(other, stage_name).get(key, [])}

    def ref(item: dict) -> dict:
        return {
            "id": item.get(id_field, ""),
            "name": item.get(name_field, ""),
            "tokens": item.get("tokens", 0),
            "lorebook_id": item.get("lorebook_id", ""),
            "lorebook_name": item.get("lorebook_name", ""),
        }

    added = [ref(o[k]) for k in sorted(set(o) - set(b))]
    removed = [ref(b[k]) for k in sorted(set(b) - set(o))]
    return {
        "added": added,
        "removed": removed,
        "baseline_tokens": sum(i.get("tokens", 0) for i in b.values()),
        "other_tokens": sum(i.get("tokens", 0) for i in o.values()),
        "same": not (added or removed),
    }


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

def _stage_key(stage: dict, index: int) -> tuple:
    """Identity for alignment: name plus item id, falling back to position.

    Without the item id, two map stages would pair by name alone and a stage
    inserted in the middle would shift every later comparison by one.
    """
    return (stage.get("name", ""), stage.get("item_id") or f"#{index}")


def _diff_stages(baseline: dict, other: dict) -> dict[str, Any]:
    b_stages = baseline.get("pipeline_data", {}).get("stages", [])
    o_stages = other.get("pipeline_data", {}).get("stages", [])

    b = {_stage_key(s, i): s for i, s in enumerate(b_stages)}
    o = {_stage_key(s, i): s for i, s in enumerate(o_stages)}

    def ref(s: dict) -> dict:
        return {
            "name": s.get("name", ""),
            "label": s.get("label", ""),
            "item_id": s.get("item_id"),
            "item_name": s.get("item_name"),
            "detail": s.get("detail", ""),
        }

    only_baseline = [ref(b[k]) for k in b if k not in o]
    only_other = [ref(o[k]) for k in o if k not in b]

    changed = []
    for k in b.keys() & o.keys():
        bs, os_ = b[k], o[k]
        if bs.get("detail") != os_.get("detail") or bs.get("setting_value") != os_.get("setting_value"):
            changed.append({
                **ref(os_),
                "baseline_detail": bs.get("detail", ""),
                "other_detail": os_.get("detail", ""),
            })

    return {
        "only_in_baseline": only_baseline,
        "only_in_other": only_other,
        "changed": sorted(changed, key=lambda c: c["label"]),
        "same": not (only_baseline or only_other or changed),
    }


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _diff_verification(baseline: dict, other: dict) -> dict[str, Any]:
    b = baseline.get("verification_data") or {}
    o = other.get("verification_data") or {}

    if not b and not o:
        return {"ran": False, "same": True}

    b_checks = b.get("check_history", [])
    o_checks = o.get("check_history", [])

    return {
        "ran": True,
        "baseline_approved": b.get("approved"),
        "other_approved": o.get("approved"),
        "approval_changed": b.get("approved") != o.get("approved"),
        "baseline_retries": b.get("retries_used", 0),
        "other_retries": o.get("retries_used", 0),
        "baseline_violations": _violation_texts(b_checks),
        "other_violations": _violation_texts(o_checks),
        "same": (
            b.get("approved") == o.get("approved")
            and b.get("retries_used") == o.get("retries_used")
            and _violation_texts(b_checks) == _violation_texts(o_checks)
        ),
    }


def _violation_texts(checks: list[dict]) -> list[str]:
    """Flatten violations to comparable strings.

    Violations are dicts since Phase 22; older exchanges hold the Python repr
    they were stored as, so both are coerced to text rather than assuming a
    shape that a pre-upgrade row will not have.
    """
    out: list[str] = []
    for check in checks or []:
        for v in check.get("violations", []) or []:
            if isinstance(v, dict):
                out.append(v.get("detail") or v.get("reason") or json.dumps(v, sort_keys=True, default=str))
            else:
                out.append(str(v))
    return out


# --------------------------------------------------------------------------
# Text diffing
# --------------------------------------------------------------------------

def _reasoning(run: dict) -> str:
    """The model's reasoning, from wherever the trace recorded it."""
    for stage in reversed(run.get("pipeline_data", {}).get("stages", [])):
        thinking = (stage.get("metadata") or {}).get("thinking")
        if thinking:
            return thinking
    return ""


# Below this many lines, a line-level diff degenerates: two prose paragraphs
# differing by one word are "one line replaced by one line", reported as 0%
# similar with the whole text in both halves of the hunk. Word granularity is
# what a reader actually wants there.
_WORD_DIFF_MAX_LINES = 4


def _diff_text(baseline: str, other: str) -> dict[str, Any]:
    """Diff two blocks of text, reported as hunks plus a similarity ratio.

    Granularity adapts: lines for anything structured (code, multi-paragraph
    output), words for short prose. A single-line response diffed by line is a
    whole-block replace and tells the reader nothing about what changed.

    Returns ``identical`` rather than an empty hunk list when the two match, so
    the UI can say "identical" instead of rendering a blank panel that looks
    like a failure.
    """
    baseline, other = baseline or "", other or ""
    if baseline == other:
        return {
            "identical": True,
            "hunks": [],
            "similarity": 1.0,
            "granularity": "none",
            "baseline_empty": not baseline,
        }

    b_lines = baseline.splitlines()
    o_lines = other.splitlines()
    word_level = max(len(b_lines), len(o_lines)) <= _WORD_DIFF_MAX_LINES

    if word_level:
        # Keep the separators so a rebuilt hunk reads as the original text.
        b_parts = re.split(r"(\s+)", baseline)
        o_parts = re.split(r"(\s+)", other)
    else:
        b_parts, o_parts = b_lines, o_lines

    matcher = difflib.SequenceMatcher(None, b_parts, o_parts, autojunk=False)

    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if word_level:
            before = ["".join(b_parts[i1:i2])] if i2 > i1 else []
            after = ["".join(o_parts[j1:j2])] if j2 > j1 else []
        else:
            before, after = b_parts[i1:i2], o_parts[j1:j2]
        hunks.append({
            "op": tag,
            "baseline_start": i1,
            "other_start": j1,
            "baseline": _clip(before),
            "other": _clip(after),
        })

    return {
        "identical": False,
        "hunks": hunks,
        "similarity": round(matcher.ratio(), 3),
        "granularity": "word" if word_level else "line",
        "baseline_empty": not baseline,
    }


def _clip(lines: list[str]) -> list[str]:
    """Bound one hunk so a long reasoning block cannot bloat the payload."""
    out, budget = [], MAX_HUNK_CHARS
    for line in lines:
        if budget <= 0:
            out.append(f"... {len(lines) - len(out)} more line(s) not shown")
            break
        out.append(line[:budget])
        budget -= len(line)
    return out


def _unique(values) -> list[str]:
    """Order-preserving unique, dropping blanks."""
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
