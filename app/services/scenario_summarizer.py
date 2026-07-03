import json
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.scenario_rule import ScenarioRule
from app.services.budget import estimate_tokens

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Summarize the following character definition and scenario information while preserving all essential details. Keep:\n"
    "- Character name, personality, appearance, and core traits\n"
    "- Key relationships and power dynamics\n"
    "- Setting/world details relevant to the scene\n"
    "- Important rules, restrictions, or behavioral guidelines\n"
    "- Any specific instructions about writing style or format\n\n"
    "Compress redundant descriptions and merge overlapping information. Be concise but complete.\n"
    "Output only the summarized text, no commentary."
)


def _find_first_system_index(messages: list[dict]) -> int | None:
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            return i
    return None


async def _load_active_rules(user_id: str, position: str) -> list[ScenarioRule]:
    async with async_session() as db:
        result = await db.execute(
            select(ScenarioRule)
            .where(
                ScenarioRule.user_id == user_id,
                ScenarioRule.is_active.is_(True),
                ScenarioRule.fire_position == position,
            )
            .order_by(ScenarioRule.token_threshold.desc())
        )
        return list(result.scalars().all())


async def maybe_summarize_scenario(
    body_json: dict[str, Any],
    user_id: str,
    position: str,
) -> dict[str, Any]:
    """Check if the system message should be summarized at the given position.

    Position 'pre' runs after memory injection, before lorebooks/cantrips.
    Position 'post' runs after cantrips/skills, before writing samples.
    """
    messages = body_json.get("messages", [])
    system_idx = _find_first_system_index(messages)

    if system_idx is None:
        return body_json

    system_content = messages[system_idx].get("content", "")
    if not isinstance(system_content, str) or not system_content.strip():
        return body_json

    system_tokens = estimate_tokens(system_content)

    rules = await _load_active_rules(user_id, position)
    if not rules:
        return body_json

    triggered_rule: ScenarioRule | None = None
    for rule in rules:
        if system_tokens >= rule.token_threshold:
            triggered_rule = rule
            break

    if triggered_rule is None:
        return body_json

    prompt = triggered_rule.prompt.strip() if triggered_rule.prompt.strip() else DEFAULT_PROMPT

    logger.info(
        "Scenario summarization triggered (%s): %d tokens >= %d threshold (rule: %s)",
        position, system_tokens, triggered_rule.token_threshold, triggered_rule.name,
    )

    summary = await _call_summarization_llm(
        system_content, prompt, triggered_rule, user_id
    )

    if summary and estimate_tokens(summary) < system_tokens:
        messages[system_idx]["content"] = summary
        logger.info(
            "Scenario summarized (%s): %d → %d tokens (rule: %s)",
            position, system_tokens, estimate_tokens(summary), triggered_rule.name,
        )
    else:
        logger.warning(
            "Scenario summarization (%s) did not reduce size, keeping original (%d tokens)",
            position, system_tokens,
        )

    return body_json


async def _call_summarization_llm(
    content: str,
    prompt: str,
    rule: ScenarioRule,
    user_id: str,
) -> str | None:
    """Call a navigator LLM to summarize the scenario content."""
    from app.services.proxy import _build_upstream_url, _do_forward, _do_forward_litellm

    async with async_session() as db:
        routing = await resolve_routing_by_rule(rule, user_id, db)

    if routing is None:
        logger.warning("Scenario summarization: no endpoint configured for rule %s", rule.name)
        return None

    base_url, api_key, provider, model = routing

    if not model:
        logger.warning("Scenario summarization: no model configured for rule %s", rule.name)
        return None

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": content[:32000]},
    ]

    test_body = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.3,
        "stream": False,
    }

    body_bytes = json.dumps(test_body).encode()
    timeout = httpx.Timeout(60.0, connect=15.0)

    try:
        if provider:
            response_data, status_code = await _do_forward_litellm(
                body_bytes, provider, base_url, api_key, timeout
            )
        else:
            api_base_path = ""
            upstream_url = _build_upstream_url(base_url, "/v1/chat/completions", api_base_path)
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            response_data, status_code = await _do_forward(
                "POST", upstream_url, headers, body_bytes, timeout
            )

        if status_code != 200:
            logger.warning(
                "Scenario summarization LLM returned %d: %.200s",
                status_code, str(response_data)[:200],
            )
            return None

        choices = response_data.get("choices", [])
        if not choices:
            return None

        summarized = choices[0].get("message", {}).get("content", "")
        return summarized.strip() if summarized else None

    except Exception as exc:
        logger.warning("Scenario summarization LLM call failed: %s", exc)
        return None


async def resolve_routing_by_rule(
    rule: ScenarioRule, user_id: str, db: AsyncSession
) -> tuple[str, str, str, str] | None:
    """Resolve endpoint/key/provider/model for a scenario rule.

    Returns (base_url, api_key, provider, model) or None.
    """
    from app.models.endpoint import Endpoint
    from app.models.user_settings import UserSettings

    model = rule.model or ""

    endpoint = None
    if rule.endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == rule.endpoint_id,
                Endpoint.user_id == user_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        settings_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        user_settings = settings_result.scalar_one_or_none()
        if user_settings and user_settings.default_endpoint_id:
            ep_result = await db.execute(
                select(Endpoint).where(
                    Endpoint.id == user_settings.default_endpoint_id,
                    Endpoint.user_id == user_id,
                    Endpoint.enabled.is_(True),
                )
            )
            endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        return None

    if not model:
        model = endpoint.default_model or ""

    return (
        endpoint.base_url,
        endpoint.api_key,
        endpoint.provider or "",
        model,
    )
