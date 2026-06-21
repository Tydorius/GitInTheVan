from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 3
MAX_TRANSCRIPT_CHARS = 24000

DEFAULT_PROMPT = (
    "Summarize the following roleplay conversation excerpt. Preserve key facts, "
    "character development, important plot points, established relationships, locations, "
    "items, and any commitments or promises made. Write in concise bullet points. "
    "Do not add new information. Output only the summary."
)

SUMMARY_OPEN_TAG = "[CONVERSATION SUMMARY]"
SUMMARY_CLOSE_TAG = "[/CONVERSATION SUMMARY]"


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


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        total += len(_extract_text(msg.get("content", ""))) // CHARS_PER_TOKEN
        if msg.get("role"):
            total += 1
    return total


def compute_boundary_hash(messages: list[dict[str, Any]]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = _extract_text(msg.get("content", ""))[:2000]
        parts.append(f"{role}|{content}")
    raw = "\n".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


async def load_summarization_config(
    db: AsyncSession, user_id: str
) -> tuple[UserSettings | None, Endpoint | None]:
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    user_settings = settings_result.scalar_one_or_none()
    if not user_settings or not user_settings.summarization_enabled:
        return None, None

    endpoint = None
    if user_settings.summarization_endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == user_settings.summarization_endpoint_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()

    return user_settings, endpoint


async def load_summary(
    db: AsyncSession, user_id: str, internal_chat_id: str
) -> ConversationSummary | None:
    if not internal_chat_id:
        return None
    result = await db.execute(
        select(ConversationSummary).where(
            ConversationSummary.user_id == user_id,
            ConversationSummary.internal_chat_id == internal_chat_id,
        )
    )
    return result.scalar_one_or_none()


async def save_summary(
    db: AsyncSession,
    user_id: str,
    internal_chat_id: str,
    summary: str,
    boundary_hash: str,
    message_count: int,
    token_estimate: int,
) -> ConversationSummary:
    existing = await load_summary(db, user_id, internal_chat_id)
    if existing:
        existing.summary = summary
        existing.boundary_hash = boundary_hash
        existing.message_count = message_count
        existing.token_estimate = token_estimate
    else:
        existing = ConversationSummary(
            user_id=user_id,
            internal_chat_id=internal_chat_id,
            summary=summary,
            boundary_hash=boundary_hash,
            message_count=message_count,
            token_estimate=token_estimate,
        )
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return existing


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    name_labels = {"user": "User", "assistant": "Character", "system": "System"}
    lines = []
    total = 0
    for msg in messages:
        role = msg.get("role", "unknown")
        label = name_labels.get(role, role.capitalize())
        text = _extract_text(msg.get("content", ""))
        if not text:
            continue
        line = f"{label}: {text}"
        if total + len(line) > MAX_TRANSCRIPT_CHARS:
            remaining = MAX_TRANSCRIPT_CHARS - total
            if remaining > 0:
                lines.append(line[:remaining] + " [...truncated]")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _build_endpoint_url(endpoint: Endpoint) -> str:
    api_base_path = endpoint.api_base_path or ""
    if api_base_path.endswith("/chat/completions"):
        return f"{endpoint.base_url}{api_base_path}"
    path_prefix = api_base_path or "/v1"
    return f"{endpoint.base_url}{path_prefix}/chat/completions"


async def summarize_messages(
    messages: list[dict[str, Any]],
    endpoint: Endpoint,
    model: str,
    prompt: str,
    existing_summary: str = "",
    timeout: int = 120,
) -> str:
    transcript = _format_transcript(messages)
    if not transcript:
        return existing_summary

    if existing_summary:
        user_content = (
            f"Previous summary:\n{existing_summary}\n\n"
            f"New conversation excerpt to incorporate into the summary:\n{transcript}\n\n"
            f"Produce an updated, consolidated summary."
        )
    else:
        user_content = f"Conversation excerpt to summarize:\n{transcript}"

    url = _build_endpoint_url(endpoint)
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model or "gpt-4",
        "messages": [
            {"role": "system", "content": prompt or DEFAULT_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "stream": False,
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0)
    ) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except Exception:
            logger.exception("Summarization LLM request failed")
            return existing_summary

        if resp.status_code != 200:
            logger.warning(
                "Summarization LLM returned %d: %s",
                resp.status_code,
                resp.text[:200],
            )
            return existing_summary

        try:
            data = resp.json()
        except Exception:
            logger.warning("Summarization LLM returned non-JSON")
            return existing_summary

        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not content:
            logger.warning("Summarization LLM returned empty content")
            return existing_summary

        return content


def build_summary_context_block(summary: str) -> str:
    summary = summary.strip()
    if not summary:
        return ""
    return f"{SUMMARY_OPEN_TAG}\n{summary}\n{SUMMARY_CLOSE_TAG}"


def _split_dialogue(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages) if m.get("role") != "system"]


def _compress_messages(
    messages: list[dict[str, Any]],
    compress_indices: set[int],
    summary_block: str,
) -> list[dict[str, Any]]:
    if not compress_indices:
        return messages

    result: list[dict[str, Any]] = []
    inserted = False
    for i, msg in enumerate(messages):
        if i in compress_indices:
            if not inserted:
                result.append({"role": "system", "content": summary_block})
                inserted = True
            continue
        result.append(msg)

    if not inserted:
        result.insert(0, {"role": "system", "content": summary_block})

    return result


async def maybe_summarize(
    body_json: dict[str, Any], user_id: str, internal_chat_id: str
) -> dict[str, Any]:
    if not internal_chat_id:
        return body_json

    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    try:
        async with async_session() as db:
            user_settings, endpoint = await load_summarization_config(db, user_id)

        if not user_settings or not endpoint:
            return body_json

        threshold = max(1, user_settings.summarization_token_threshold)
        keep_recent = max(0, user_settings.summarization_keep_recent)

        total_tokens = estimate_tokens(messages)
        if total_tokens < threshold:
            return body_json

        dialogue_indices = _split_dialogue(messages)
        if len(dialogue_indices) <= keep_recent:
            return body_json

        keep = keep_recent
        compress_indices_set = set(dialogue_indices[:-keep]) if keep > 0 else set(dialogue_indices)

        to_compress = [messages[i] for i in sorted(compress_indices_set)]
        boundary = compute_boundary_hash(to_compress)

        async with async_session() as db:
            existing = await load_summary(db, user_id, internal_chat_id)

        summary_text = ""
        if existing and existing.boundary_hash == boundary and existing.summary:
            summary_text = existing.summary
            logger.info(
                "Summarization: reuse cached summary for chat %s (%d msgs compressed)",
                internal_chat_id[:12],
                len(to_compress),
            )
        else:
            prompt = user_settings.summarization_prompt or DEFAULT_PROMPT
            summary_text = await summarize_messages(
                to_compress,
                endpoint,
                user_settings.summarization_model,
                prompt,
                existing_summary=existing.summary if existing else "",
            )
            if summary_text:
                async with async_session() as db:
                    await save_summary(
                        db, user_id, internal_chat_id, summary_text,
                        boundary, len(to_compress), total_tokens,
                    )
                logger.info(
                    "Summarization: generated summary for chat %s (%d msgs -> %d chars)",
                    internal_chat_id[:12],
                    len(to_compress),
                    len(summary_text),
                )

        if not summary_text:
            return body_json

        summary_block = build_summary_context_block(summary_text)
        body_json["messages"] = _compress_messages(messages, compress_indices_set, summary_block)
        return body_json
    except Exception:
        logger.exception("Summarization failed, forwarding original request")
        return body_json
