"""Persistent token metrics: per-object prompt deltas and per-LLM-call rows.

The accumulator follows the debug-trace pattern: a mutable dict stashed at
``body_json["_gitv_metrics"]`` and read by every recording helper, so call
sites need no guard. Unlike the trace it is created for **every** authenticated
pipeline request, not only with debug mode on -- these tables are the product
feature, not a diagnostic.

Rows accumulate in memory through the pipeline; ``flush_metrics`` commits them
in one batch at the end. A metrics failure must never fail the request: the
flush swallows and logs, the same posture as the debug captures.

The LLM-call rows ride the existing ``debug_metrics.record_llm_call`` hook
(repo rule 16 already routes every upstream call through it), so this module
adds no new instrumentation point for them -- only the prompt-object
measurement points in the pipeline are new.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.models.metrics import LlmCallMetric, PromptObjectMetric
from app.models.skill import Skill
from app.services.debug import run_source

logger = logging.getLogger(__name__)

# Default age cap for both tables. Admin-configurable; 0/negative is clamped
# to 1 day at the router, the same floor the other retention caps use.
DEFAULT_RETENTION_DAYS = 30
RETENTION_INTERVAL_SECONDS = 6 * 60 * 60

# debug_metrics tags usage "upstream"/"estimated"; the table stores the
# spec's "usage"/"estimate". One vocabulary per surface, mapped here so the
# two can never drift apart silently.
_TOKENS_SOURCE_MAP = {"upstream": "usage", "estimated": "estimate"}

# Types whose object_id is a real row id resolvable to a display name.
_NAMED_TYPES = ("lorebook_entry", "cantrip", "skill", "sample")


def init_metrics(body_json: dict[str, Any], user_id: str) -> None:
    """Create the accumulator. Idempotent; safe to call unconditionally."""
    if "_gitv_metrics" not in body_json:
        body_json["_gitv_metrics"] = {
            "user_id": user_id,
            "chat_id": "",
            "retry": False,
            "stage_ref": None,
            "rows_prompt": [],
            "rows_llm": [],
        }


def _metrics(body_json: dict[str, Any] | None) -> dict[str, Any] | None:
    m = (body_json or {}).get("_gitv_metrics")
    return m if isinstance(m, dict) else None


def set_metrics_chat(body_json: dict[str, Any], chat_id: str) -> None:
    m = _metrics(body_json)
    if m is not None:
        m["chat_id"] = chat_id or ""


def set_metrics_retry(body_json: dict[str, Any], value: bool) -> None:
    """Flag rows recorded from here on as navigator-retry cycle output."""
    m = _metrics(body_json)
    if m is not None:
        m["retry"] = bool(value)


def set_metrics_stage_ref(body_json: dict[str, Any], map_id: str, stage_index: int) -> None:
    """Attribute prompt-object rows recorded from here on to one map stage."""
    m = _metrics(body_json)
    if m is not None:
        m["stage_ref"] = {"map_id": map_id, "stage_index": stage_index}


def clear_metrics_stage_ref(body_json: dict[str, Any]) -> None:
    m = _metrics(body_json)
    if m is not None:
        m["stage_ref"] = None


def record_prompt_object(
    body_json: dict[str, Any],
    object_type: str,
    object_id: str,
    pre_tokens: int,
    post_tokens: int,
    parent_id: str = "",
) -> None:
    """Record one prompt-mutating object's token delta for this request.

    ``difference`` is post minus pre: positive for injections (lorebook
    entries, skills), possibly negative for cantrips and summarizers that
    replace or compress existing content.
    """
    m = _metrics(body_json)
    if m is None:
        return
    if not parent_id and m.get("stage_ref"):
        parent_id = m["stage_ref"].get("map_id", "")
    m["rows_prompt"].append({
        "user_id": m["user_id"],
        "chat_id": m.get("chat_id", ""),
        "object_type": object_type,
        "object_id": str(object_id)[:64],
        "parent_id": str(parent_id or "")[:64],
        "pre_tokens": int(pre_tokens),
        "post_tokens": int(post_tokens),
        "difference": int(post_tokens) - int(pre_tokens),
        "context": run_source(body_json),
        "is_retry": bool(m.get("retry")),
    })


def record_llm_metric(
    body_json: dict[str, Any],
    *,
    purpose: str,
    latency_ms: float,
    status_code: int,
    usage: dict[str, Any] | None,
    endpoint_id: str = "",
    endpoint_name: str = "",
    model: str = "",
    failover_attempt: int = 0,
    rule_id: str = "",
) -> None:
    """Record one upstream LLM call. Called from ``debug_metrics.record_llm_call``
    with the usage dict that hook already resolved (upstream when present,
    estimate otherwise) -- the single source of both token figures."""
    m = _metrics(body_json)
    if m is None:
        return
    usage = usage or {}
    m["rows_llm"].append({
        "user_id": m["user_id"],
        "chat_id": m.get("chat_id", ""),
        "purpose": purpose,
        "endpoint_id": endpoint_id or "",
        "endpoint_name": (endpoint_name or "")[:128],
        "model": (model or "")[:128],
        "status_code": status_code,
        "latency_ms": round(float(latency_ms), 1),
        "failover_attempt": int(failover_attempt),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "tokens_source": _TOKENS_SOURCE_MAP.get(usage.get("tokens_source", ""), ""),
        "rule_id": rule_id or "",
        "context": run_source(body_json),
        "is_retry": bool(m.get("retry")),
    })


async def flush_metrics(body_json: dict[str, Any] | None) -> None:
    """Commit the request's accumulated rows in one batch.

    Idempotent: rows are drained whether the commit succeeds or not, so the
    per-return-point calls in the pipeline can never double-insert. Failure is
    swallowed and logged -- metrics must never fail the request.
    """
    m = _metrics(body_json)
    if m is None:
        return
    prompt_rows = m["rows_prompt"]
    llm_rows = m["rows_llm"]
    m["rows_prompt"] = []
    m["rows_llm"] = []
    if not prompt_rows and not llm_rows:
        return
    try:
        async with async_session() as db:
            if prompt_rows:
                db.add_all([PromptObjectMetric(**r) for r in prompt_rows])
            if llm_rows:
                db.add_all([LlmCallMetric(**r) for r in llm_rows])
            await db.commit()
    except Exception:
        logger.exception(
            "Metrics flush failed: %d prompt rows, %d llm rows dropped",
            len(prompt_rows), len(llm_rows),
        )


async def prune_metrics(retention_days: int) -> int:
    """Delete rows older than the retention cap from both tables."""
    try:
        days = max(1, int(retention_days))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    cutoff = datetime.now(UTC) - timedelta(days=days)
    removed = 0
    async with async_session() as db:
        r1 = await db.execute(delete(PromptObjectMetric).where(PromptObjectMetric.created_at < cutoff))
        r2 = await db.execute(delete(LlmCallMetric).where(LlmCallMetric.created_at < cutoff))
        await db.commit()
        removed = int(r1.rowcount or 0) + int(r2.rowcount or 0)
    if removed:
        logger.info("Metrics retention: pruned %d row(s) older than %d day(s)", removed, days)
    return removed


async def metrics_retention_loop() -> None:
    """Prune on a fixed interval, re-reading the admin setting each cycle.

    Same shape as the backup scheduler: work in try/except-log so one failure
    never kills the loop, then sleep. The first pass runs immediately at
    startup, which covers the prune-on-startup case.
    """
    from app.services.admin import get_admin_settings

    while True:
        try:
            admin_settings = await get_admin_settings()
            await prune_metrics(admin_settings.metrics_retention_days)
        except Exception:
            logger.exception("Metrics retention cycle failed")
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Aggregation (Metrics page)
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


async def _object_names(rows: list[PromptObjectMetric]) -> dict[str, str]:
    """Best-effort display names for the object ids in the window.

    Rows outlive the objects they describe (endpoint deletions, lorebook
    edits), so a missing name is normal and renders as the raw id.
    """
    ids_by_type: dict[str, set[str]] = {}
    parent_books: set[str] = set()
    for r in rows:
        if r.object_type in _NAMED_TYPES and r.object_id:
            ids_by_type.setdefault(r.object_type, set()).add(r.object_id)
        if r.object_type == "lorebook_entry" and r.parent_id:
            parent_books.add(r.parent_id)

    names: dict[str, str] = {}
    async with async_session() as db:
        if ids_by_type.get("lorebook_entry"):
            res = await db.execute(
                select(LorebookEntry.id, LorebookEntry.name)
                .where(LorebookEntry.id.in_(ids_by_type["lorebook_entry"]))
            )
            names.update({row_id: name or "" for row_id, name in res.all()})
        if ids_by_type.get("cantrip"):
            res = await db.execute(
                select(Cantrip.id, Cantrip.name).where(Cantrip.id.in_(ids_by_type["cantrip"]))
            )
            names.update({row_id: name or "" for row_id, name in res.all()})
        for type_key in ("skill", "sample"):
            if ids_by_type.get(type_key):
                res = await db.execute(
                    select(Skill.id, Skill.name).where(Skill.id.in_(ids_by_type[type_key]))
                )
                names.update({row_id: name or "" for row_id, name in res.all()})
        if parent_books:
            res = await db.execute(
                select(Lorebook.id, Lorebook.name).where(Lorebook.id.in_(parent_books))
            )
            names.update({row_id: name or "" for row_id, name in res.all()})
    return names


def _row_dict(r: PromptObjectMetric | LlmCallMetric) -> dict[str, Any]:
    return {
        "id": r.id,
        "chat_id": r.chat_id,
        "context": r.context,
        "is_retry": r.is_retry,
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


async def metrics_summary(
    user_id: str,
    days: int = 30,
    chat_id: str = "",
    include_flagged: bool = False,
) -> dict[str, Any]:
    """Aggregate both tables over a window, in Python.

    The row counts per user per window are small (a request produces at most a
    few dozen rows; retention caps the window), so fetching the rows and
    grouping here keeps median/mean trivially portable across SQLite,
    PostgreSQL and MariaDB -- none of which agree on a percentile function.
    """
    days = max(1, min(int(days), 365))
    cutoff = datetime.now(UTC) - timedelta(days=days)

    prompt_where = [PromptObjectMetric.user_id == user_id, PromptObjectMetric.created_at >= cutoff]
    llm_where = [LlmCallMetric.user_id == user_id, LlmCallMetric.created_at >= cutoff]
    if chat_id:
        prompt_where.append(PromptObjectMetric.chat_id == chat_id)
        llm_where.append(LlmCallMetric.chat_id == chat_id)
    if not include_flagged:
        prompt_where.append(PromptObjectMetric.context == "live")
        llm_where.append(LlmCallMetric.context == "live")

    async with async_session() as db:
        prompt_rows = (
            (await db.execute(select(PromptObjectMetric).where(*prompt_where)
                .order_by(PromptObjectMetric.created_at.desc()))).scalars().all()
        )
        llm_rows = (
            (await db.execute(select(LlmCallMetric).where(*llm_where)
                .order_by(LlmCallMetric.created_at.desc()))).scalars().all()
        )
        chats_res = await db.execute(
            select(LlmCallMetric.chat_id).where(
                LlmCallMetric.user_id == user_id, LlmCallMetric.chat_id != ""
            ).distinct().limit(200)
        )
        chats = [c for (c,) in chats_res.all()]

    # Prompt objects: one group per (type, object, parent) plus per-type totals.
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    type_totals: dict[str, dict[str, Any]] = {}
    for r in prompt_rows:
        key = (r.object_type, r.object_id, r.parent_id or "")
        g = groups.setdefault(key, {
            "object_type": r.object_type, "object_id": r.object_id, "parent_id": r.parent_id or "",
            "fires": 0, "sum_difference": 0, "avg_difference": 0.0, "differences": [],
        })
        g["fires"] += 1
        g["sum_difference"] += r.difference
        g["differences"].append(r.difference)

        t = type_totals.setdefault(r.object_type, {
            "object_type": r.object_type, "fires": 0, "sum_difference": 0,
        })
        t["fires"] += 1
        t["sum_difference"] += r.difference

    names = await _object_names(prompt_rows)
    prompt_objects = []
    for g in groups.values():
        diffs = g.pop("differences")
        g["avg_difference"] = _mean([float(d) for d in diffs])
        g["object_name"] = names.get(g["object_id"], "")
        g["parent_name"] = names.get(g["parent_id"], "") if g["parent_id"] else ""
        prompt_objects.append(g)
    prompt_objects.sort(key=lambda g: (-abs(g["sum_difference"]), -g["fires"]))

    # LLM calls: one group per (purpose, endpoint, model, rule).
    llm_groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for r in llm_rows:
        key = (r.purpose, r.endpoint_id, r.endpoint_name, r.model, r.rule_id)
        g = llm_groups.setdefault(key, {
            "purpose": r.purpose, "endpoint_id": r.endpoint_id, "endpoint_name": r.endpoint_name,
            "model": r.model, "rule_id": r.rule_id,
            "calls": 0, "ok": 0, "errors": 0, "retries": 0, "failovers": 0,
            "latencies": [], "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "tokens_source_counts": {"usage": 0, "estimate": 0},
        })
        g["calls"] += 1
        if r.status_code == 200:
            g["ok"] += 1
        else:
            g["errors"] += 1
        if r.is_retry:
            g["retries"] += 1
        if r.failover_attempt and r.failover_attempt > 0:
            g["failovers"] += 1
        if r.latency_ms is not None:
            g["latencies"].append(float(r.latency_ms))
        if r.tokens_source in ("usage", "estimate"):
            g["prompt_tokens"] += r.prompt_tokens or 0
            g["completion_tokens"] += r.completion_tokens or 0
            g["reasoning_tokens"] += r.reasoning_tokens or 0
            g["tokens_source_counts"][r.tokens_source] += 1

    rule_names = await _rule_names(llm_rows)
    llm_calls = []
    for g in llm_groups.values():
        lats = g.pop("latencies")
        g["mean_latency_ms"] = _mean(lats)
        g["median_latency_ms"] = _median(lats)
        g["min_latency_ms"] = round(min(lats), 1) if lats else 0.0
        g["max_latency_ms"] = round(max(lats), 1) if lats else 0.0
        g["rule_name"] = rule_names.get(g["rule_id"], "") if g["rule_id"] else ""
        llm_calls.append(g)
    llm_calls.sort(key=lambda g: (-g["calls"], g["purpose"]))

    return {
        "window_days": days,
        "chat_id": chat_id,
        "include_flagged": include_flagged,
        "chats": chats,
        "prompt_objects": prompt_objects,
        "prompt_type_totals": sorted(type_totals.values(), key=lambda t: -t["fires"]),
        "llm_calls": llm_calls,
    }


async def _rule_names(rows: list[LlmCallMetric]) -> dict[str, str]:
    rule_ids = {r.rule_id for r in rows if r.rule_id}
    if not rule_ids:
        return {}
    from app.models.verification import VerificationRule

    async with async_session() as db:
        res = await db.execute(
            select(VerificationRule.id, VerificationRule.name).where(VerificationRule.id.in_(rule_ids))
        )
        return {row_id: name or "" for row_id, name in res.all()}


async def list_prompt_object_metrics(
    user_id: str, limit: int = 100, offset: int = 0, chat_id: str = "",
) -> tuple[list[dict[str, Any]], int]:
    where = [PromptObjectMetric.user_id == user_id]
    if chat_id:
        where.append(PromptObjectMetric.chat_id == chat_id)
    async with async_session() as db:
        total = (await db.execute(select(func.count()).select_from(PromptObjectMetric).where(*where))).scalar_one()
        rows = (
            (await db.execute(
                select(PromptObjectMetric).where(*where)
                .order_by(PromptObjectMetric.created_at.desc())
                .limit(limit).offset(offset)
            )).scalars().all()
        )
    return [
        {**_row_dict(r),
         "object_type": r.object_type, "object_id": r.object_id, "parent_id": r.parent_id,
         "pre_tokens": r.pre_tokens, "post_tokens": r.post_tokens, "difference": r.difference}
        for r in rows
    ], int(total)


async def list_llm_call_metrics(
    user_id: str, limit: int = 100, offset: int = 0, chat_id: str = "",
) -> tuple[list[dict[str, Any]], int]:
    where = [LlmCallMetric.user_id == user_id]
    if chat_id:
        where.append(LlmCallMetric.chat_id == chat_id)
    async with async_session() as db:
        total = (await db.execute(select(func.count()).select_from(LlmCallMetric).where(*where))).scalar_one()
        rows = (
            (await db.execute(
                select(LlmCallMetric).where(*where)
                .order_by(LlmCallMetric.created_at.desc())
                .limit(limit).offset(offset)
            )).scalars().all()
        )
    return [
        {**_row_dict(r),
         "purpose": r.purpose, "endpoint_id": r.endpoint_id, "endpoint_name": r.endpoint_name,
         "model": r.model, "status_code": r.status_code, "latency_ms": r.latency_ms,
         "failover_attempt": r.failover_attempt, "prompt_tokens": r.prompt_tokens,
         "completion_tokens": r.completion_tokens, "reasoning_tokens": r.reasoning_tokens,
         "tokens_source": r.tokens_source, "rule_id": r.rule_id}
        for r in rows
    ], int(total)
