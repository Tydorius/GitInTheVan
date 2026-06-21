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


async def _load_active_cantrips(db: AsyncSession, user_id: str) -> list[Cantrip]:
    result = await db.execute(
        select(Cantrip)
        .where(
            Cantrip.user_id == user_id,
            Cantrip.is_active.is_(True),
            Cantrip.hook_type == "pre",
        )
        .order_by(Cantrip.execution_order, Cantrip.created_at)
    )
    return list(result.scalars().all())


async def process_cantrips(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    tags: list | None = None,
) -> dict[str, Any]:
    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    params = extract_context_params(body_json, request_headers)

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id)
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

        accumulated_personality = ""
        accumulated_scenario = ""
        accumulated_example_dialogs = ""

        for cantrip in cantrips:
            try:
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

            chat_data = result.chat_data

        if conversation_id:
            await _save_chat_data(db, user_id, conversation_id, chat_data)

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
