"""Manual pre-forward ordering (pipeline ordering, 0.41.0).

A user-defined interleaved order over the pre-forward window -- the stretch
of pipeline that today runs [all lorebook injection → the global pre-Driver
cantrip pass]. With manual ordering ON, that window instead walks the
user's list (``user_settings.pipeline_order_json``): Cantrip 1, Lorebook 1,
Lorebook 2, Cantrip 2 ... any interleaving. Everything a step outputs lands
before the next step runs, so a lorebook slot matches against text an
earlier cantrip stripped or injected.

Design: ``Planning/past-phases.md`` (full plan archived at
``Planning/archive/pipeline-order-2026-11.md``). Two invariants:

- **The list orders, it never activates** (rule 19). A listed item still
  must pass ``should_activate_resource`` to run; a listed-but-inactive,
  untagged cantrip simply does not fire.
- **Off/empty is byte-identical to the default window.** The proxy only
  reaches this module when the plan is enabled AND the request resolved to
  no map; everything else takes today's exact path.

Unlisted-but-activating resources run after the listed block (cantrips by
execution_order, lorebooks by creation order, <jslorebook> scripts there
too), so nothing is ever silently dropped and newly-created resources
self-append at the tail.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.database import async_session

logger = logging.getLogger(__name__)

# Config caps: the plan is a UI-built ordering list, not content. 200 items
# is far beyond any plausible resource count and keeps the column bounded.
MAX_PLAN_ITEMS = 200


def parse_plan(raw: str | None) -> dict[str, Any]:
    """Parse pipeline_order_json. Never raises: corrupt JSON degrades to a
    disabled plan (the default window runs), with a log line."""
    if not raw or not raw.strip():
        return {"enabled": False, "items": []}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("pipeline_order_json did not parse; manual ordering off")
        return {"enabled": False, "items": []}
    if not isinstance(data, dict):
        return {"enabled": False, "items": []}
    items: list[dict[str, str]] = []
    raw_items = data.get("items", [])
    if isinstance(raw_items, list):
        for entry in raw_items[:MAX_PLAN_ITEMS]:
            if not isinstance(entry, dict):
                continue
            item_type = str(entry.get("type", ""))
            item_id = str(entry.get("id", ""))
            if item_type in ("cantrip", "lorebook") and item_id:
                items.append({"type": item_type, "id": item_id})
    return {"enabled": bool(data.get("enabled")) and bool(items), "items": items}


async def load_plan(user_id: str) -> dict[str, Any]:
    """Read and parse the user's plan. One settings read; callers that only
    need the enabled flag still pay the same single query."""
    from sqlalchemy import select

    from app.models.user_settings import UserSettings

    async with async_session() as db:
        raw = (
            await db.execute(
                select(UserSettings.pipeline_order_json).where(
                    UserSettings.user_id == user_id
                )
            )
        ).scalar_one_or_none()
    return parse_plan(raw)


def ordered_steps(
    plan: dict[str, Any],
    activating_cantrips: list[Any],
    activating_lorebooks: list[Any],
) -> list[tuple[str, Any]]:
    """The execution order: listed items first (plan order, keeping only
    those that actually activate this request), then the unlisted tail
    (cantrips by execution_order, lorebooks by creation order). The plan
    never adds an item that did not activate."""
    listed_cantrips = {
        i["id"] for i in plan.get("items", []) if i["type"] == "cantrip"
    }
    listed_lorebooks = {
        i["id"] for i in plan.get("items", []) if i["type"] == "lorebook"
    }
    by_cid = {c.id: c for c in activating_cantrips}
    by_lid = {lb.id: lb for lb in activating_lorebooks}

    steps: list[tuple[str, Any]] = []
    for item in plan.get("items", []):
        if item["type"] == "cantrip" and item["id"] in by_cid:
            steps.append(("cantrip", by_cid[item["id"]]))
        elif item["type"] == "lorebook" and item["id"] in by_lid:
            steps.append(("lorebook", by_lid[item["id"]]))

    tail_cantrips = sorted(
        (c for c in activating_cantrips if c.id not in listed_cantrips),
        key=lambda c: c.execution_order,
    )
    tail_lorebooks = sorted(
        (lb for lb in activating_lorebooks if lb.id not in listed_lorebooks),
        key=lambda lb: lb.created_at,
    )
    steps.extend(("cantrip", c) for c in tail_cantrips)
    steps.extend(("lorebook", lb) for lb in tail_lorebooks)
    return steps


def _lorebook_budget_for(lb: Any, allocs: dict[str, Any] | None) -> int:
    from app.services.lorebook import lorebook_budget_chars

    return lorebook_budget_chars(lb, allocs)


async def _inject_book_now(
    body_json: dict[str, Any],
    user_id: str,
    lb: Any,
) -> int:
    """One lorebook slot: keyword-match against the message list AS IT
    STANDS at this slot (earlier cantrips may have stripped or injected
    text) and inject immediately. Mirrors the default pass's per-book
    matching and provenance/metrics path; inject_after_cantrips is ignored
    here -- in manual mode, position in the list is the truth."""
    from app.services.budget import estimate_tokens as _est_tokens
    from app.services.debug import debug_capture
    from app.services.lorebook import book_entries_to_dicts, inject_entries, match_book_entries
    from app.services.metrics import record_prompt_object

    messages = body_json.get("messages", [])
    entry_dicts = book_entries_to_dicts(
        (entry for entry in lb.entries if not entry.is_disabled),
        book=lb,
    )
    if not entry_dicts:
        return 0
    allocs = body_json.get("_gitv_budget_allocations")
    book_budget = _lorebook_budget_for(lb, allocs)
    matched = match_book_entries(
        messages,
        entry_dicts,
        lb.scan_depth,
        word_boundary=lb.word_boundary_keys,
        total_budget=book_budget,
    )
    matched.sort(key=lambda m: m.insertion_order)
    if not matched:
        debug_capture(
            body_json, f"pipeline_order_book_{lb.id[:8]}", "Pipeline Order: Lorebook Slot",
            detail=f"'{lb.name}' matched no entries at its slot",
            metadata={"lorebook_id": lb.id, "lorebook_name": lb.name, "matched": 0},
        )
        return 0

    body_json["messages"] = inject_entries(
        body_json["messages"],
        matched,
        cache_friendly=bool(body_json.get("_gitv_cache_friendly")),
    )
    for m in matched:
        record_prompt_object(
            body_json, "lorebook_entry", m.entry_id or lb.id,
            0, _est_tokens(m.content), parent_id=lb.id,
        )
    debug_capture(
        body_json, f"pipeline_order_book_{lb.id[:8]}", "Pipeline Order: Lorebook Slot",
        detail=f"'{lb.name}' injected {len(matched)} entr"
               f"{'y' if len(matched) == 1 else 'ies'} at its slot",
        metadata={
            "lorebook_id": lb.id,
            "lorebook_name": lb.name,
            "matched": [
                {"entry_id": m.entry_id, "name": m.name, "tokens": _est_tokens(m.content)}
                for m in matched
            ],
        },
    )
    logger.info(
        "Pipeline order: lorebook '%s' injected %d entries at its slot",
        lb.name, len(matched),
    )
    return len(matched)


async def run_pre_forward_ordered(
    plan: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
    internal_chat_id: str = "",
    embedded_scripts: list | None = None,
) -> None:
    """Walk the manual order over the pre-forward window. Replaces
    [_apply_lorebook_injection + the global process_cantrips pass] for this
    request. Cantrips execute through the shared ``_execute_cantrip_step``
    (the extracted default-pass loop body); lorebook slots match and inject
    against the message list as it stands at the slot; a cantrip's
    strip / inject / activate outputs apply at its slot, immediately."""
    from app.services.cantrip import (
        _execute_cantrip_step,
        _load_active_cantrips,
        _load_cantrip_data_batch,
        _load_chat_data,
        _load_user_data,
        _prepare_lorebook_seeding,
        _save_cantrip_data_batch,
        _save_chat_data,
        _save_user_data,
        _store_writes_isolated,
        apply_cantrip_result_to_messages,
        apply_history_ops,
        apply_prompt_strips,
    )
    from app.services.cantrip_context import (
        CardEditTracker,
        build_context,
        extract_context_params,
    )
    from app.services.conversation import load_memories_for_chat
    from app.services.debug import debug_capture
    from app.services.debug_metrics import record_cantrip
    from app.services.lorebook import inject_entries as _inject_entries
    from app.services.lorebook import load_active_lorebooks
    from app.services.passenger import get_passenger_handler
    from app.services.tagging import should_activate_resource

    params = extract_context_params(body_json, request_headers)
    conversation_id = params.get("conversation_id", "")
    isolated = _store_writes_isolated(body_json)

    async with async_session() as db:
        # Activation (rule 19): the plan orders, it never activates. The
        # skipped candidates are recorded exactly as the default pass
        # records them, so comparisons stay meaningful.
        from app.services.group_resolver import group_direct_members

        direct_ids = group_direct_members(body_json)
        all_cantrips: list[Any] = await _load_active_cantrips(
            db, user_id, "pre_driver", direct_member_ids=direct_ids
        )
        if embedded_scripts:
            all_cantrips = [*all_cantrips, *embedded_scripts]
        cantrips = []
        for c in all_cantrips:
            if c.id in direct_ids or should_activate_resource(
                c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags or []
            ):
                cantrips.append(c)
            else:
                record_cantrip(
                    body_json, cantrip_id=c.id, name=c.name, position="pre_driver",
                    triggered=False, tag=c.tag,
                    reason=(
                        f"tag '{c.tag}' not present in this request"
                        if c.tag else "not active and no tag activates it"
                    ),
                )
        if cantrips:
            from app.services import deno_runner

            _pool = getattr(deno_runner, "deno_pool", None)
            if _pool is not None:
                _pool.ensure_warm(len(cantrips))

        activating_lorebooks = await load_active_lorebooks(
            db, user_id, tags, request_headers, internal_chat_id,
            direct_member_ids=direct_ids,
        )

        steps = ordered_steps(plan, cantrips, activating_lorebooks)
        debug_capture(
            body_json, "pipeline_order_plan", "Pipeline Order (Manual)",
            detail=f"{len(steps)} step(s): "
            + " → ".join(
                f"{kind}@{getattr(obj, 'name', '?')}" for kind, obj in steps[:12]
            )
            + (" …" if len(steps) > 12 else ""),
            metadata={
                "steps": [
                    {"type": kind, "id": getattr(obj, "id", ""), "name": getattr(obj, "name", "")}
                    for kind, obj in steps
                ],
            },
        )
        if not steps:
            return

        chat_data = (
            await _load_chat_data(db, user_id, conversation_id) if conversation_id else {}
        )
        user_data = await _load_user_data(db, user_id)
        before_chat_data = dict(chat_data)
        before_user_data = dict(user_data)
        cantrip_data_map = await _load_cantrip_data_batch(
            db,
            user_id,
            [
                c.id
                for c in cantrips
                if not getattr(c, "ephemeral", False)
                and not getattr(c, "is_embedded", False)
            ],
        )
        lorebook_seed, lorebook_always, binding_conv_id = await _prepare_lorebook_seeding(
            db, body_json, user_id, request_headers, internal_chat_id, cantrips
        )

        context = build_context(body_json.get("messages", []), **params)
        if internal_chat_id:
            context["__memories"] = await load_memories_for_chat(internal_chat_id, user_id)

        budget_allocations = body_json.get("_gitv_budget_allocations")
        seeded_character = {
            "personality": context["character"]["personality"],
            "scenario": context["character"]["scenario"],
            "example_dialogs": context["character"]["example_dialogs"],
        }
        card_tracker = CardEditTracker(seeded_character)
        state: dict[str, Any] = {
            "chat_data": chat_data,
            "user_data": user_data,
            "accumulated_memories": {},
            "lorebook_seed": lorebook_seed,
            "lorebook_always": lorebook_always,
            "cantrip_data_map": cantrip_data_map,
            "cantrip_data_dirty": {},
        }
        passenger_handler = await get_passenger_handler(body_json, user_id)
        passenger_handler.stage_index = None

        messages_dirty = False
        for kind, obj in steps:
            if kind == "lorebook":
                injected = await _inject_book_now(body_json, user_id, obj)
                if injected:
                    messages_dirty = True
                continue

            # Refresh the chat view only when an earlier slot actually mutated
            # body_json["messages"] (lorebook injection, strips, history ops,
            # or request rows). Character fields and request facts stay on the
            # shared context (they chain across cantrips exactly as the default
            # pass chains them).
            if messages_dirty:
                fresh = build_context(body_json.get("messages", []), **params)
                context["chat"] = fresh["chat"]
                messages_dirty = False

            pre_character = {
                "personality": context["character"]["personality"],
                "scenario": context["character"]["scenario"],
                "example_dialogs": context["character"]["example_dialogs"],
            }
            result, request_rows = await _execute_cantrip_step(
                obj,
                db=db,
                body_json=body_json,
                user_id=user_id,
                request_headers=request_headers,
                position="pre_driver",
                context=context,
                state=state,
                binding_conv_id=binding_conv_id,
                internal_chat_id=internal_chat_id,
                isolated=isolated,
                store_seeds=None,
                passenger_handler=passenger_handler,
                budget_allocations=budget_allocations,
                cantrips_for_seeding=cantrips,
            )
            if result is None:
                continue

            card_tracker.record_step(
                pre_character,
                context["character"],
                is_standing=bool(getattr(obj, "card_edits_standing", False)),
            )

            # Slot-local outputs: everything this cantrip asked for lands
            # NOW, before the next step runs -- strips rewrites the message
            # list, history ops trim/drop messages, request rows
            # (activate()/inject()) inject immediately.
            if result.prompt_strips:
                apply_prompt_strips(
                    body_json, result.prompt_strips, obj.id, obj.name
                )
                messages_dirty = True
            history_ops = getattr(result, "history_ops", None)
            if history_ops:
                apply_history_ops(body_json, history_ops)
                messages_dirty = True
            if request_rows:
                body_json["messages"] = _inject_entries(
                    body_json["messages"],
                    request_rows,
                    cache_friendly=bool(body_json.get("_gitv_cache_friendly")),
                )
                messages_dirty = True
                debug_capture(
                    body_json, f"pipeline_order_slot_{obj.id[:8]}", "Pipeline Order: Slot Outputs",
                    detail=f"'{obj.name}' injected {len(request_rows)} request row(s) at its slot",
                    metadata={"cantrip_id": obj.id, "rows": len(request_rows)},
                )

        # Pass tail (mirrors process_cantrips': stores, memories, card
        # fields). The per-cantrip step itself is shared; only this small
        # ending is written here. Store deletes aggregate in the step and
        # persist here (0.42.0).
        if conversation_id and not body_json.get("_gitv_replay"):
            await _save_chat_data(
                db, user_id, conversation_id, state["chat_data"],
                deletes=state.get("chat_data_deletes"),
                before=before_chat_data,
            )
        if not isolated:
            await _save_user_data(
                db, user_id, state["user_data"],
                deletes=state.get("user_data_deletes"),
                before=before_user_data,
            )
            if state["cantrip_data_dirty"]:
                await _save_cantrip_data_batch(
                    db, user_id, state["cantrip_data_dirty"]
                )

    if internal_chat_id and state["accumulated_memories"] and not body_json.get("_gitv_replay"):
        from app.services.conversation import save_memories_for_chat
        await save_memories_for_chat(
            internal_chat_id, user_id, state["accumulated_memories"]
        )

    final_personality = context["character"]["personality"]
    final_scenario = context["character"]["scenario"]
    final_example_dialogs = context["character"]["example_dialogs"]
    if card_tracker.any_changed(context["character"]):
        cache_friendly = bool(body_json.get("_gitv_cache_friendly"))
        if cache_friendly:
            standing_finals, tail_deltas = card_tracker.resolve_for_cache_friendly()
            body_json["messages"] = apply_cantrip_result_to_messages(
                body_json.get("messages", []),
                standing_finals["personality"],
                standing_finals["scenario"],
                standing_finals["example_dialogs"],
                cache_friendly=True,
                seeded_character=standing_finals,
                extra_tail_deltas=tail_deltas,
            )
        else:
            body_json["messages"] = apply_cantrip_result_to_messages(
                body_json.get("messages", []),
                final_personality,
                final_scenario,
                final_example_dialogs,
                cache_friendly=False,
                seeded_character=seeded_character,
            )
