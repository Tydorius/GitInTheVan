"""Per-run token, latency and endpoint accounting for debug traces.

The debug trace is the mutable dict stashed at ``body_json["_gitv_debug"]`` by
``debug.init_debug``. This module adds the ``run`` block to it: one record per
upstream LLM call, plus the derived totals the Debug and comparison views show.

Every function here no-ops when the trace is absent, exactly as
``debug.debug_capture`` does, so call sites need no ``if debug_on`` guard.

Token counts come from the upstream ``usage`` object when the provider returns
one, and from the char/4 estimator in ``budget`` when it does not. The two are
never mixed silently: each call carries ``tokens_source``, and a run whose calls
disagree reports ``mixed`` so the UI can badge it. Comparing an estimate against
a measurement without saying so would make the comparison view lie.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.budget import estimate_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)

# Purposes a recorded call may serve. Not enforced — a new caller may add one —
# but the comparison view groups by these, so reuse an existing value where it
# fits rather than inventing a synonym.
PURPOSE_MAIN = "main"
PURPOSE_MAP_STAGE = "map_stage"
PURPOSE_VERIFICATION = "verification_judge"
PURPOSE_SUMMARIZER = "summarizer"

TOKENS_UPSTREAM = "upstream"
TOKENS_ESTIMATED = "estimated"
TOKENS_MIXED = "mixed"


def _run_block(body_json: dict[str, Any]) -> dict[str, Any] | None:
    """Return the trace's ``run`` block, or None when debug is off.

    Tolerates a trace created by an older ``init_debug`` (schema 1) that has no
    ``run`` key, so a request in flight across an upgrade does not raise.
    """
    debug = body_json.get("_gitv_debug")
    if not isinstance(debug, dict):
        return None
    run = debug.get("run")
    if run is None:
        run = {"llm_calls": [], "cantrips": [], "totals": {}}
        debug["run"] = run
    return run


def record_llm_call(
    body_json: dict[str, Any],
    *,
    purpose: str,
    latency_ms: float,
    status_code: int,
    response_data: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    stage_index: int | None = None,
    endpoint_id: str = "",
    endpoint_name: str = "",
    provider: str = "",
    model_requested: str = "",
    model_resolved: str = "",
    failover_attempt: int = 0,
    error: str = "",
) -> None:
    """Record one upstream LLM call on the run.

    ``response_data`` supplies the upstream ``usage`` object when present.
    ``messages`` is the request that was actually sent, used only to estimate
    prompt tokens when the provider returns no usage.
    """
    run = _run_block(body_json)
    if run is None:
        return

    usage = extract_usage(response_data)
    if usage is None:
        usage = _estimate_usage(messages, response_data)

    run.setdefault("llm_calls", []).append({
        "purpose": purpose,
        "stage_index": stage_index,
        "endpoint_id": endpoint_id,
        "endpoint_name": endpoint_name,
        "provider": provider,
        "model_requested": model_requested,
        "model_resolved": model_resolved or _resolved_model(response_data) or model_requested,
        "failover_attempt": failover_attempt,
        "latency_ms": round(latency_ms, 1),
        "status_code": status_code,
        "error": error,
        **usage,
    })


def extract_usage(response_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read prompt/completion/reasoning tokens from an upstream response.

    Returns None when the response carries no usable ``usage`` object, so the
    caller can fall back to estimation and label it. A ``usage`` block present
    but empty of both token counts is treated as absent — some providers emit
    the key with nulls on streamed responses.
    """
    if not isinstance(response_data, dict):
        return None
    usage = response_data.get("usage")
    if not isinstance(usage, dict):
        return None

    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return None

    reasoning = 0
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        reasoning = int(details.get("reasoning_tokens") or 0)

    prompt = int(prompt or 0)
    completion = int(completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": int(usage.get("total_tokens") or (prompt + completion)),
        "tokens_source": TOKENS_UPSTREAM,
    }


