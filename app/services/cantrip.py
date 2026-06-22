from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.chat_data import ChatData
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


async def _load_active_cantrips(
    db: AsyncSession, user_id: str, position: str = "pre_driver"
) -> list[Cantrip]:
    flag_map = {
        "pre_driver": Cantrip.run_pre_driver,
        "pre_navigator": Cantrip.run_pre_navigator,
        "post_navigator": Cantrip.run_post_navigator,
    }
    flag = flag_map.get(position, Cantrip.run_pre_driver)

    query = (
        select(Cantrip)
        .where(
            Cantrip.user_id == user_id,
            Cantrip.is_active.is_(True),
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
) -> dict[str, Any]:
    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id, position)
        if not all_cantrips:
            return body_json

        from app.services.tagging import should_activate_resource
        cantrips = []
        for c in all_cantrips:
            if c.tag and tags:
                if should_activate_resource(
                    c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags
                ):
                    cantrips.append(c)
            else:
                cantrips.append(c)

        if not cantrips:
            return body_json

        conversation_id = params.get("conversation_id", "")
        chat_data = await _load_chat_data(db, user_id, conversation_id)

        context = build_context(messages, **params)

        if internal_chat_id:
            from app.services.conversation import load_memories_for_chat

            memory_map = await load_memories_for_chat(internal_chat_id, user_id)
            context["__memories"] = memory_map

        accumulated_memories: dict[str, str] = {}
        accumulated_personality = ""
        accumulated_scenario = ""
        accumulated_example_dialogs = ""

        for cantrip in cantrips:
            try:
                if internal_chat_id and accumulated_memories:
                    context["__memories"] = {**context.get("__memories", {}), **accumulated_memories}

                result = await run_cantrip(
                    code=cantrip.code,
                    context=context,
                    chat_data=chat_data,
                    timeout_ms=cantrip.timeout_ms,
                )
            except Exception:
                logger.exception("Script '%s' failed to execute", cantrip.name)
                continue

            if result.has_error:
                logger.warning("Script '%s' returned error: %s", cantrip.name, result.error)

            if result.debug_logs:
                logger.debug(
                    "Script '%s' logs: %s", cantrip.name, " | ".join(result.debug_logs)
                )

            accumulated_personality += result.personality
            accumulated_scenario += result.scenario
            accumulated_example_dialogs += result.example_dialogs
            accumulated_memories.update(result.memories)

            chat_data = result.chat_data

        if conversation_id:
            await _save_chat_data(db, user_id, conversation_id, chat_data)

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
    timeout_ms: int = 5000,
) -> CantripResult:
    return await run_cantrip(code, context, chat_data, timeout_ms)


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
            if c.tag and tags:
                if should_activate_resource(
                    c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags
                ):
                    cantrips.append(c)
            else:
                cantrips.append(c)

        if not cantrips:
            return response_content

        conversation_id = params.get("conversation_id", "")
        chat_data = await _load_chat_data(db, user_id, conversation_id)

        context = build_context(messages, **params)
        context["response"] = {
            "content": response_content,
            "original_content": response_content,
            "modified": False,
        }

        current_content = response_content

        for cantrip in cantrips:
            try:
                context["response"]["content"] = current_content
                result = await run_cantrip(
                    code=cantrip.code,
                    context=context,
                    chat_data=chat_data,
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

        if conversation_id:
            await _save_chat_data(db, user_id, conversation_id, chat_data)

    if current_content != response_content:
        logger.info(
            "Post-driver cantrips (%s): %d scripts, response modified %d -> %d chars",
            position, len(cantrips), len(response_content), len(current_content),
        )

    return current_content
