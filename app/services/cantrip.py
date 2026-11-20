from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.cantrip_data import CantripData
from app.models.chat_data import ChatData
from app.models.lorebook import Lorebook
from app.models.user_data import UserData
from app.services.cantrip_context import apply_cantrip_result_to_messages, build_context, extract_context_params
from app.services.deno_runner import CantripResult, run_cantrip
from app.services.dynamic_lorebook import DELTA_KEY, apply_ops, build_seed, parse_reads

logger = logging.getLogger(__name__)


@dataclass
class EmbeddedScriptCantrip:
    """A <jslorebook> script extracted from the message text (Phase 20).

    Runs through the cantrip pass machinery without a DB row. is_active=True
    and tag="" mean the Activation Hierarchy runs it unconditionally (it
    arrived in the chat, so no tag/Active question applies). It owns no
    cantrip_data (its id is per-request, not stable identity) and takes no
    context-budget slice.
    """

    id: str
    name: str
    code: str
    timeout_ms: int = 5000
    tag: str = ""
    is_active: bool = True
    is_public: bool = False
    user_id: str = ""
    execution_order: int = 0
    lorebook_reads: str = "[]"
    budget_weight: float = 0.0
    ephemeral: bool = True
    created_at: Any = field(default=None)


def embedded_jslb_scripts(
    body_json: dict[str, Any], position: str
) -> list[EmbeddedScriptCantrip]:
    """The request's extracted <jslorebook> scripts for one position.

    They run in the global pre-driver pass and the response-side passes at
    their detected position (jslorebook._detect_position); map stages do not
    re-run them — a stage inherits their effects through the prompt the
    global pass produced.
    """
    from app.services.jslorebook import EmbeddedScript

    scripts = body_json.get("_gitv_jslb_scripts") or []
    if not isinstance(scripts, list):
        return []
    return [
        EmbeddedScriptCantrip(id=f"jslb-{i}", name=f"jslorebook #{i + 1}", code=s.code)
        for i, s in enumerate(scripts)
        if isinstance(s, EmbeddedScript) and s.position == position
    ]


async def _load_chat_data(db: AsyncSession, user_id: str, conversation_id: str) -> dict[str, Any]:
    if not conversation_id:
        return {}
    result = await db.execute(
        select(ChatData).where(
            ChatData.user_id == user_id,
            ChatData.conversation_id == conversation_id,
        )
    )
    rows = result.scalars().all()
    data: dict[str, Any] = {}
    for row in rows:
        try:
            data[row.key] = json.loads(row.value_json) if row.value_json else None
        except (json.JSONDecodeError, TypeError):
            data[row.key] = row.value_json
    return data


async def _save_chat_data(
    db: AsyncSession,
    user_id: str,
    conversation_id: str,
    chat_data: dict[str, Any],
    deletes: set[str] | None = None,
) -> None:
    if not conversation_id:
        return

    existing_result = await db.execute(
        select(ChatData).where(
            ChatData.user_id == user_id,
            ChatData.conversation_id == conversation_id,
        )
    )
    existing = {row.key: row for row in existing_result.scalars().all()}

    for key, value in chat_data.items():
        value_json = json.dumps(value)
        if key in existing:
            if existing[key].value_json != value_json:
                existing[key].value_json = value_json
        else:
            db.add(
                ChatData(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    key=key,
                    value_json=value_json,
                )
            )

    # 0.42.0: delete() persists. A key deleted then re-set in the same run
    # keeps its new value (the final dict is the truth).
    for key in deletes or ():
        if key in chat_data:
            continue
        row = existing.get(key)
        if row is not None:
            await db.delete(row)

    await db.commit()


async def _load_user_data(db: AsyncSession, user_id: str) -> dict[str, Any]:
    result = await db.execute(
        select(UserData).where(UserData.user_id == user_id)
    )
    rows = result.scalars().all()
    data: dict[str, Any] = {}
    for row in rows:
        try:
            data[row.key] = json.loads(row.value_json) if row.value_json else None
        except (json.JSONDecodeError, TypeError):
            data[row.key] = row.value_json
    return data


async def _save_user_data(
    db: AsyncSession,
    user_id: str,
    user_data: dict[str, Any],
    deletes: set[str] | None = None,
) -> None:
    existing_result = await db.execute(
        select(UserData).where(UserData.user_id == user_id)
    )
    existing = {row.key: row for row in existing_result.scalars().all()}

    for key, value in user_data.items():
        value_json = json.dumps(value)
        if key in existing:
            if existing[key].value_json != value_json:
                existing[key].value_json = value_json
        else:
            db.add(
                UserData(
                    user_id=user_id,
                    key=key,
                    value_json=value_json,
                )
            )

    for key in deletes or ():
        if key in user_data:
            continue
        row = existing.get(key)
        if row is not None:
            await db.delete(row)

    await db.commit()


async def _load_cantrip_data(
    db: AsyncSession, user_id: str, cantrip_id: str
) -> dict[str, Any]:
    result = await db.execute(
        select(CantripData).where(
            CantripData.user_id == user_id,
            CantripData.cantrip_id == cantrip_id,
        )
    )
    rows = result.scalars().all()
    data: dict[str, Any] = {}
    for row in rows:
        try:
            data[row.key] = json.loads(row.value_json) if row.value_json else None
        except (json.JSONDecodeError, TypeError):
            data[row.key] = row.value_json
    return data