def _estimate_usage(
    messages: list[dict[str, Any]] | None,
    response_data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Char/4 fallback for providers that return no usage object."""
    prompt = estimate_messages_tokens(messages) if messages else 0

    completion = 0
    reasoning = 0
    if isinstance(response_data, dict):
        for choice in response_data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, str) and content:
                completion += estimate_tokens(content)
            thinking = message.get("reasoning_content") or message.get("thinking")
            if isinstance(thinking, str) and thinking:
                reasoning += estimate_tokens(thinking)

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + completion,
        "tokens_source": TOKENS_ESTIMATED,
    }


def _resolved_model(response_data: dict[str, Any] | None) -> str:
    """The model the upstream says it actually served.

    This differs from the requested model more often than it looks: an endpoint
    with its own model overrides the body, a failover candidate may carry a
    different one, and a provider may resolve an alias to a dated snapshot.
    """
    if isinstance(response_data, dict):
        model = response_data.get("model")
        if isinstance(model, str):
            return model
    return ""


def record_cantrip(
    body_json: dict[str, Any],
    *,
    cantrip_id: str,
    name: str,
    position: str,
    triggered: bool,
    reason: str = "",
    tag: str = "",
    code: str = "",
    execution_order: int = 0,
    timeout_ms: int = 0,
    duration_ms: float = 0.0,
    output: dict[str, Any] | None = None,
    data_changes: dict[str, Any] | None = None,
    debug_logs: list[str] | None = None,
    error: str = "",
) -> None:
    """Record a cantrip that was considered, whether or not it ran.

    The set of cantrips that did *not* fire is as diagnostic as the set that
    did — "fired in run A, silent in run B" is the most common thing a user is
    trying to see, and it is invisible if only executions are recorded.

    This is the single source of truth for cantrip detail in a trace. The
    timeline stage for a cantrip pass stores only ids and joins against this,
    so the code and output are not serialized twice.
    """
    run = _run_block(body_json)
    if run is None:
        return

    entry: dict[str, Any] = {
        "id": cantrip_id,
        "name": name,
        "position": position,
        "triggered": triggered,
        "reason": reason,
        "tag": tag,
        "execution_order": execution_order,
        "timeout_ms": timeout_ms,
    }

    if triggered:
        entry.update({
            "duration_ms": round(duration_ms, 1),
            "code": code,
            "code_hash": code_fingerprint(code),
            "debug_logs": list(debug_logs or []),
            "error": error,
            "output": output or {},
            "fields_changed": sorted(k for k, v in (output or {}).items() if v),
            # Per-store deltas, so a comparison can show that a cantrip stopped
            # writing a key -- invisible if only the final store state is kept.
            "data_changes": {k: v for k, v in (data_changes or {}).items() if v},
        })

    run.setdefault("cantrips", []).append(entry)


def code_fingerprint(code: str) -> str:
    """Short content hash of cantrip source.

    Lets the comparison view say "same cantrip, different code" without diffing
    the whole body first, and lets an export identify a version without
    embedding it.
    """
    import hashlib

    if not code:
        return ""
    return hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()[:12]


def summarize_run(
    trace: dict[str, Any],
    *,
    total_latency_ms: float | None = None,
) -> dict[str, Any]:
    """Compute the totals shown in the metrics bar.

    ``overhead_ms`` is the interesting one: wall-clock minus the time spent
    waiting on upstreams, i.e. what this proxy's own pipeline costs. Nothing
    else in the product surfaces that number.
    """
    calls = trace.get("run", {}).get("llm_calls", []) if isinstance(trace, dict) else []

    prompt = sum(int(c.get("prompt_tokens") or 0) for c in calls)
    completion = sum(int(c.get("completion_tokens") or 0) for c in calls)
    reasoning = sum(int(c.get("reasoning_tokens") or 0) for c in calls)
    llm_latency = sum(float(c.get("latency_ms") or 0.0) for c in calls)

    sources = {c.get("tokens_source") for c in calls if c.get("tokens_source")}
    if not sources:
        tokens_source = TOKENS_ESTIMATED
    elif len(sources) == 1:
        tokens_source = sources.pop()
    else:
        tokens_source = TOKENS_MIXED

    totals: dict[str, Any] = {
        "llm_call_count": len(calls),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + completion,
        "tokens_source": tokens_source,
        "llm_latency_ms": round(llm_latency, 1),
        "injected_tokens": _injected_tokens(trace),
    }

    if total_latency_ms is not None:
        totals["total_latency_ms"] = round(total_latency_ms, 1)
        # Clamp at zero: concurrent stage calls can sum past wall-clock, and a
        # negative "overhead" would read as a bug rather than as parallelism.
        totals["overhead_ms"] = round(max(0.0, total_latency_ms - llm_latency), 1)

    if llm_latency > 0 and completion > 0:
        totals["tokens_per_second"] = round(completion / (llm_latency / 1000.0), 2)

    original = _original_prompt_tokens(trace)
    if original > 0 and totals["injected_tokens"] > 0:
        totals["injection_overhead_pct"] = round(
            totals["injected_tokens"] / original * 100.0, 1
        )

    return totals


def _original_prompt_tokens(trace: dict[str, Any]) -> int:
    """Estimated size of the messages as the client sent them."""
    import json

    raw = trace.get("original_messages") if isinstance(trace, dict) else None
    if not raw:
        return 0
    try:
        messages = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return 0
    if not isinstance(messages, list):
        return 0
    return estimate_messages_tokens(messages)


def _injected_tokens(trace: dict[str, Any]) -> int:
    """Tokens the pipeline added to the prompt, before it reached the upstream.

    Measured as the last request-side stage's message size minus the original,
    rather than by summing per-stage deltas, because summarization *removes*
    tokens and the two must net out.
    """
    import json

    if not isinstance(trace, dict):
        return 0

    final_snapshot = None
    for stage in reversed(trace.get("stages", []) or []):
        after = stage.get("messages_after")
        if after:
            final_snapshot = after
            break
    if not final_snapshot:
        return 0

    try:
        messages = json.loads(final_snapshot)
    except (ValueError, TypeError):
        return 0
    if not isinstance(messages, list):
        return 0

    return max(0, estimate_messages_tokens(messages) - _original_prompt_tokens(trace))


def stage_injection_costs(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-stage token delta, largest first.

    Answers "why is my prompt twelve thousand tokens" by naming the stages that
    made it so. Stages that removed tokens (summarization) appear with a
    negative delta rather than being dropped.
    """
    import json

    costs: list[dict[str, Any]] = []
    for stage in trace.get("stages", []) or []:
        before, after = stage.get("messages_before"), stage.get("messages_after")
        if not before or not after or before == after:
            continue
        try:
            delta = estimate_messages_tokens(json.loads(after)) - estimate_messages_tokens(
                json.loads(before)
            )
        except (ValueError, TypeError):
            continue
        if delta == 0:
            continue
        costs.append({
            "name": stage.get("name", ""),
            "label": stage.get("label", ""),
            "item_id": stage.get("item_id"),
            "item_name": stage.get("item_name"),
            "delta_tokens": delta,
        })

    costs.sort(key=lambda c: abs(c["delta_tokens"]), reverse=True)
    return costs
