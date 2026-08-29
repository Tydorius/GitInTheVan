from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.cantrip_data import CantripData
from app.models.chat_data import ChatData
from app.models.user_data import UserData
from app.services.cantrip_context import apply_cantrip_result_to_messages, build_context, extract_context_params
from app.services.deno_runner import CantripResult, run_cantrip

logger = logging.getLogger(__name__)


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
    db: AsyncSession, user_id: str, conversation_id: str, chat_data: dict[str, Any]
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
    db: AsyncSession, user_id: str, user_data: dict[str, Any]
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
    db: AsyncSession, user_id: str, cantrip_id: str, cantrip_data: dict[str, Any]
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
    db: AsyncSession, user_id: str, position: str = "pre_driver"
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
    query = (
        select(Cantrip)
        .where(
            Cantrip.user_id == user_id,
            or_(Cantrip.is_active.is_(True), Cantrip.tag != ""),
            flag.is_(True),
        )
        .order_by(Cantrip.execution_order, Cantrip.created_at)
    )
    result = await db.execute(query)
    return list(result.scalars().all())


async def process_cantrips(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
    position: str = "pre_driver",
    internal_chat_id: str = "",
    only_ids: set[str] | None = None,
    exclude_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Run cantrips at a pipeline position.

    ``only_ids`` and ``exclude_ids`` scope the run to a subset of the user's
    cantrips. The map pipeline uses them to honour stage attachments: the
    pre-stage "global" pass excludes every cantrip bound to a stage, and each
    stage runs only the cantrips attached to it. Without that split a cantrip
    with side effects -- a dice roll, a balance change, a counter -- fires once
    globally and again on every stage of the map.
    """
    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id, position)
        if not all_cantrips:
            return body_json

        if only_ids is not None:
            all_cantrips = [c for c in all_cantrips if c.id in only_ids]
        if exclude_ids:
            all_cantrips = [c for c in all_cantrips if c.id not in exclude_ids]
        if not all_cantrips:
            return body_json

        from app.services.debug_metrics import record_cantrip
        from app.services.tagging import should_activate_resource
        cantrips = []
        for c in all_cantrips:
            # Asked for every candidate. There used to be an `if c.tag and tags`
            # pre-check with an unconditional `else: append`, which skipped the
            # hierarchy entirely whenever the request carried no tags -- so an
            # inactive cantrip ran anyway.
            if should_activate_resource(
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
        chat_data = await _load_chat_data(db, user_id, conversation_id)
        user_data = await _load_user_data(db, user_id)

        context = build_context(messages, **params)

        if internal_chat_id:
            from app.services.conversation import load_memories_for_chat

            memory_map = await load_memories_for_chat(internal_chat_id, user_id)
            context["__memories"] = memory_map

        from app.services.budget import build_cantrip_budget_context

        budget_allocations = body_json.get("_gitv_budget_allocations")

        accumulated_memories: dict[str, str] = {}
        accumulated_personality = ""
        accumulated_scenario = ""
        accumulated_example_dialogs = ""

        for cantrip in cantrips:
            if budget_allocations:
                context["budget"] = build_cantrip_budget_context(
                    budget_allocations, cantrip.id, cantrip.budget_weight
                )

            cantrip_data = await _load_cantrip_data(db, user_id, cantrip.id)

            started = time.monotonic()
            try:
                if internal_chat_id and accumulated_memories:
                    context["__memories"] = {**context.get("__memories", {}), **accumulated_memories}

                result = await run_cantrip(
                    code=cantrip.code,
                    context=context,
                    chat_data=chat_data,
                    user_data=user_data,
                    cantrip_data=cantrip_data,
                    timeout_ms=cantrip.timeout_ms,
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
                continue

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
                    "chat_data": _diff_store(chat_data, result.chat_data),
                    "user_data": _diff_store(user_data, result.user_data),
                    "cantrip_data": _diff_store(cantrip_data, result.cantrip_data),
                },
            )

            accumulated_personality += result.personality
            accumulated_scenario += result.scenario
            accumulated_example_dialogs += result.example_dialogs
            accumulated_memories.update(result.memories)

            chat_data = result.chat_data
            user_data = result.user_data

            if result.cantrip_data != cantrip_data:
                await _save_cantrip_data(db, user_id, cantrip.id, result.cantrip_data)

        if conversation_id:
            await _save_chat_data(db, user_id, conversation_id, chat_data)

        await _save_user_data(db, user_id, user_data)

    if internal_chat_id and accumulated_memories:
        from app.services.conversation import save_memories_for_chat
        await save_memories_for_chat(internal_chat_id, user_id, accumulated_memories)
        logger.info("Cantrip memory updates: %d keys for chat %s", len(accumulated_memories), internal_chat_id[:12])

    if accumulated_personality or accumulated_scenario or accumulated_example_dialogs:
        body_json["messages"] = apply_cantrip_result_to_messages(
            messages,
            accumulated_personality,
            accumulated_scenario,
            accumulated_example_dialogs,
        )
        logger.info(
            "Cantrips processed: %d scripts, personality=%d chars, scenario=%d chars, dialogs=%d chars",
            len(cantrips),
            len(accumulated_personality),
            len(accumulated_scenario),
            len(accumulated_example_dialogs),
        )

    return body_json


async def test_cantrip(
    code: str,
    context: dict[str, Any],
    chat_data: dict[str, Any] | None = None,
    user_data: dict[str, Any] | None = None,
    cantrip_data: dict[str, Any] | None = None,
    timeout_ms: int = 5000,
) -> CantripResult:
    return await run_cantrip(code, context, chat_data, user_data, cantrip_data, timeout_ms)


async def process_cantrips_post_driver(
    response_content: str,
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
    position: str = "pre_navigator",
) -> str:
    """Run post-Driver cantrips that can modify the response content.

    Used for pre-Navigator (regex/keyword checks, cleanup) and
    post-Navigator (final formatting) positions. Cantrips at these
    positions have access to context.response.content.
    """
    messages = body_json.get("messages", [])
    if not messages:
        return response_content

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id, position)
        if not all_cantrips:
            return response_content

        from app.services.tagging import should_activate_resource
        cantrips = []
        for c in all_cantrips:
            if should_activate_resource(
                c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags or []
            ):
                cantrips.append(c)

        if not cantrips:
            return response_content

        conversation_id = params.get("conversation_id", "")
        chat_data = await _load_chat_data(db, user_id, conversation_id)
        user_data = await _load_user_data(db, user_id)

        context = build_context(messages, **params)
        context["response"] = {
            "content": response_content,
            "original_content": response_content,
            "modified": False,
        }

        current_content = response_content

        for cantrip in cantrips:
            cantrip_data = await _load_cantrip_data(db, user_id, cantrip.id)

            try:
                context["response"]["content"] = current_content
                result = await run_cantrip(
                    code=cantrip.code,
                    context=context,
                    chat_data=chat_data,
                    user_data=user_data,
                    cantrip_data=cantrip_data,
                    timeout_ms=cantrip.timeout_ms,
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

            if result.response_content is not None and result.response_content != current_content:
                current_content = result.response_content
                context["response"]["modified"] = True

            chat_data = result.chat_data
            user_data = result.user_data

            if result.cantrip_data != cantrip_data:
                await _save_cantrip_data(db, user_id, cantrip.id, result.cantrip_data)

        if conversation_id:
            await _save_chat_data(db, user_id, conversation_id, chat_data)

        await _save_user_data(db, user_id, user_data)

    if current_content != response_content:
        logger.info(
            "Post-driver cantrips (%s): %d scripts, response modified %d -> %d chars",
            position, len(cantrips), len(response_content), len(current_content),
        )

    return current_content
