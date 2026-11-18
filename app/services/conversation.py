from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.conversation_hash import ConversationHash
from app.models.memory import Memory

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = 4


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
        return " ".join(parts)
    return str(content) if content else ""


def _messages_to_hashable(messages: list[dict[str, Any]]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = _extract_text(msg.get("content", ""))
        parts.append(f"{role}:{content}")
    return "\n".join(parts)


def compute_hash(messages: list[dict[str, Any]], window: int = DEFAULT_WINDOW) -> str:
    if not messages:
        return ""

    items = []
    for msg in messages[-window:]:
        role = msg.get("role", "")
        content = _extract_text(msg.get("content", ""))
        items.append(f"{role}|{content[:2000]}")

    raw = "\n".join(items)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _prior_messages_for_hash(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select the message slice to hash for conversation resolution.

    The current user message (last) is always excluded. The preceding
    assistant message is also excluded when present, because users frequently
    edit or swipe the LLM's most recent response before sending their next
    message. Hashing therefore ends at the prior user message so such edits
    do not orphan the conversation's memories and summaries.
    """
    if len(messages) < 2:
        return []
    cut = len(messages) - 1
    if len(messages) >= 2 and messages[-2].get("role") == "assistant":
        cut = len(messages) - 2
    return messages[:cut]


async def resolve_conversation(
    messages: list[dict[str, Any]],
    user_id: str,
    window: int = DEFAULT_WINDOW,
) -> tuple[str, bool]:
    """Resolve internal chat ID from message history.

    Returns (internal_chat_id, is_new).
    """
    if len(messages) < 2:
        return await _create_new_conversation(user_id)

    prior_messages = _prior_messages_for_hash(messages)
    pre_hash = compute_hash(prior_messages, window)

    if not pre_hash:
        return await _create_new_conversation(user_id)

    async with async_session() as db:
        existing = await _find_by_hash(db, user_id, pre_hash)
        if existing:
            return existing.internal_chat_id, False

        legacy_hash = compute_hash(messages[:-1], window)
        if legacy_hash and legacy_hash != pre_hash:
            legacy = await _find_by_hash(db, user_id, legacy_hash)
            if legacy:
                return legacy.internal_chat_id, False

        return await _create_new_conversation(user_id)


async def _create_new_conversation(user_id: str) -> tuple[str, bool]:
    return str(uuid.uuid4()), True


async def _find_by_hash(db: AsyncSession, user_id: str, hash_value: str) -> ConversationHash | None:
    result = await db.execute(
        select(ConversationHash).where(
            ConversationHash.user_id == user_id,
            ConversationHash.hash_value == hash_value,
        )
    )
    return result.scalar_one_or_none()


async def record_post_hash(
    messages: list[dict[str, Any]],
    bot_response_content: str,
    internal_chat_id: str,
    user_id: str,
    window: int = DEFAULT_WINDOW,
) -> str:
    """Record a hash of the request messages so the next request can chain.

    The bot response is intentionally excluded from the hash. Users may edit
    or swipe the LLM's most recent reply before their next turn; resolve_*
    tolerates that by excluding the trailing assistant message, so the
    recorded anchor must be the request messages exactly as the user sent
    them. bot_response_content is accepted for interface compatibility and
    potential future use (e.g. logging) but is not part of the hash.
    """
    post_hash = compute_hash(messages, window)

    if not post_hash:
        return ""

    async with async_session() as db:
        existing = await _find_by_hash(db, user_id, post_hash)
        if not existing:
            db.add(
                ConversationHash(
                    user_id=user_id,
                    hash_value=post_hash,
                    internal_chat_id=internal_chat_id,
                )
            )
            await db.commit()

    return post_hash


async def load_memories_for_chat(
    internal_chat_id: str, user_id: str
) -> dict[str, str]:
    async with async_session() as db:
        result = await db.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.conversation_id == internal_chat_id,
            )
        )
        rows = result.scalars().all()
        return {row.key: row.value for row in rows}


async def save_memories_for_chat(
    internal_chat_id: str, user_id: str, memories: dict[str, str]
) -> None:
    if not memories:
        return

    async with async_session() as db:
        result = await db.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.conversation_id == internal_chat_id,
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
                        conversation_id=internal_chat_id,
                        key=key,
                        value=value,
                    )
                )

        await db.commit()


async def bump_turn_counter(user_id: str, conversation_id: str) -> int:
    """Advance and return the monotonic per-conversation turn index.

    `context.chat.message_count` is len(messages) of the client request, so
    it stops growing once the client trims history. This counts client
    requests instead and only ever goes up. One client request = one
    increment; internal rounds (verification retries, driver-callable loops,
    map stages) never call it. Keyed by the Phase-29 conversation identity,
    the same id lexicon bindings and chat_data scoping use.

    Concurrency: the increment is a single atomic UPDATE. Two truly parallel
    requests in one chat can observe the same index under a race; that never
    decreases the counter and is acceptable for roleplay, where one chat has
    one writer.
    """
    from datetime import UTC, datetime

    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    from app.models.conversation_turn_counter import ConversationTurnCounter

    async with async_session() as db:
        await db.execute(
            update(ConversationTurnCounter)
            .where(
                ConversationTurnCounter.user_id == user_id,
                ConversationTurnCounter.conversation_id == conversation_id,
            )
            .values(
                turn_count=ConversationTurnCounter.turn_count + 1,
                updated_at=datetime.now(UTC),
            )
        )
        row = (await db.execute(
            select(ConversationTurnCounter.turn_count).where(
                ConversationTurnCounter.user_id == user_id,
                ConversationTurnCounter.conversation_id == conversation_id,
            )
        )).scalar_one_or_none()

        if row is None:
            # First request in this conversation. A concurrent first request
            # can win the insert; the loser retries the update path once.
            db.add(
                ConversationTurnCounter(
                    user_id=user_id, conversation_id=conversation_id, turn_count=1
                )
            )
            try:
                await db.commit()
                return 1
            except IntegrityError:
                await db.rollback()
                await db.execute(
                    update(ConversationTurnCounter)
                    .where(
                        ConversationTurnCounter.user_id == user_id,
                        ConversationTurnCounter.conversation_id == conversation_id,
                    )
                    .values(
                        turn_count=ConversationTurnCounter.turn_count + 1,
                        updated_at=datetime.now(UTC),
                    )
                )
                await db.commit()
                row = (await db.execute(
                    select(ConversationTurnCounter.turn_count).where(
                        ConversationTurnCounter.user_id == user_id,
                        ConversationTurnCounter.conversation_id == conversation_id,
                    )
                )).scalar_one()
                return row

        await db.commit()
        return row