async def _save_cantrip_data(
    db: AsyncSession,
    user_id: str,
    cantrip_id: str,
    cantrip_data: dict[str, Any],
    deletes: set[str] | None = None,
) -> None:
    existing_result = await db.execute(
        select(CantripData).where(
            CantripData.user_id == user_id,
            CantripData.cantrip_id == cantrip_id,
        )
    )
    existing = {row.key: row for row in existing_result.scalars().all()}

    for key, value in cantrip_data.items():
        value_json = json.dumps(value)
        if key in existing:
            if existing[key].value_json != value_json:
                existing[key].value_json = value_json
        else:
            db.add(
                CantripData(
                    user_id=user_id,
                    cantrip_id=cantrip_id,
                    key=key,
                    value_json=value_json,
                )
            )

    for key in deletes or ():
        if key in cantrip_data:
            continue
        row = existing.get(key)
        if row is not None:
            await db.delete(row)

    await db.commit()


def _diff_store(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What one cantrip changed in a persistent store.

    Cantrips at the same position run in sequence against a shared chat_data and
    user_data, so the store a cantrip received already carries the previous
    cantrip's writes. Only the delta identifies who did what.

    Values are recorded as they are; the trace's size cap bounds the whole thing
    if a cantrip writes something enormous.
    """
    before = before or {}
    after = after or {}
    changes: dict[str, Any] = {}

    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if key not in before:
            changes[key] = {"op": "added", "to": new}
        elif key not in after:
            changes[key] = {"op": "removed", "from": old}
        else:
            changes[key] = {"op": "changed", "from": old, "to": new}

    return changes


async def _load_active_cantrips(
    db: AsyncSession,
    user_id: str,
    position: str = "pre_driver",
    direct_member_ids: set[str] | None = None,
) -> list[Cantrip]:
    flag_map = {
        "pre_driver": Cantrip.run_pre_driver,
        "pre_navigator": Cantrip.run_pre_navigator,
        "post_navigator": Cantrip.run_post_navigator,
    }
    flag = flag_map.get(position, Cantrip.run_pre_driver)

    # Loads candidates, not decisions. A cantrip that is inactive but tagged must
    # reach should_activate_resource, or its tag can never switch it on -- which
    # is rule 3 of the Activation Hierarchy (see the README). Filtering
    # is_active here is what made cantrip tags unable to activate anything.
    # direct_member_ids (the seventh source, rule 19) must widen the same
    # candidate set: a blank-tag inactive cantrip is exactly what group
    # membership activates.
    conditions = [or_(Cantrip.is_active.is_(True), Cantrip.tag != "")]
    if direct_member_ids:
        conditions.append(Cantrip.id.in_(direct_member_ids))
    query = (
        select(Cantrip)
        .where(
            Cantrip.user_id == user_id,
            or_(*conditions),
            flag.is_(True),
        )
        .order_by(Cantrip.execution_order, Cantrip.created_at)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


def _store_writes_isolated(body_json: dict[str, Any]) -> bool:
    """Sandbox and replay runs must not mutate stores keyed outside the fork.

    chat_data and memories are isolated by the forked/synthetic chat id the
    proxy resolves, but user_data and cantrip_data are keyed by user alone, so
    writes from a sandbox or replay would land in the same live rows a real
    request writes. Reads stay live so the run still sees the state it is
    replaying; only persistence is suppressed.
    """
    return bool(body_json.get("_gitv_sandbox") or body_json.get("_gitv_replay"))


async def _prepare_lorebook_seeding(
    db: AsyncSession,
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    cantrips: list[Cantrip],
    capture: bool = True,
) -> tuple[dict[str, dict[str, Any]], set[str], str]:
    """Phase 20 seeding state for one cantrip pass.

    Returns (full seed blob, always-included book names, binding conversation
    id). The binding id is the Phase 29 identity (client header, else the
    internal rolling-hash id) — the same resolver the lexicon compiler and the
    injection-side fifth-source lookup use, so a book written here is the book
    that activates there.
    """
    # Lazy: lexicon imports this module (test_cantrip), so a top-level import
    # would be a cycle.
    from app.services.lexicon import resolve_action_identity

    binding_conv_id, _ = resolve_action_identity(request_headers, internal_chat_id)
    all_reads: list[str] = []
    for c in cantrips:
        for name in parse_reads(getattr(c, "lorebook_reads", None)):
            if name not in all_reads:
                all_reads.append(name)
    full_seed, always = await build_seed(
        db, body_json, user_id, binding_conv_id, extra_names=all_reads, capture=capture
    )
    return full_seed, always, binding_conv_id


def _seed_for_cantrip(
    full_seed: dict[str, dict[str, Any]], always: set[str], reads: list[str]
) -> dict[str, dict[str, Any]]:
    """The seed one cantrip sees: always-bound books for everyone at this
    position, plus that cantrip's own lorebook_reads names."""
    names = list(always)
    for name in reads:
        if name not in names:
            names.append(name)
    return {n: full_seed[n] for n in names if n in full_seed}


async def process_cantrips(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
    position: str = "pre_driver",
    internal_chat_id: str = "",
    only_ids: set[str] | None = None,
    exclude_ids: set[str] | None = None,
    stage_index: int | None = None,
    embedded_scripts: list[EmbeddedScriptCantrip] | None = None,
    store_seeds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run cantrips at a pipeline position.

    ``only_ids`` and ``exclude_ids`` scope the run to a subset of the user's
    cantrips. The map pipeline uses them to honour stage attachments: the
    pre-stage "global" pass excludes every cantrip bound to a stage, and each
    stage runs only the cantrips attached to it. Without that split a cantrip
    with side effects -- a dice roll, a balance change, a counter -- fires once
    globally and again on every stage of the map.

    ``stage_index`` names the map stage this pass belongs to, so Passenger
    LLM-call rows can attribute their cost to the stage (None everywhere
    else). ``embedded_scripts`` adds <jslorebook> scripts from the message
    text to this pass (Phase 20); they run after the DB cantrips.

    ``store_seeds`` (sequence tester) replaces the DB stores with an
    in-memory holder: keys ``chat_data`` / ``user_data`` / ``cantrip_data``
    (the last keyed by cantrip id). When present, nothing is loaded from or
    written to the DB -- the mutated dicts are written back into the holder
    so the caller can thread them through the next pass. ``None`` (every
    pipeline call site) keeps today's load/save behavior exactly.
    """
    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        from app.services.group_resolver import group_direct_members

        all_cantrips: list[Any] = await _load_active_cantrips(
            db, user_id, position, direct_member_ids=group_direct_members(body_json)
        )
        if embedded_scripts:
            all_cantrips = [*all_cantrips, *embedded_scripts]
            from app.services.debug import debug_capture

            debug_capture(
                body_json, "jslorebook_execution", "jslorebook Scripts",
                detail=(
                    f"{len(embedded_scripts)} embedded script(s) executing at "
                    f"'{position}' (sandboxed, guarded)"
                ),
                metadata={
                    "position": position,
                    "scripts": [{"id": s.id, "name": s.name} for s in embedded_scripts],
                },
            )
        if not all_cantrips:
            return body_json

        if only_ids is not None:
            all_cantrips = [c for c in all_cantrips if c.id in only_ids]
        if exclude_ids:
            all_cantrips = [c for c in all_cantrips if c.id not in exclude_ids]
        if not all_cantrips:
            return body_json

        from app.services.debug_metrics import record_cantrip
        from app.services.group_resolver import group_direct_members
        from app.services.tagging import should_activate_resource
        direct_ids = group_direct_members(body_json)
        cantrips = []
        for c in all_cantrips:
            # Asked for every candidate. There used to be an `if c.tag and tags`
            # pre-check with an unconditional `else: append`, which skipped the
            # hierarchy entirely whenever the request carried no tags -- so an
            # inactive cantrip ran anyway.
            # Seventh source (rule 19): direct membership in an activated
            # opted-in group, OR-ed in like every union source.
            if c.id in direct_ids or should_activate_resource(
                c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags or []
            ):
                cantrips.append(c)
            else:
                # Recorded so a comparison can show "this fired in run A and not
                # in run B", which is invisible if only executions are stored.
                record_cantrip(
                    body_json, cantrip_id=c.id, name=c.name, position=position,
                    triggered=False, tag=c.tag,
                    reason=(
                        f"tag '{c.tag}' not present in this request"
                        if c.tag else "not active and no tag activates it"
                    ),
                )

        if not cantrips:
            return body_json

        conversation_id = params.get("conversation_id", "")
        isolated = _store_writes_isolated(body_json)
        if store_seeds is not None:
            store_seeds.setdefault("chat_data", {})
            store_seeds.setdefault("user_data", {})
            store_seeds.setdefault("cantrip_data", {})
            chat_data = dict(store_seeds["chat_data"])
            user_data = dict(store_seeds["user_data"])
        else:
            chat_data = await _load_chat_data(db, user_id, conversation_id)
            user_data = await _load_user_data(db, user_id)

        lorebook_seed, lorebook_always, binding_conv_id = await _prepare_lorebook_seeding(
            db, body_json, user_id, request_headers, internal_chat_id, cantrips
        )

        context = build_context(messages, **params)

        if internal_chat_id:
            from app.services.conversation import load_memories_for_chat

            memory_map = await load_memories_for_chat(internal_chat_id, user_id)
            context["__memories"] = memory_map

        budget_allocations = body_json.get("_gitv_budget_allocations")

        accumulated_memories: dict[str, str] = {}
        # JanitorAI parity: the card fields are seeded with the request's real
        # section values, each cantrip sees the previous cantrip's output, and
        # the final value replaces the card's own section (never concatenated
        # alongside it).
        seeded_character = {
            "personality": context["character"]["personality"],
            "scenario": context["character"]["scenario"],
            "example_dialogs": context["character"]["example_dialogs"],
        }

        from app.services.passenger import get_passenger_handler

        # One Passenger handler per request, shared by every cantrip at every
        # position (and every map stage) so the call budget is per-request,
        # never per-cantrip. stage_index attributes LLM-call rows to the map
        # stage this pass belongs to (None resets it between passes).
        passenger_handler = await get_passenger_handler(body_json, user_id)
        passenger_handler.stage_index = stage_index

        # Mutable pass state threaded through the per-cantrip steps. The
        # manual-order orchestrator (pipeline_order.py) threads the same
        # shape through the shared _execute_cantrip_step, so both orderings
        # execute cantrips through ONE code path and cannot drift.
        state: dict[str, Any] = {
            "chat_data": chat_data,
            "user_data": user_data,
            "accumulated_memories": accumulated_memories,
            "lorebook_seed": lorebook_seed,
            "lorebook_always": lorebook_always,
        }

        for cantrip in cantrips:
            result, request_rows = await _execute_cantrip_step(
                cantrip,
                db=db,
                body_json=body_json,
                user_id=user_id,
                request_headers=request_headers,
                position=position,
                context=context,
                state=state,
                binding_conv_id=binding_conv_id,
                internal_chat_id=internal_chat_id,
                isolated=isolated,
                store_seeds=store_seeds,
                passenger_handler=passenger_handler,
                budget_allocations=budget_allocations,
                cantrips_for_seeding=cantrips,
            )
            if result is None:
                continue

            # Package 3 (items 8 + 4): prompt-side requests. Like the op
            # delta, they only land before the forward; on response-side
            # passes there is no injection point left, so they are dropped
            # (the facade is a pre-forward tool by contract).
            if request_rows and position == "pre_driver":
                body_json.setdefault(DELTA_KEY, []).extend(request_rows)

            # Pipeline ordering (0.41.0): strips collected on a pre-driver
            # pass ride the body for the proxy to apply right after the
            # pass -- before deferred lorebook entries match, so
            # inject-after-cantrips books see cleaned text. Response-side
            # strips are dropped the same way (no prompt left to rewrite).
            if result.prompt_strips and position == "pre_driver":
                body_json.setdefault("_gitv_prompt_strips", []).extend(
                    {**s, "source": cantrip.name} for s in result.prompt_strips
                )

        chat_data = state["chat_data"]
        user_data = state["user_data"]

        if store_seeds is not None:
            # The seeds holder IS the store for this run: write the final
            # pass state back so the caller threads it into the next pass.
            store_seeds["chat_data"] = chat_data
            store_seeds["user_data"] = user_data
        else:
            # A sandbox writes chat_data into its forked id (isolated); a replay
            # would write the live conversation's store, so it does not.
            if conversation_id and not body_json.get("_gitv_replay"):
                await _save_chat_data(
                    db, user_id, conversation_id, chat_data,
                    deletes=state.get("chat_data_deletes"),
                )

            if not isolated:
                await _save_user_data(
                    db, user_id, user_data,
                    deletes=state.get("user_data_deletes"),
                )

    if internal_chat_id and state["accumulated_memories"]:
        from app.services.conversation import save_memories_for_chat
        await save_memories_for_chat(internal_chat_id, user_id, state["accumulated_memories"])
        logger.info(
            "Cantrip memory updates: %d keys for chat %s",
            len(state["accumulated_memories"]), internal_chat_id[:12],
        )

    final_personality = context["character"]["personality"]
    final_scenario = context["character"]["scenario"]
    final_example_dialogs = context["character"]["example_dialogs"]

    character_changed = (
        final_personality != seeded_character["personality"]
        or final_scenario != seeded_character["scenario"]
        or final_example_dialogs != seeded_character["example_dialogs"]
    )

    if character_changed:
        body_json["messages"] = apply_cantrip_result_to_messages(
            messages,
            final_personality,
            final_scenario,
            final_example_dialogs,
        )
        logger.info(
            "Cantrips processed: %d scripts, personality=%d chars, scenario=%d chars, dialogs=%d chars",
            len(cantrips),
            len(final_personality),
            len(final_scenario),
            len(final_example_dialogs),
        )

    return body_json


async def _execute_cantrip_step(
    cantrip: Any,
    *,
    db: AsyncSession,
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    position: str,
    context: dict[str, Any],
    state: dict[str, Any],
    binding_conv_id: str,
    internal_chat_id: str,
    isolated: bool,
    store_seeds: dict[str, Any] | None,
    passenger_handler: Any,
    budget_allocations: dict[str, Any] | None,
    cantrips_for_seeding: list[Any],
) -> tuple[CantripResult | None, list]:
    """Run ONE cantrip of a pass: the extracted body of the process_cantrips
    loop, shared verbatim with the manual-order orchestrator
    (pipeline_order.py) so the two orderings execute cantrips through one
    code path and cannot drift.

    Mutates ``state`` (chat_data / user_data / accumulated_memories /
    lorebook seed) and ``context`` (character fields, memories, budget,
    per-cantrip lorebook read seed). Returns ``(result, prompt_request_rows)``;
    ``result is None`` means the cantrip raised and was recorded -- the
    caller skips its request handling, exactly as the loop's ``continue``
    did. The CALLER decides what the request rows and prompt strips do:
    DELTA_KEY / body-key in the default pass, at-slot application in manual
    order.
    """
    from app.services.budget import build_cantrip_budget_context
    from app.services.budget import estimate_tokens as _est_tokens
    from app.services.debug_metrics import record_cantrip
    from app.services.metrics import record_prompt_object

    ephemeral = getattr(cantrip, "ephemeral", False)
    if budget_allocations and not ephemeral:
        context["budget"] = build_cantrip_budget_context(
            budget_allocations, cantrip.id, cantrip.budget_weight
        )

    # Per-cantrip lorebook seed: bound books for everyone, this cantrip's
    # configured reads for it alone.
    context["__lorebook"] = _seed_for_cantrip(
        state["lorebook_seed"], state["lorebook_always"], parse_reads(cantrip.lorebook_reads)
    )

    # An embedded script owns no cantrip_data: its id is per-request,
    # not stable identity, so persisting under it would collide
    # across scripts and requests.
    if ephemeral:
        cantrip_data = {}
    elif store_seeds is not None:
        cantrip_data = dict(store_seeds["cantrip_data"].get(cantrip.id, {}))
    else:
        cantrip_data = await _load_cantrip_data(db, user_id, cantrip.id)

    started = time.monotonic()
    try:
        if internal_chat_id and state["accumulated_memories"]:
            context["__memories"] = {
                **context.get("__memories", {}), **state["accumulated_memories"]
            }

        result = await run_cantrip(
            code=cantrip.code,
            context=context,
            chat_data=state["chat_data"],
            user_data=state["user_data"],
            cantrip_data=cantrip_data,
            timeout_ms=cantrip.timeout_ms,
            passenger_handler=passenger_handler,
        )
    except Exception as exc:
        logger.exception("Script '%s' failed to execute", cantrip.name)
        record_cantrip(
            body_json, cantrip_id=cantrip.id, name=cantrip.name, position=position,
            triggered=True, tag=cantrip.tag or "", code=cantrip.code,
            execution_order=cantrip.execution_order, timeout_ms=cantrip.timeout_ms,
            duration_ms=(time.monotonic() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return None, []

    if result.has_error:
        logger.warning("Script '%s' returned error: %s", cantrip.name, result.error)

    if result.debug_logs:
        logger.debug(
            "Script '%s' logs: %s", cantrip.name, " | ".join(result.debug_logs)
        )

    # The whole CantripResult used to be discarded once its fields were
    # accumulated, so the Debug timeline could not name a single cantrip
    # that had run. This is the record the comparison view diffs.
    #
    # The data stores are captured as before/after deltas rather than as
    # output fields, because cantrips at the same position run in
    # sequence against a shared chat_data and user_data: the second
    # cantrip reads what the first one wrote. Recording only the final
    # state would attribute every change to whichever ran last.
    record_cantrip(
        body_json, cantrip_id=cantrip.id, name=cantrip.name, position=position,
        triggered=True, tag=cantrip.tag or "", code=cantrip.code,
        execution_order=cantrip.execution_order, timeout_ms=cantrip.timeout_ms,
        duration_ms=(time.monotonic() - started) * 1000.0,
        debug_logs=result.debug_logs, error=result.error or "",
        output={
            "personality": result.personality,
            "scenario": result.scenario,
            "example_dialogs": result.example_dialogs,
            "memories": result.memories,
            "tool_result": result.tool_result,
        },
        data_changes={
            "chat_data": _diff_store(state["chat_data"], result.chat_data),
            "user_data": _diff_store(state["user_data"], result.user_data),
            "cantrip_data": _diff_store(cantrip_data, result.cantrip_data),
            **({"lorebook_ops": result.lorebook_ops} if result.lorebook_ops else {}),
        },
    )

    # Per-cantrip prompt delta: attribute the character-field change to
    # the cantrip that made it, measured against the values in place
    # before it ran (the previous cantrip's output, or the card's own).
    record_prompt_object(
        body_json, "cantrip", cantrip.id,
        _est_tokens(context["character"]["personality"])
        + _est_tokens(context["character"]["scenario"])
        + _est_tokens(context["character"]["example_dialogs"]),
        _est_tokens(result.personality)
        + _est_tokens(result.scenario)
        + _est_tokens(result.example_dialogs),
    )

    context["character"]["personality"] = result.personality
    context["character"]["scenario"] = result.scenario
    context["character"]["example_dialogs"] = result.example_dialogs
    state["accumulated_memories"].update(result.memories)

    # Map nominations ride the body like memories do; the proxy's map
    # branch merges them into selection after the cantrip pass.
    if result.maps_nominated:
        nominations = body_json.setdefault("_gitv_map_nominations", [])
        for tag in result.maps_nominated:
            if tag not in nominations:
                nominations.append(tag)

    state["chat_data"] = result.chat_data
    state["user_data"] = result.user_data
    # 0.42.0: aggregate the pass's store deletes. Sequential cantrips share
    # the state dict, so a later set of a deleted key simply wins (the
    # final dict is the truth; the save skips keys still present).
    state.setdefault("chat_data_deletes", set()).update(result.chat_data_deletes)
    state.setdefault("user_data_deletes", set()).update(result.user_data_deletes)

    if store_seeds is not None:
        if not ephemeral:
            store_seeds["cantrip_data"][cantrip.id] = result.cantrip_data
    elif result.cantrip_data != cantrip_data and not isolated and not ephemeral:
        await _save_cantrip_data(
            db, user_id, cantrip.id, result.cantrip_data,
            deletes=set(result.cantrip_data_deletes),
        )

    # Phase 20: apply this cantrip's lorebook ops now, not after the
    # pass, so the next cantrip's seed reflects them (the shared
    # chat_data merge, mirrored). Refusals degrade inside apply_ops.
    if result.lorebook_ops:
        delta_rows = await apply_ops(
            db, body_json, user_id, binding_conv_id,
            result.lorebook_ops, source=cantrip.name,
        )
        # Same-turn injection only exists before the forward: the
        # pre_driver position and map stage passes (which run
        # pre_driver). Response-side passes write for next turn.
        if delta_rows and position == "pre_driver":
            body_json.setdefault(DELTA_KEY, []).extend(delta_rows)
        state["lorebook_seed"], state["lorebook_always"], _ = (
            await _prepare_lorebook_seeding(
                db, body_json, user_id, request_headers, internal_chat_id,
                cantrips_for_seeding, capture=False,
            )
        )

    request_rows: list = []
    if result.book_activations or result.injections:
        request_rows = await _apply_prompt_requests(
            db, user_id, body_json,
            result.book_activations, result.injections,
            source=cantrip.name,
        )
    return result, request_rows


def apply_prompt_strips(
    body_json: dict[str, Any],
    strips: list[dict],
    cantrip_id: str = "",
    cantrip_name: str = "",
) -> int:
    """Apply context.strip() requests (pipeline ordering, 0.41.0).

    Rewrites message content in place -- string contents and text parts --
    honoring each strip's scope ('all' / 'system' / 'messages'). A pattern
    that does not compile degrades to skipped-with-capture, never an
    error. Every applied strip is debug-captured and recorded as a
    negative prompt-object delta. Returns the number applied.

    Deliberately never touches the conversation-hash anchor: identity was
    resolved before any cantrip ran, and the same strips fire on the same
    bytes every turn, so stripping is continuity-neutral by construction.
    """
    import re as _re

    from app.services.budget import estimate_tokens as _est_tokens
    from app.services.debug import debug_capture
    from app.services.metrics import record_prompt_object

    messages = body_json.get("messages", [])
    source = cantrip_name or cantrip_id or "unknown"
    applied = 0
    for raw in strips[:20]:
        pattern = str((raw or {}).get("pattern", ""))[:256]
        scope = (raw or {}).get("scope", "all")
        if scope not in ("all", "system", "messages"):
            scope = "all"
        if not pattern:
            continue
        try:
            matcher = _re.compile(pattern)
        except _re.error as exc:
            logger.warning(
                "prompt strip from '%s' did not compile, skipped: %s", source, exc
            )
            debug_capture(
                body_json, "prompt_strip", "Prompt Strip",
                detail=f"pattern from '{source}' refused (compile error): {exc}",
                metadata={"source": source, "scope": scope, "applied": False},
            )
            continue

        removed_chars = 0
        for msg in messages:
            role = msg.get("role", "")
            if scope == "system" and role != "system":
                continue
            if scope == "messages" and role == "system":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                new_content = matcher.sub("", content)
                if new_content != content:
                    removed_chars += len(content) - len(new_content)
                    msg["content"] = new_content
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        new_text = matcher.sub("", item["text"])
                        if new_text != item["text"]:
                            removed_chars += len(item["text"]) - len(new_text)
                            item["text"] = new_text
        applied += 1
        debug_capture(
            body_json, f"prompt_strip_{applied}", "Prompt Strip",
            detail=f"'{source}' stripped ~{removed_chars} chars (scope {scope})",
            metadata={
                "source": source,
                "scope": scope,
                "pattern": pattern[:80],
                "removed_chars": removed_chars,
            },
        )
        record_prompt_object(
            body_json, "prompt_strip", cantrip_id or source,
            0, -max(1, _est_tokens("x" * removed_chars)),
        )
    return applied


async def test_cantrip(
    code: str,
    context: dict,
    chat_data: dict | None = None,
    user_data: dict | None = None,
    cantrip_data: dict | None = None,
    timeout_ms: int = 5000,
    passenger_handler: Any | None = None,
) -> CantripResult:
    """Run one cantrip outside the pipeline (Cantrip Tester, smoke runs).

    Passenger is opt-in here: the Tester passes a handler built against a
    sandbox-marked synthetic body; the auto-install smoke run passes nothing,
    so a smoke test never makes real LLM calls.
    """
    return await run_cantrip(
        code, context, chat_data, user_data, cantrip_data, timeout_ms,
        passenger_handler=passenger_handler,
    )


async def _apply_prompt_requests(
    db: AsyncSession,
    user_id: str,
    body_json: dict[str, Any],
    activations: list[str],
    injections: list[dict],
    source: str,
) -> list:
    """Apply context.activate()/context.inject() requests (Package 3).

    Activation is a union source under rule 19: each named book (the user's
    own static lorebook, exact-name match) has its entries keyword-matched
    against the current message list -- honoring the book's scan_depth --
    and the matches become delta rows with full provenance. Nothing
    persists; the request-scoped activation dies with the request.

    Injections convert straight to delta rows (name/provenance empty --
    the script is the source). Bounded: 20 per request at the facade, and
    here 32k chars each / 64k total; refusals drop the single item with a
    capture, never the request. Returns the delta rows for the caller to
    extend into DELTA_KEY. Never raises.
    """
    from app.services.debug import debug_capture
    from app.services.lorebook import MatchedEntry, match_book_entries
    from app.services.metrics import record_prompt_object

    rows: list = []
    notes: list[dict[str, Any]] = []

    try:
        messages = body_json.get("messages", [])

        for raw_name in activations[:10]:
            name = str(raw_name or "").strip()[:128]
            if not name:
                continue
            book = (
                await db.execute(
                    select(Lorebook)
                    .where(Lorebook.user_id == user_id, Lorebook.name == name)
                    .options(selectinload(Lorebook.entries))
                    .order_by(Lorebook.created_at)
                )
            ).scalars().first()
            if book is None:
                notes.append({"kind": "activate", "book": name, "outcome": "refused",
                              "reason": "no lorebook of that name"})
                continue
            entry_dicts = [
                {
                    "lorebook_id": book.id,
                    "lorebook_name": book.name,
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
                for entry in book.entries
            ]
            matched = match_book_entries(messages, entry_dicts, scan_depth=book.scan_depth, word_boundary=book.word_boundary_keys)
            rows.extend(matched)
            notes.append({"kind": "activate", "book": name, "outcome": "activated",
                          "matched": len(matched), "entries": len(entry_dicts)})
            if matched:
                record_prompt_object(
                    body_json, "book_activation", book.id,
                    0, sum(len(m.content) for m in matched),
                )

        total_chars = 0
        for i, req in enumerate(injections[:20]):
            text = str(req.get("text") or "")
            position = str(req.get("position") or "before_last_message")
            if position not in ("system_start", "before_last_message", "in_chat_system"):
                position = "before_last_message"
            if not text.strip():
                notes.append({"kind": "inject", "index": i, "outcome": "refused",
                              "reason": "empty text"})
                continue
            if len(text) > 32_000:
                notes.append({"kind": "inject", "index": i, "outcome": "refused",
                              "reason": "over the 32k per-injection cap"})
                continue
            if total_chars + len(text) > 64_000:
                notes.append({"kind": "inject", "index": i, "outcome": "refused",
                              "reason": "over the 64k per-request injection cap"})
                continue
            total_chars += len(text)
            rows.append(MatchedEntry(
                content=text, position=position,
                insertion_order=1000 + i, name="",
            ))
            record_prompt_object(body_json, "injection", "", 0, max(1, len(text) // 4))
            notes.append({"kind": "inject", "index": i, "position": position,
                          "outcome": "injected", "chars": len(text)})

        debug_capture(
            body_json, "prompt_requests", "Prompt Requests (activate/inject)",
            detail=(f"{len(activations)} activation(s), {len(injections)} injection(s) "
                    f"from '{source}': {len(rows)} delta row(s)"),
            metadata={"source": source, "requests": notes},
        )
        return rows
    except Exception:
        logger.exception("Prompt-request application failed (source '%s')", source)
        return rows


async def process_cantrips_post_driver(
    response_content: str,
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
    position: str = "pre_navigator",
    only_ids: set[str] | None = None,
    stage_index: int | None = None,
    embedded_scripts: list[EmbeddedScriptCantrip] | None = None,
    store_seeds: dict[str, Any] | None = None,
) -> str:
    """Run post-Driver cantrips that can modify the response content.

    Used for pre-Navigator (regex/keyword checks, cleanup) and post-Navigator
    (final formatting) positions. Cantrips at these positions have access to
    context.response.content. ``only_ids`` restricts the pass to specific
    cantrips (map stage capture mode runs just the stage's own).
    ``stage_index`` attributes Passenger LLM-call rows to a map stage.
    ``embedded_scripts`` adds <jslorebook> scripts detected for this position.

    ``store_seeds`` (sequence tester) mirrors the pre-forward pass: an
    in-memory store holder that replaces DB loads and saves, mutated dicts
    written back for the caller to thread onward. ``None`` keeps today's
    behavior.
    """
    messages = body_json.get("messages", [])
    if not messages:
        return response_content

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        from app.services.group_resolver import group_direct_members

        all_cantrips: list[Any] = await _load_active_cantrips(
            db, user_id, position, direct_member_ids=group_direct_members(body_json)
        )
        if embedded_scripts:
            all_cantrips = [*all_cantrips, *embedded_scripts]
            from app.services.debug import debug_capture

            debug_capture(
                body_json, "jslorebook_execution", "jslorebook Scripts",
                detail=(
                    f"{len(embedded_scripts)} embedded script(s) executing at "
                    f"'{position}' (sandboxed, guarded)"
                ),
                metadata={
                    "position": position,
                    "scripts": [{"id": s.id, "name": s.name} for s in embedded_scripts],
                },
            )
        if not all_cantrips:
            return response_content

        from app.services.group_resolver import group_direct_members
        from app.services.tagging import should_activate_resource
        direct_ids = group_direct_members(body_json)
        cantrips = []
        for c in all_cantrips:
            if c.id in direct_ids or should_activate_resource(
                c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags or []
            ):
                cantrips.append(c)

        if only_ids is not None:
            cantrips = [c for c in cantrips if c.id in only_ids]

        if not cantrips:
            return response_content

        conversation_id = params.get("conversation_id", "")
        isolated = _store_writes_isolated(body_json)
        if store_seeds is not None:
            store_seeds.setdefault("chat_data", {})
            store_seeds.setdefault("user_data", {})
            store_seeds.setdefault("cantrip_data", {})
            chat_data = dict(store_seeds["chat_data"])
            user_data = dict(store_seeds["user_data"])
        else:
            chat_data = await _load_chat_data(db, user_id, conversation_id)
            user_data = await _load_user_data(db, user_id)
        # 0.42.0: keys deleted by this pass's scripts (persisted at the
        # pass-level save below).
        chat_data_deletes: set = set()
        user_data_deletes: set = set()

        # Phase 20: same seeding/ops as the pre-forward pass, minus the
        # same-turn injection (the prompt has already been sent; entries
        # written here inject next turn via the conversation binding).
        lorebook_seed, lorebook_always, binding_conv_id = await _prepare_lorebook_seeding(
            db, body_json, user_id, request_headers,
            body_json.get("_gitv_chat_id", ""), cantrips,
        )

        context = build_context(messages, **params)
        context["response"] = {
            "content": response_content,
            "original_content": response_content,
            "modified": False,
        }
        # Package 3 (item 3): this turn's registered-tag extractions, seeded
        # by process_response_capture just before this pass. The owning
        # cantrip reads context.response_tags[name] -> array of payloads --
        # same-turn delivery, no raw tag text ever reaches the script.
        context["response_tags"] = body_json.get("_gitv_response_tags") or {}

        current_content = response_content

        from app.services.budget import estimate_tokens as _est_tokens
        from app.services.metrics import record_prompt_object
        from app.services.passenger import get_passenger_handler

        passenger_handler = await get_passenger_handler(body_json, user_id)
        passenger_handler.stage_index = stage_index

        for cantrip in cantrips:
            ephemeral = getattr(cantrip, "ephemeral", False)
            context["__lorebook"] = _seed_for_cantrip(
                lorebook_seed, lorebook_always, parse_reads(cantrip.lorebook_reads)
            )

            if ephemeral:
                cantrip_data = {}
            elif store_seeds is not None:
                cantrip_data = dict(store_seeds["cantrip_data"].get(cantrip.id, {}))
            else:
                cantrip_data = await _load_cantrip_data(db, user_id, cantrip.id)

            try:
                context["response"]["content"] = current_content
                started = time.monotonic()
                result = await run_cantrip(
                    code=cantrip.code,
                    context=context,
                    chat_data=chat_data,
                    user_data=user_data,
                    cantrip_data=cantrip_data,
                    timeout_ms=cantrip.timeout_ms,
                    passenger_handler=passenger_handler,
                )
            except Exception:
                logger.exception("Post-driver cantrip '%s' failed", cantrip.name)
                continue

            if result.has_error:
                logger.warning("Post-driver cantrip '%s' error: %s", cantrip.name, result.error)

            if result.debug_logs:
                logger.debug(
                    "Post-driver cantrip '%s' logs: %s",
                    cantrip.name, " | ".join(result.debug_logs),
                )

            # Response-content delta for this cantrip, against the content in
            # place before it ran (the previous cantrip's output).
            prev_content_tokens = _est_tokens(current_content)
            if result.response_content is not None and result.response_content != current_content:
                current_content = result.response_content
                context["response"]["modified"] = True
            record_prompt_object(
                body_json, "cantrip", cantrip.id,
                prev_content_tokens, _est_tokens(current_content),
            )

            # 0.42.0 (N4c): navigator-position runs belong in the Debug
            # trace like every other position. Console output used to be
            # reachable only at debug log level, and run comparison could
            # not see response-side cantrips at all.
            from app.services.debug_metrics import record_cantrip

            record_cantrip(
                body_json, cantrip_id=cantrip.id, name=cantrip.name, position=position,
                triggered=True, tag=cantrip.tag or "", code=cantrip.code,
                execution_order=cantrip.execution_order, timeout_ms=cantrip.timeout_ms,
                duration_ms=(time.monotonic() - started) * 1000.0,
                debug_logs=result.debug_logs, error=result.error or "",
                output={
                    "response_content": result.response_content,
                    "tool_result": result.tool_result,
                },
                data_changes={
                    "chat_data": _diff_store(chat_data, result.chat_data),
                    "user_data": _diff_store(user_data, result.user_data),
                    "cantrip_data": _diff_store(cantrip_data, result.cantrip_data),
                    **({"lorebook_ops": result.lorebook_ops} if result.lorebook_ops else {}),
                },
            )

            chat_data = result.chat_data
            user_data = result.user_data
            # 0.42.0: aggregate the pass's store deletes (same semantics as
            # the pre-forward pass: a later set of a deleted key wins).
            chat_data_deletes.update(result.chat_data_deletes)
            user_data_deletes.update(result.user_data_deletes)

            if store_seeds is not None:
                if not ephemeral:
                    store_seeds["cantrip_data"][cantrip.id] = result.cantrip_data
            elif result.cantrip_data != cantrip_data and not isolated and not ephemeral:
                await _save_cantrip_data(
                    db, user_id, cantrip.id, result.cantrip_data,
                    deletes=set(result.cantrip_data_deletes),
                )

            if result.lorebook_ops:
                await apply_ops(
                    db, body_json, user_id, binding_conv_id,
                    result.lorebook_ops, source=cantrip.name,
                )
                lorebook_seed, lorebook_always, _ = await _prepare_lorebook_seeding(
                    db, body_json, user_id, request_headers,
                    body_json.get("_gitv_chat_id", ""), cantrips, capture=False,
                )

        if store_seeds is not None:
            # The seeds holder IS the store for this run (same as the
            # pre-forward pass): write the final state back for threading.
            store_seeds["chat_data"] = chat_data
            store_seeds["user_data"] = user_data
        else:
            # Same isolation rules as process_cantrips: the fork owns chat_data
            # for a sandbox, a replay writes nothing, and user/cantrip-scoped
            # stores are never written from an isolated run.
            if conversation_id and not body_json.get("_gitv_replay"):
                await _save_chat_data(
                    db, user_id, conversation_id, chat_data,
                    deletes=chat_data_deletes or None,
                )

            if not isolated:
                await _save_user_data(db, user_id, user_data, deletes=user_data_deletes or None)

    if current_content != response_content:
        logger.info(
            "Post-driver cantrips (%s): %d scripts, response modified %d -> %d chars",
            position, len(cantrips), len(response_content), len(current_content),
        )

    return current_content
