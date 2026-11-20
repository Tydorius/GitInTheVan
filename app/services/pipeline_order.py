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
    from app.services.lorebook import inject_entries, match_book_entries
    from app.services.metrics import record_prompt_object

    messages = body_json.get("messages", [])
    entry_dicts = [
        {
            "lorebook_id": lb.id,
            "lorebook_name": lb.name,
            "entry_id": entry.id,
            "name": entry.name,
            "keys": entry.keys,
            "secondary_keys": entry.secondary_keys,
            "content": entry.content,
            "position": entry.position,
            "insertion_order": entry.insertion_order,
            "is_constant": entry.is_constant,
            "is_selective": entry.is_selective,
            "is_disabled": entry.is_disabled,
            "character_limit": entry.character_limit,
        }
        for entry in lb.entries
        if not entry.is_disabled
    ]
    if not entry_dicts:
        return 0
    matched = match_book_entries(messages, entry_dicts, lb.scan_depth)
    matched.sort(key=lambda m: m.insertion_order)
    if not matched:
        debug_capture(
            body_json, f"pipeline_order_book_{lb.id[:8]}", "Pipeline Order: Lorebook Slot",
            detail=f"'{lb.name}' matched no entries at its slot",
            metadata={"lorebook_id": lb.id, "lorebook_name": lb.name, "matched": 0},
        )
        return 0

    body_json["messages"] = inject_entries(body_json["messages"], matched)
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
        _load_chat_data,
        _load_user_data,
        _prepare_lorebook_seeding,
        _save_chat_data,
        _save_user_data,
        _store_writes_isolated,
        apply_cantrip_result_to_messages,
        apply_prompt_strips,
    )
    from app.services.cantrip_context import build_context, extract_context_params
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
    messages = body_json.get("messages", [])

    async with async_session() as db:
        # Activation (rule 19): the plan orders, it never activates. The
        # skipped candidates are recorded exactly as the default pass
        # records them, so comparisons stay meaningful.
        all_cantrips: list[Any] = await _load_active_cantrips(db, user_id, "pre_driver")
        if embedded_scripts:
            all_cantrips = [*all_cantrips, *embedded_scripts]
        cantrips = []
        for c in all_cantrips:
            if should_activate_resource(
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
        activating_lorebooks = await load_active_lorebooks(
            db, user_id, tags, request_headers, internal_chat_id
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
        state: dict[str, Any] = {
            "chat_data": chat_data,
            "user_data": user_data,
            "accumulated_memories": {},
            "lorebook_seed": lorebook_seed,
            "lorebook_always": lorebook_always,
        }
        passenger_handler = await get_passenger_handler(body_json, user_id)
        passenger_handler.stage_index = None

        for kind, obj in steps:
            if kind == "lorebook":
                await _inject_book_now(body_json, user_id, obj)
                continue

            # Refresh the chat view: earlier slots may have injected lorebook
            # entries or stripped text into body_json["messages"], and this
            # cantrip's context.chat.last_messages must see the prompt AS IT
            # STANDS at its slot. Character fields and request facts stay on
            # the shared context (they chain across cantrips exactly as the
            # default pass chains them).
            fresh = build_context(body_json.get("messages", []), **params)
            context["chat"] = fresh["chat"]

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

            # Slot-local outputs: everything this cantrip asked for lands
            # NOW, before the next step runs -- strips rewrites the message
            # list, request rows (activate()/inject()) inject immediately.
            if result.prompt_strips:
                apply_prompt_strips(
                    body_json, result.prompt_strips, obj.id, obj.name
                )
            if request_rows:
                body_json["messages"] = _inject_entries(
                    body_json["messages"], request_rows
                )
                debug_capture(
                    body_json, f"pipeline_order_slot_{obj.id[:8]}", "Pipeline Order: Slot Outputs",
                    detail=f"'{obj.name}' injected {len(request_rows)} request row(s) at its slot",
                    metadata={"cantrip_id": obj.id, "rows": len(request_rows)},
                )

        # Pass tail (mirrors process_cantrips': stores, memories, card
        # fields). The per-cantrip step itself is shared; only this small
        # ending is written here.
        if conversation_id and not body_json.get("_gitv_replay"):
            await _save_chat_data(db, user_id, conversation_id, state["chat_data"])
        if not isolated:
            await _save_user_data(db, user_id, state["user_data"])

    if internal_chat_id and state["accumulated_memories"]:
        from app.services.conversation import save_memories_for_chat
        await save_memories_for_chat(
            internal_chat_id, user_id, state["accumulated_memories"]
        )

    final_personality = context["character"]["personality"]
    final_scenario = context["character"]["scenario"]
    final_example_dialogs = context["character"]["example_dialogs"]
    if (
        final_personality != seeded_character["personality"]
        or final_scenario != seeded_character["scenario"]
        or final_example_dialogs != seeded_character["example_dialogs"]
    ):
        body_json["messages"] = apply_cantrip_result_to_messages(
            messages,
            final_personality,
            final_scenario,
            final_example_dialogs,
        )
