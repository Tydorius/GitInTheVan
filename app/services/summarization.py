from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.memory_rule import MemoryRule
from app.models.user_settings import UserSettings
from app.services.llm_params import apply_to_body, parse_params, resolve

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


def summarizer_parameters(
    user_settings: UserSettings | None, endpoint: Endpoint | None, model: str
) -> dict[str, Any]:
    """Resolve the conversation summarizer's parameters.

    The summarizer has no rule-level scope of its own, so the endpoint and its
    curated model entry are the closest layers to the call.
    """
    from app.services.routing import endpoint_parameters, model_parameter_map

    layers = [
        (
            "user settings (summarization)",
            parse_params(user_settings.summarization_parameters_json, "user settings")
            if user_settings else [],
        ),
    ]
    if endpoint is not None:
        layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
        layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
    return dict(resolve(layers).values)


async def summarize_messages(
    messages: list[dict[str, Any]],
    endpoint: Endpoint,
    model: str,
    prompt: str,
    existing_summary: str = "",
    timeout: int = 120,
    configured: dict[str, Any] | None = None,
    body_json: dict[str, Any] | None = None,
) -> str:
    """Summarize `messages` on the summarization endpoint.

    Goes through `proxy._do_forward`, so a summarizer on a provider endpoint
    routes through LiteLLM exactly as the driver does.

    Every failure returns `existing_summary` and lets the request through
    unsummarized -- summarization must never block a reply. `body_json` carries
    the debug trace so that degradation is recorded rather than silent.
    """
    from app.services.debug import debug_capture
    from app.services.debug_metrics import PURPOSE_SUMMARIZER, record_llm_call
    from app.services.proxy import _build_upstream_url, _do_forward

    def _degraded(detail: str) -> str:
        logger.warning("Summarization did not run: %s", detail)
        if body_json is not None:
            debug_capture(
                body_json, "summarization_error", "Summarization",
                detail=f"Did not run: {detail}. Conversation forwarded unsummarized.",
                metadata={"error": detail, "endpoint": endpoint.name},
            )
        return existing_summary

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

    url = _build_upstream_url(
        endpoint.base_url, "/v1/chat/completions", endpoint.api_base_path or ""
    )
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }
    # `temperature` is a default here, not a fixed value -- a configured
    # parameter at any layer overrides it.
    body: dict[str, Any] = {
        "model": model or "gpt-4",
        "messages": [
            {"role": "system", "content": prompt or DEFAULT_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    body = apply_to_body(body, configured or {})

    started = time.monotonic()
    try:
        data, status_code = await _do_forward(
            "POST", url, headers, json.dumps(body).encode(),
            httpx.Timeout(timeout, connect=15.0),
            provider=endpoint.provider or "",
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
            configured=configured,
        )
    except Exception as exc:
        logger.exception("Summarization LLM request failed")
        record_llm_call(
            body_json or {},
            purpose=PURPOSE_SUMMARIZER,
            latency_ms=(time.monotonic() - started) * 1000.0,
            status_code=0,
            response_data=None,
            messages=body.get("messages"),
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.name,
            provider=endpoint.provider or "",
            model_requested=body.get("model", ""),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        return _degraded(f"{type(exc).__name__}")

    record_llm_call(
        body_json or {},
        purpose=PURPOSE_SUMMARIZER,
        latency_ms=(time.monotonic() - started) * 1000.0,
        status_code=status_code,
        response_data=data,
        messages=body.get("messages"),
        endpoint_id=endpoint.id,
        endpoint_name=endpoint.name,
        provider=endpoint.provider or "",
        model_requested=body.get("model", ""),
    )

    if status_code != 200:
        return _degraded(f"the endpoint returned {status_code}")

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    if not content:
        return _degraded("the endpoint returned empty content")

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


async def resolve_memory_rule(
    db: AsyncSession, user_id: str, tags: list | None
) -> MemoryRule | None:
    """Find the first matching memory rule for the given tags.

    Rules are evaluated in execution_order. Tagged rules are checked first,
    then untagged (default) rules. Returns None if no rules match.
    """
    # Candidates, not decisions: a tagged inactive rule must reach the tag check
    # for its tag to be able to select it.
    result = await db.execute(
        select(MemoryRule)
        .where(
            MemoryRule.user_id == user_id,
            or_(MemoryRule.is_active.is_(True), MemoryRule.tag != ""),
        )
        .order_by(MemoryRule.execution_order, MemoryRule.created_at)
    )
    all_rules = list(result.scalars().all())

    if not all_rules:
        return None

    from app.services.tagging import tag_matches_resource

    tagged_rule = None
    default_rule = None

    # Match-only for the tagged branch, as for maps: one rule wins, so the Active
    # flag cannot also mean "apply to everything" without the first active tagged
    # rule hijacking every request that carries any tag. The untagged active rule
    # is the blanket source here.
    for rule in all_rules:
        if rule.tag:
            if tagged_rule is None and tag_matches_resource(
                rule.tag, "memory-rule", False, rule.user_id, user_id, tags or []
            ):
                tagged_rule = rule
                break
        elif rule.is_active and not default_rule:
            default_rule = rule

    return tagged_rule or default_rule


async def maybe_summarize(
    body_json: dict[str, Any], user_id: str, internal_chat_id: str,
    tags: list | None = None,
) -> dict[str, Any]:
    if not internal_chat_id:
        return body_json

    messages = body_json.get("messages", [])
    if not messages:
        return body_json

    try:
        async with async_session() as db:
            user_settings, endpoint = await load_summarization_config(db, user_id)
            memory_rule = await resolve_memory_rule(db, user_id, tags)

        if not user_settings or not endpoint:
            return body_json

        if memory_rule and not memory_rule.summarization_enabled:
            logger.info("Memory rule '%s' disabled summarization for this request", memory_rule.name)
            return body_json

        threshold = max(1, user_settings.summarization_token_threshold)
        keep_recent = max(0, user_settings.summarization_keep_recent)
        prompt = user_settings.summarization_prompt or DEFAULT_PROMPT

        if memory_rule:
            if memory_rule.token_threshold > 0:
                threshold = max(1, memory_rule.token_threshold)
            if memory_rule.keep_recent > 0:
                keep_recent = max(0, memory_rule.keep_recent)
            if memory_rule.prompt:
                prompt = memory_rule.prompt
            logger.info("Memory rule '%s' applied to summarization", memory_rule.name)

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
            summary_text = await summarize_messages(
                to_compress,
                endpoint,
                user_settings.summarization_model,
                prompt,
                existing_summary=existing.summary if existing else "",
                configured=summarizer_parameters(
                    user_settings, endpoint, user_settings.summarization_model
                ),
                body_json=body_json,
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
