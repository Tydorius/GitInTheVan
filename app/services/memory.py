from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.memory import Memory

logger = logging.getLogger(__name__)

MEMSTORE_TAG_PATTERN = re.compile(
    r"<memstore\s+key=[\"']([^\"']+)[\"']\s*>(.*?)</memstore>",
    re.DOTALL | re.IGNORECASE,
)

MEMSTORE_INLINE_PATTERN = re.compile(
    r"<memstore\s+key=[\"']([^\"']+)[\"']\s+value=[\"']([^\"']*)[\"']\s*/>",
    re.IGNORECASE,
)


async def load_memories(db: AsyncSession, user_id: str, conversation_id: str) -> dict[str, str]:
    if not conversation_id:
        return {}
    result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == conversation_id,
        )
    )
    rows = result.scalars().all()
    return {row.key: row.value for row in rows}


async def save_memory(
    db: AsyncSession, user_id: str, conversation_id: str, key: str, value: str
) -> None:
    if not conversation_id:
        return
    result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == conversation_id,
            Memory.key == key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.value = value
    else:
        db.add(
            Memory(
                user_id=user_id,
                conversation_id=conversation_id,
                key=key,
                value=value,
            )
        )
    await db.commit()


async def save_memories_batch(
    db: AsyncSession, user_id: str, conversation_id: str, memories: dict[str, str]
) -> None:
    if not conversation_id or not memories:
        return

    result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == conversation_id,
        )
    )
    existing = {row.key: row for row in result.scalars().all()}

    for key, value in memories.items():
        if key in existing:
            if existing[key].value != value:
                existing[key].value = value
        else:
            db.add(
                Memory(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    key=key,
                    value=value,
                )
            )

    await db.commit()


def parse_memstore_tags(text: str) -> tuple[str, dict[str, str]]:
    memories: dict[str, str] = {}

    for match in MEMSTORE_TAG_PATTERN.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        memories[key] = value

    for match in MEMSTORE_INLINE_PATTERN.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        memories[key] = value

    cleaned = MEMSTORE_TAG_PATTERN.sub("", text)
    cleaned = MEMSTORE_INLINE_PATTERN.sub("", cleaned)

    return cleaned, memories


def build_memory_context_block(memories: dict[str, str]) -> str:
    if not memories:
        return ""

    lines = ["[PERSISTENT MEMORY]"]
    for key, value in sorted(memories.items()):
        lines.append(f"{key}: {value}")
    lines.append("[/PERSISTENT MEMORY]")

    return "\n".join(lines)


async def process_memories_for_request(
    body_json: dict[str, Any],
    user_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    if not conversation_id:
        return body_json

    try:
        async with async_session() as db:
            memories = await load_memories(db, user_id, conversation_id)

        if memories:
            memory_block = build_memory_context_block(memories)
            if memory_block:
                messages = body_json.get("messages", [])
                system_idx = None
                for i, msg in enumerate(messages):
                    if msg.get("role") == "system":
                        system_idx = i
                        break

                if system_idx is not None:
                    messages[system_idx] = {
                        **messages[system_idx],
                        "content": messages[system_idx]["content"] + "\n\n" + memory_block,
                    }
                else:
                    messages.insert(0, {"role": "system", "content": memory_block})

                body_json["messages"] = messages
                logger.info(
                    "Memory injection: %d keys loaded for conversation %s",
                    len(memories),
                    conversation_id[:8],
                )
    except Exception:
        logger.exception("Memory loading failed")

    return body_json


async def process_memories_from_response(
    response_content: str,
    user_id: str,
    conversation_id: str,
) -> str:
    if not conversation_id:
        return response_content

    cleaned, memories = parse_memstore_tags(response_content)

    if memories:
        try:
            async with async_session() as db:
                await save_memories_batch(db, user_id, conversation_id, memories)
            logger.info(
                "Memory extraction: %d keys saved from response for conversation %s",
                len(memories),
                conversation_id[:8],
            )
        except Exception:
            logger.exception("Memory saving failed")

    return cleaned


def build_cantrip_memory_object(memories: dict[str, str]) -> dict[str, Any]:
    local_memories = dict(memories)

    return {
        "get": lambda key: local_memories.get(key),
        "set": lambda key, value: local_memories.__setitem__(key, str(value)),
        "keys": lambda: list(local_memories.keys()),
        "delete": lambda key: local_memories.pop(key, None),
        "all": lambda: dict(local_memories),
    }
