"""Render a debug run, or a comparison of runs, as JSON or Markdown.

Rendering is server-side so the screen, the Markdown export and the JSON export
share one implementation and the Markdown is testable. JSON export is lossless:
the whole serialized exchange including the run block, so it can be re-read
later, diffed offline, or attached to a bug report.

There is no Markdown library in this project and this does not add one --
``packs.py`` builds its README from a plain format string for the same reason.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

JSON_MEDIA = "application/json"
MARKDOWN_MEDIA = "text/markdown; charset=utf-8"

# Export format version, independent of the trace schema. Bump when the shape of
# an exported document changes in a way an external reader would notice.
EXPORT_VERSION = 1


def render_exchange(exchange: dict[str, Any], fmt: str) -> tuple[str, str, str]:
    """Render one run. Returns (content, media_type, filename)."""
    stem = _filename_stem(exchange)
    if fmt == "json":
        payload = {
            "export_version": EXPORT_VERSION,
            "kind": "debug_run",
            "run": exchange,
        }
        return json.dumps(payload, indent=2, default=str), JSON_MEDIA, f"{stem}.json"
    return _exchange_markdown(exchange), MARKDOWN_MEDIA, f"{stem}.md"


def render_comparison(
    runs: list[dict[str, Any]], result: dict[str, Any], fmt: str
) -> tuple[str, str, str]:
    """Render a comparison. Returns (content, media_type, filename)."""
    if fmt == "json":
        payload = {
            "export_version": EXPORT_VERSION,
            "kind": "debug_comparison",
            "comparison": result,
            "runs": {r["id"]: r for r in runs},
        }
        return json.dumps(payload, indent=2, default=str), JSON_MEDIA, "gitv-comparison.json"
    return _comparison_markdown(runs, result), MARKDOWN_MEDIA, "gitv-comparison.md"


# --------------------------------------------------------------------------
# Single run
# --------------------------------------------------------------------------

def _exchange_markdown(e: dict[str, Any]) -> str:
    pipeline = e.get("pipeline_data", {})
    run = pipeline.get("run", {})
    out: list[str] = []

    title = e.get("label") or "Debug Run"
    out.append(f"# {title}")
    out.append("")
    out.append(f"- **Run ID:** `{e.get('id', '')}`")
    out.append(f"- **When:** {e.get('created_at', '')}")
    out.append(f"- **Model requested:** `{e.get('model', '') or '(none)'}`")
    if run.get("source") == "replay":
        out.append(f"- **Source:** replay of `{run.get('replay_of', '')}`")
    if pipeline.get("tags"):
        out.append(f"- **Tags:** {', '.join(f'`{t}`' for t in pipeline['tags'])}")
    if pipeline.get("truncated"):
        out.append(f"- **Truncated:** {pipeline.get('truncated_reason', 'yes')}")
    out.append("")

    out.extend(_metrics_section(run.get("totals", {})))
    out.extend(_calls_section(run.get("llm_calls", [])))
    out.extend(_cantrips_section(run.get("cantrips", [])))
    out.extend(_timeline_section(pipeline.get("stages", [])))
    out.extend(_verification_section(e.get("verification_data") or {}))

    out.append("## Final Response")
    out.append("")
    out.append(_fence(e.get("response_content", "")))
    out.append("")
    return "\n".join(out)


def _metrics_section(totals: dict[str, Any]) -> list[str]:
    if not totals:
        return []
    out = ["## Metrics", ""]

    source = totals.get("tokens_source", "")
    if source == "estimated":
        out.append("> Token counts are **estimated** — the upstream returned no usage data.")
        out.append("")
    elif source == "mixed":
        out.append(
            "> Token counts are **mixed**: some calls reported usage and some were "
            "estimated. Treat totals as approximate."
        )
        out.append("")

    rows = [
        ("Prompt tokens", totals.get("prompt_tokens")),
        ("Completion tokens", totals.get("completion_tokens")),
        ("Reasoning tokens", totals.get("reasoning_tokens")),
        ("Injected tokens", totals.get("injected_tokens")),
        ("Injection overhead", _pct(totals.get("injection_overhead_pct"))),
        ("Upstream calls", totals.get("llm_call_count")),
        ("Upstream latency", _ms(totals.get("llm_latency_ms"))),
        ("Total latency", _ms(totals.get("total_latency_ms"))),
        ("Pipeline overhead", _ms(totals.get("overhead_ms"))),
        ("Tokens/second", totals.get("tokens_per_second")),
    ]
    out.append("| Metric | Value |")
    out.append("|---|---|")
    for name, value in rows:
        if value not in (None, ""):
            out.append(f"| {name} | {value} |")
    out.append("")
    return out


def _calls_section(calls: list[dict]) -> list[str]:
    if not calls:
        return []
    out = ["## Upstream Calls", "", "| # | Purpose | Endpoint | Model served | Latency | Tokens | Status |", "|---|---|---|---|---|---|---|"]
    for i, c in enumerate(calls, 1):
        tokens = f"{c.get('prompt_tokens', 0)}+{c.get('completion_tokens', 0)}"
        if c.get("tokens_source") == "estimated":
            tokens += " (est)"
        status = str(c.get("status_code", ""))
        if c.get("error"):
            status += f" — {c['error'][:80]}"
        out.append(
            f"| {i} | {c.get('purpose', '')} | {c.get('endpoint_name', '') or '—'} "
            f"| `{c.get('model_resolved', '') or '—'}` | {_ms(c.get('latency_ms'))} "
            f"| {tokens} | {status} |"
        )
    out.append("")
    return out


def _cantrips_section(cantrips: list[dict]) -> list[str]:
    if not cantrips:
        return []
    fired = [c for c in cantrips if c.get("triggered")]
    silent = [c for c in cantrips if not c.get("triggered")]

    out = ["## Cantrips", ""]
    if fired:
        out.append("| Cantrip | Position | Duration | Changed | Error |")
        out.append("|---|---|---|---|---|")
        for c in fired:
            out.append(
                f"| {c.get('name', '')} | {c.get('position', '')} "
                f"| {_ms(c.get('duration_ms'))} "
                f"| {', '.join(c.get('fields_changed', [])) or '—'} "
                f"| {c.get('error', '') or '—'} |"
            )
        out.append("")
    if silent:
        # Recorded because a cantrip that did not fire is often the finding.
        out.append("**Did not fire:** " + ", ".join(
            f"{c.get('name', '')} ({c.get('reason', 'no reason recorded')})" for c in silent
        ))
        out.append("")

    logs = [(c["name"], c["debug_logs"]) for c in fired if c.get("debug_logs")]
    if logs:
        out.append("### Cantrip Logs")
        out.append("")
        for name, entries in logs:
            out.append(f"**{name}**")
            out.append(_fence("\n".join(entries)))
            out.append("")
    return out


def _timeline_section(stages: list[dict]) -> list[str]:
    if not stages:
        return []
    out = ["## Pipeline Timeline", ""]
    for i, s in enumerate(stages, 1):
        out.append(f"### {i}. {s.get('label', s.get('name', ''))}")
        out.append("")
        if s.get("detail"):
            out.append(s["detail"])
            out.append("")
        if s.get("item_name"):
            out.append(f"- **Object:** {s['item_name']} (`{s.get('item_id', '')}`)")
        if s.get("setting"):
            out.append(f"- **Setting:** `{s['setting']}` = `{s.get('setting_value')}`")

        meta = s.get("metadata") or {}
        thinking = meta.get("thinking")
        if thinking:
            out.append("")
            out.append("**Reasoning**")
            out.append("")
            # Whole, never clipped. Preserving reasoning is the point of the
            # export; a reader comparing two runs needs all of it.
            out.append(_fence(thinking))
        remainder = {k: v for k, v in meta.items() if k != "thinking"}
        if remainder:
            out.append("")
            out.append("<details><summary>Metadata</summary>")
            out.append("")
            out.append(_fence(json.dumps(remainder, indent=2, default=str), "json"))
            out.append("")
            out.append("</details>")

        if s.get("content_after"):
            out.append("")
            out.append("**Content after**")
            out.append("")
            out.append(_fence(s["content_after"]))
        out.append("")
    return out


def _verification_section(v: dict[str, Any]) -> list[str]:
    if not v:
        return []
    out = ["## Verification", ""]
    out.append(f"- **Approved:** {'yes' if v.get('approved') else 'no'}")
    out.append(f"- **Retries used:** {v.get('retries_used', 0)}")
    out.append("")
    for i, check in enumerate(v.get("check_history", []), 1):
        out.append(f"**Check {i}: {'PASS' if check.get('approved') else 'FAIL'}**")
        out.append("")
        for violation in check.get("violations", []) or []:
            if isinstance(violation, dict):
                text = violation.get("detail") or violation.get("reason") or json.dumps(violation, default=str)
            else:
                text = str(violation)
            out.append(f"- {text}")
        if check.get("thinking"):
            out.append("")
            out.append(_fence(check["thinking"]))
        out.append("")
    return out


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _comparison_markdown(runs: list[dict], result: dict[str, Any]) -> str:
    by_id = {r["id"]: r for r in runs}
    order = result.get("order", [])
    baseline_id = result.get("baseline_id", "")
    summaries = result.get("runs", {})

    out = ["# Debug Run Comparison", ""]
    out.append(f"Baseline: **{_name(summaries.get(baseline_id, {}), by_id.get(baseline_id, {}))}**")
    out.append("")

    out.append("## Runs")
    out.append("")
    out.append("| | Run | When | Model | Endpoint | Source |")
    out.append("|---|---|---|---|---|---|")
    for i, rid in enumerate(order):
        s = summaries.get(rid, {})
        marker = "**baseline**" if rid == baseline_id else str(i + 1)
        out.append(
            f"| {marker} | {_name(s, by_id.get(rid, {}))} | {s.get('created_at', '')} "
            f"| `{'`, `'.join(s.get('models_resolved', [])) or s.get('model_requested', '—')}` "
            f"| {', '.join(s.get('endpoints', [])) or '—'} | {s.get('source', 'live')} |"
        )
    out.append("")

    out.extend(_comparison_metrics(order, baseline_id, summaries, result, by_id))

    for rid in order:
        if rid == baseline_id:
            continue
        diff = result.get("diffs", {}).get(rid, {})
        out.append(f"## {_name(summaries.get(rid, {}), by_id.get(rid, {}))} vs baseline")
        out.append("")
        out.extend(_comparison_identity(diff.get("identity", {})))
        out.extend(_comparison_cantrips(diff.get("cantrips", {})))
        out.extend(_comparison_injections("Lorebook entries", diff.get("lorebooks", {})))
        out.extend(_comparison_injections("Skills", diff.get("skills", {})))
        out.extend(_comparison_stages(diff.get("stages", {})))
        out.extend(_comparison_verification(diff.get("verification", {})))
        out.extend(_comparison_text("Response", diff.get("response", {})))
        out.extend(_comparison_text("Reasoning", diff.get("reasoning", {})))

    return "\n".join(out)


def _comparison_metrics(order, baseline_id, summaries, result, by_id) -> list[str]:
    keys = [
        ("total_tokens", "Total tokens"),
        ("prompt_tokens", "Prompt tokens"),
        ("completion_tokens", "Completion tokens"),
        ("injected_tokens", "Injected tokens"),
        ("llm_latency_ms", "Upstream latency"),
        ("overhead_ms", "Pipeline overhead"),
        ("tokens_per_second", "Tokens/second"),
    ]
    out = ["## Metrics", ""]

    incomparable = [
        rid for rid in order
        if rid != baseline_id
        and not result.get("diffs", {}).get(rid, {}).get("metrics", {}).get("_comparable", True)
    ]
    if incomparable:
        out.append(
            "> One or more runs mix measured and estimated token counts. "
            "Token deltas below are indicative, not exact."
        )
        out.append("")

    header = "| Metric | " + " | ".join(
        _name(summaries.get(rid, {}), by_id.get(rid, {})) + (" (baseline)" if rid == baseline_id else "")
        for rid in order
    ) + " |"
    out.append(header)
    out.append("|---" * (len(order) + 1) + "|")

    for key, label in keys:
        cells = []
        for rid in order:
            totals = summaries.get(rid, {}).get("totals", {})
            value = totals.get(key)
            if value is None:
                cells.append("—")
                continue
            text = _ms(value) if key.endswith("_ms") else str(value)
            if rid != baseline_id:
                metric = result.get("diffs", {}).get(rid, {}).get("metrics", {}).get(key, {})
                delta = metric.get("delta")
                if delta:
                    text += f" ({'+' if delta > 0 else ''}{round(delta, 1)})"
            cells.append(text)
        if any(c != "—" for c in cells):
            out.append(f"| {label} | " + " | ".join(cells) + " |")
    out.append("")
    return out


def _comparison_identity(identity: dict) -> list[str]:
    if identity.get("same", True):
        return []
    out = ["### Configuration", ""]
    for field, values in identity.get("changed", {}).items():
        out.append(f"- **{field}**: `{values['baseline']}` → `{values['other']}`")
    out.append("")
    return out


def _comparison_cantrips(c: dict) -> list[str]:
    if not c or c.get("same", True):
        return []
    out = ["### Cantrips", ""]
    for item in c.get("added", []):
        out.append(f"- **Added:** {item['name']}")
    for item in c.get("removed", []):
        out.append(f"- **Removed:** {item['name']}")
    for item in c.get("trigger_changed", []):
        was = "fired" if item["baseline_triggered"] else "silent"
        now = "fired" if item["other_triggered"] else "silent"
        reason = item["other_reason"] or item["baseline_reason"]
        out.append(f"- **{item['name']}**: {was} → {now}" + (f" ({reason})" if reason else ""))
    for item in c.get("code_changed", []):
        out.append(f"- **{item['name']}**: code changed (`{item['baseline_hash']}` → `{item['other_hash']}`)")
        out.extend(_inline_diff(item.get("diff", {})))
    for item in c.get("output_changed", []):
        out.append(f"- **{item['name']}**: output changed ({', '.join(item['fields'])})")
    out.append("")
    return out


def _comparison_injections(title: str, d: dict) -> list[str]:
    if not d or d.get("same", True):
        return []
    out = [f"### {title}", ""]
    for item in d.get("added", []):
        where = f" (from {item['lorebook_name']})" if item.get("lorebook_name") else ""
        out.append(f"- **Added:** {item['name']}{where} — {item.get('tokens', 0)} tokens")
    for item in d.get("removed", []):
        where = f" (from {item['lorebook_name']})" if item.get("lorebook_name") else ""
        out.append(f"- **Removed:** {item['name']}{where} — {item.get('tokens', 0)} tokens")
    out.append(f"- Tokens: {d.get('baseline_tokens', 0)} → {d.get('other_tokens', 0)}")
    out.append("")
    return out


def _comparison_stages(d: dict) -> list[str]:
    if not d or d.get("same", True):
        return []
    out = ["### Pipeline stages", ""]
    for s in d.get("only_in_baseline", []):
        out.append(f"- **Only in baseline:** {s['label']}")
    for s in d.get("only_in_other", []):
        out.append(f"- **Only in this run:** {s['label']}")
    for s in d.get("changed", []):
        out.append(f"- **{s['label']}**: {s['baseline_detail']} → {s['other_detail']}")
    out.append("")
    return out


def _comparison_verification(d: dict) -> list[str]:
    if not d or not d.get("ran") or d.get("same", True):
        return []
    out = ["### Verification", ""]
    if d.get("approval_changed"):
        out.append(
            f"- **Result:** {'PASS' if d.get('baseline_approved') else 'FAIL'} → "
            f"{'PASS' if d.get('other_approved') else 'FAIL'}"
        )
    if d.get("baseline_retries") != d.get("other_retries"):
        out.append(f"- **Retries:** {d.get('baseline_retries')} → {d.get('other_retries')}")

    gained = [v for v in d.get("other_violations", []) if v not in d.get("baseline_violations", [])]
    fixed = [v for v in d.get("baseline_violations", []) if v not in d.get("other_violations", [])]
    for v in gained:
        out.append(f"- **New violation:** {v}")
    for v in fixed:
        out.append(f"- **Resolved:** {v}")
    out.append("")
    return out


def _comparison_text(title: str, d: dict) -> list[str]:
    if not d or d.get("identical", True):
        return []
    out = [f"### {title}", "", f"Similarity: {int(d.get('similarity', 0) * 100)}%", ""]
    out.extend(_inline_diff(d))
    out.append("")
    return out


def _inline_diff(d: dict) -> list[str]:
    """Render diff hunks as a fenced unified-style block."""
    if not d or d.get("identical", True) or not d.get("hunks"):
        return []
    lines = []
    for hunk in d["hunks"]:
        for line in hunk.get("baseline", []):
            lines.append(f"- {line}")
        for line in hunk.get("other", []):
            lines.append(f"+ {line}")
    return ["", _fence("\n".join(lines), "diff"), ""]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _filename_stem(exchange: dict[str, Any]) -> str:
    """A filename a user can recognise in a downloads folder.

    Built from the run's label when it has one, since a directory of files all
    called gitv-debug-<uuid> is no more use than the uuid alone.
    """
    label = (exchange.get("label") or "").strip()
    if label:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()[:48]
        if slug:
            return f"gitv-debug-{slug}"
    return f"gitv-debug-{exchange.get('id', 'run')[:8]}"


def _name(summary: dict, exchange: dict) -> str:
    return summary.get("label") or exchange.get("label") or (summary.get("id") or exchange.get("id", ""))[:8]


def _ms(value: Any) -> str:
    if value is None:
        return ""
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{round(value)}ms"


def _pct(value: Any) -> str:
    return "" if value is None else f"{value}%"


def _fence(text: str, lang: str = "") -> str:
    """Fence a block, widening the fence if the content contains backticks.

    Cantrip code and LLM output routinely contain fenced blocks of their own; a
    plain three-backtick fence would be closed early by them and the rest of the
    document would render as prose.
    """
    text = text or ""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"
