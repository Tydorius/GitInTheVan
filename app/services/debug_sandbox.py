"""Forked, re-runnable copies of a request.

A **sandbox** is made from a debug run. It copies that run's request *and* forks
the conversation state the request read — memories, chat data, the conversation
summary — into a private conversation of its own. Firing it accumulates state in
the fork, so the second run sees what the first one wrote, exactly as a real
conversation would, while the conversation it came from is never touched.

This is the difference from **replay** (`debug_replay.py`), which is a one-shot
"same input, current config" check that writes nothing:

| | Replay | Sandbox |
|---|---|---|
| Runs | once | as often as you like |
| Conversation state | read-only, throwaway | forked, and it evolves |
| Good for | did my change alter this exact output | iterating on a scenario |

**Nothing is sent to the chat service the request originally came from.** A
sandbox run starts inside GitInTheVan and its response is returned to the Debug
UI. There is no downstream client and no outbound call to JanitorAI, Wyvern or
anything else — the only external request is to the user's own configured LLM
endpoint, which is what any run does.

Cantrip side effects are *not* suppressed here, and that is the point: a dice
roll or a balance change should happen, and should land in the fork where it can
be inspected and re-run without consequence.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from app.database import async_session
from app.models.chat_data import ChatData
from app.models.conversation_summary import ConversationSummary
from app.models.debug_sandbox import DebugSandbox
from app.models.memory import Memory
from app.services.debug import get_exchange, latest_exchange_id

logger = logging.getLogger(__name__)

# Sandbox conversation ids carry this prefix so they are recognisable in the
# database and can never collide with an id minted by resolve_conversation.
SANDBOX_CHAT_PREFIX = "sandbox:"


class SandboxError(Exception):
    """A sandbox operation failed. The message is safe to show the user."""


async def create_sandbox(user_id: str, exchange_id: str, name: str = "") -> dict[str, Any]:
    """Fork a debug run into a re-runnable sandbox."""
    source = await get_exchange(user_id, exchange_id)
    if not source:
        raise SandboxError("Run not found")

    messages = _original_messages(source)
    if not messages:
        raise SandboxError(
            "This run has no recoverable original messages, so it cannot be forked."
        )

    source_chat_id = source.get("chat_id", "")
    sandbox_chat_id = f"{SANDBOX_CHAT_PREFIX}{uuid.uuid4()}"

    async with async_session() as db:
        sandbox = DebugSandbox(
            user_id=user_id,
            name=name or source.get("label") or "Sandbox",
            source_exchange_id=exchange_id,
            source_chat_id=source_chat_id,
            sandbox_chat_id=sandbox_chat_id,
            messages=json.dumps(messages, default=str),
            model=source.get("model", ""),
        )
        db.add(sandbox)
        await db.flush()
        sandbox_id = sandbox.id

        copied = await _fork_conversation_state(db, user_id, source_chat_id, sandbox_chat_id)
        await db.commit()

    logger.info(
        "Sandbox %s forked from run %s (%d state row(s) copied)",
        sandbox_id[:8], exchange_id[:8], copied,
    )
    return await get_sandbox(user_id, sandbox_id)


async def _fork_conversation_state(
    db, user_id: str, source_chat_id: str, sandbox_chat_id: str
) -> int:
    """Copy the conversation state a run reads, keyed to the sandbox's own id.

    Copied rather than shared, so a sandbox run that writes a memory or a chat
    data key cannot change what the real conversation sees. The rolling
    conversation *hash* is deliberately not copied: it maps a message history to
    a conversation id, and duplicating it would make the real conversation
    resolve to the sandbox.
    """
    if not source_chat_id:
        return 0

    copied = 0

    memories = await db.execute(
        select(Memory).where(Memory.user_id == user_id, Memory.conversation_id == source_chat_id)
    )
    for m in memories.scalars().all():
        db.add(Memory(
            user_id=user_id, conversation_id=sandbox_chat_id,
            key=m.key, value=m.value, memory_type=m.memory_type,
        ))
        copied += 1

    chat_rows = await db.execute(
        select(ChatData).where(
            ChatData.user_id == user_id, ChatData.conversation_id == source_chat_id
        )
    )
    for row in chat_rows.scalars().all():
        db.add(ChatData(
            user_id=user_id, conversation_id=sandbox_chat_id,
            key=row.key, value_json=row.value_json,
        ))
        copied += 1

    summaries = await db.execute(
        select(ConversationSummary).where(
            ConversationSummary.user_id == user_id,
            ConversationSummary.internal_chat_id == source_chat_id,
        )
    )
    for s in summaries.scalars().all():
        db.add(ConversationSummary(
            user_id=user_id, internal_chat_id=sandbox_chat_id,
            summary=s.summary, boundary_hash=s.boundary_hash,
            message_count=s.message_count, token_estimate=s.token_estimate,
        ))
        copied += 1

    return copied


async def run_sandbox(user_id: str, sandbox_id: str) -> str:
    """Fire a sandbox. Returns the id of the debug run it produced."""
    sandbox = await _load(user_id, sandbox_id)
    if sandbox is None:
        raise SandboxError("Sandbox not found")

    try:
        messages = json.loads(sandbox["messages"])
    except (ValueError, TypeError):
        raise SandboxError("This sandbox's stored messages are unreadable.") from None
    if not messages:
        raise SandboxError("This sandbox has no messages to send.")

    from app.services.routing import resolve_routing_for_user

    async with async_session() as db:
        routing = await resolve_routing_for_user(user_id, db)
    if routing is None:
        raise SandboxError("You have no enabled endpoint to run this sandbox against.")

    target = (
        routing.base_url, routing.api_key, routing.user_id,
        routing.api_base_path, routing.bypass_method, routing.provider,
        routing.model, routing.endpoint_id, routing.failover_chain,
    )

    body = {
        "model": sandbox["model"] or routing.model,
        "messages": messages,
        # Buffered: the caller needs the resulting run id, and there is no
        # streaming client to serve.
        "stream": False,
        "_gitv_sandbox": True,
        "_gitv_sandbox_id": sandbox_id,
        "_gitv_sandbox_chat_id": sandbox["sandbox_chat_id"],
    }

    before = await latest_exchange_id(user_id, sandbox["sandbox_chat_id"])

    from app.services.debug_replay import _synthetic_request
    from app.services.proxy import forward_request

    response = await forward_request(_synthetic_request(body), target=target)
    status_code = getattr(response, "status_code", 500)

    after = await latest_exchange_id(user_id, sandbox["sandbox_chat_id"])

    async with async_session() as db:
        row = await db.get(DebugSandbox, sandbox_id)
        if row is not None:
            row.run_count += 1
            row.last_run_at = datetime.now(UTC)
            await db.commit()

    if after and after != before:
        return after

    if status_code != 200:
        raise SandboxError(
            f"The sandbox request failed upstream (HTTP {status_code}) and no run was recorded."
        )
    raise SandboxError(
        "The sandbox ran but no debug run was recorded. "
        "Check that debug mode is still enabled in Settings."
    )


async def reset_sandbox(user_id: str, sandbox_id: str) -> dict[str, Any]:
    """Discard the sandbox's accumulated state and re-fork from the source run.

    Its id and name survive, so anything pointing at it still resolves.
    """
    sandbox = await _load(user_id, sandbox_id)
    if sandbox is None:
        raise SandboxError("Sandbox not found")

    async with async_session() as db:
        await _purge_state(db, user_id, sandbox["sandbox_chat_id"])
        copied = await _fork_conversation_state(
            db, user_id, sandbox["source_chat_id"], sandbox["sandbox_chat_id"]
        )
        row = await db.get(DebugSandbox, sandbox_id)
        if row is not None:
            row.run_count = 0
            row.last_run_at = None
        await db.commit()

    logger.info("Sandbox %s reset (%d state row(s) restored)", sandbox_id[:8], copied)
    return await get_sandbox(user_id, sandbox_id)


async def delete_sandbox(user_id: str, sandbox_id: str) -> bool:
    """Delete a sandbox and everything its runs accumulated."""
    sandbox = await _load(user_id, sandbox_id)
    if sandbox is None:
        return False

    async with async_session() as db:
        await _purge_state(db, user_id, sandbox["sandbox_chat_id"])
        await db.execute(
            delete(DebugSandbox).where(
                DebugSandbox.id == sandbox_id, DebugSandbox.user_id == user_id
            )
        )
        await db.commit()
    return True


async def _purge_state(db, user_id: str, sandbox_chat_id: str) -> None:
    """Remove state belonging to a sandbox conversation.

    Guarded on the sandbox prefix: these deletes are keyed by conversation id,
    and a blank or malformed id would otherwise reach real conversations.
    """
    if not sandbox_chat_id or not sandbox_chat_id.startswith(SANDBOX_CHAT_PREFIX):
        logger.warning("Refusing to purge non-sandbox conversation id %r", sandbox_chat_id)
        return

    await db.execute(delete(Memory).where(
        Memory.user_id == user_id, Memory.conversation_id == sandbox_chat_id))
    await db.execute(delete(ChatData).where(
        ChatData.user_id == user_id, ChatData.conversation_id == sandbox_chat_id))
    await db.execute(delete(ConversationSummary).where(
        ConversationSummary.user_id == user_id,
        ConversationSummary.internal_chat_id == sandbox_chat_id))


async def list_sandboxes(user_id: str) -> list[dict[str, Any]]:
    async with async_session() as db:
        result = await db.execute(
            select(DebugSandbox)
            .where(DebugSandbox.user_id == user_id)
            .order_by(DebugSandbox.created_at.desc())
        )
        return [_serialize(s) for s in result.scalars().all()]


async def get_sandbox(user_id: str, sandbox_id: str) -> dict[str, Any] | None:
    return await _load(user_id, sandbox_id)


async def rename_sandbox(user_id: str, sandbox_id: str, name: str) -> bool:
    async with async_session() as db:
        result = await db.execute(
            select(DebugSandbox).where(
                DebugSandbox.id == sandbox_id, DebugSandbox.user_id == user_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.name = name[:128]
        await db.commit()
        return True


async def update_messages(user_id: str, sandbox_id: str, messages: list[dict]) -> bool:
    """Edit the request a sandbox sends.

    Editing the prompt and re-running is the other half of iterating: a user
    testing a cantrip usually wants to vary the input as well as the config.
    """
    async with async_session() as db:
        result = await db.execute(
            select(DebugSandbox).where(
                DebugSandbox.id == sandbox_id, DebugSandbox.user_id == user_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        row.messages = json.dumps(messages, default=str)
        await db.commit()
        return True


async def _load(user_id: str, sandbox_id: str) -> dict[str, Any] | None:
    async with async_session() as db:
        result = await db.execute(
            select(DebugSandbox).where(
                DebugSandbox.id == sandbox_id, DebugSandbox.user_id == user_id
            )
        )
        row = result.scalar_one_or_none()
        return _serialize(row) if row else None


def _serialize(s: DebugSandbox) -> dict[str, Any]:
    try:
        messages = json.loads(s.messages) if s.messages else []
    except (ValueError, TypeError):
        messages = []
    return {
        "id": s.id,
        "name": s.name,
        "source_exchange_id": s.source_exchange_id,
        "source_chat_id": s.source_chat_id,
        "sandbox_chat_id": s.sandbox_chat_id,
        "messages": s.messages,
        "message_list": messages,
        "message_count": len(messages),
        "model": s.model,
        "run_count": s.run_count,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else "",
        "created_at": s.created_at.isoformat() if s.created_at else "",
    }


def _original_messages(exchange: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages as the client originally sent them."""
    from app.services.debug_replay import _original_messages as extract

    return extract(exchange)
