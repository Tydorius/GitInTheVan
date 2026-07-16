from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.lorebook import Lorebook
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += estimate_tokens(str(part["text"]))
    return total


async def load_budget_config(user_id: str) -> tuple[float, int]:
    """Load context budget settings for a user.

    Returns (budget_percent, context_window_override).
    """
    async with async_session() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        us = result.scalar_one_or_none()
        if not us:
            return 10.0, 0
        return us.context_budget_percent, us.context_window_override


def estimate_context_window(
    body_json: dict[str, Any], context_window_override: int
) -> int:
    """Estimate the model's context window size.

    If the user has set an override, use that. Otherwise, try to infer
    from the model name. Falls back to 8192 (a conservative default).
    """
    if context_window_override > 0:
        return context_window_override

    model = body_json.get("model", "")
    model_lower = model.lower()

    if "gpt-4o" in model_lower or "gpt-4.1" in model_lower:
        return 128000
    if "gpt-4" in model_lower and "mini" not in model_lower:
        return 8192
    if "gpt-4-mini" in model_lower or "gpt-4o-mini" in model_lower:
        return 128000
    if "gpt-3.5" in model_lower:
        return 16385
    if "claude-3-5" in model_lower or "claude-3.7" in model_lower or "claude-sonnet" in model_lower:
        return 200000
    if "claude-3" in model_lower or "claude-opus" in model_lower or "claude-haiku" in model_lower:
        return 200000
    if "gemini-2" in model_lower:
        return 1000000
    if "gemini-1.5" in model_lower:
        return 1000000
    if "llama-3.3" in model_lower or "llama-3.2" in model_lower:
        return 128000
    if "llama-3.1" in model_lower or "llama-3" in model_lower:
        return 8192
    if "mistral" in model_lower or "mixtral" in model_lower:
        return 32768
    if "qwen" in model_lower:
        return 32768

    return 8192


def compute_budget(
    body_json: dict[str, Any],
    user_id: str,
    budget_percent: float,
    context_window: int,
) -> dict[str, Any]:
    """Compute the injection budget for the current request.

    Returns a budget info dict stored on body_json for later reference.
    """
    messages = body_json.get("messages", [])
    current_tokens = estimate_messages_tokens(messages)
    max_tokens = body_json.get("max_tokens", 0)
    completion_reserve = max_tokens if max_tokens and max_tokens > 0 else 0

    available = context_window - completion_reserve - current_tokens
    if available < 0:
        available = 0

    injection_budget = int(context_window * budget_percent / 100.0)

    budget_info: dict[str, Any] = {
        "context_window": context_window,
        "current_tokens": current_tokens,
        "completion_reserve": completion_reserve,
        "available": available,
        "injection_budget": injection_budget,
    }

    body_json["_gitv_budget"] = budget_info
    return budget_info


async def load_weighted_resources(
    user_id: str, tags: list | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Load active cantrips, lorebooks, and skills with their budget weights.

    Returns (cantrips, lorebooks, skills) where each entry is a dict with
    id, name, weight, and position info.
    """
    from app.models.skill import Skill
    from app.services.tagging import should_activate_resource

    async with async_session() as db:
        lb_result = await db.execute(
            select(Lorebook).where(Lorebook.user_id == user_id)
        )
        lorebooks = lb_result.scalars().all()

        active_lorebooks = []
        for lb in lorebooks:
            if lb.tag and tags:
                if should_activate_resource(
                    lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, user_id, tags
                ):
                    active_lorebooks.append(lb)
            elif lb.is_active and lb.run_pre_driver:
                active_lorebooks.append(lb)

        cantrip_result = await db.execute(
            select(Cantrip).where(
                Cantrip.user_id == user_id,
                Cantrip.is_active.is_(True),
                Cantrip.run_pre_driver.is_(True),
            )
        )
        all_cantrips = cantrip_result.scalars().all()

        active_cantrips = []
        for c in all_cantrips:
            if c.tag and tags:
                if should_activate_resource(
                    c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags
                ):
                    active_cantrips.append(c)
            else:
                active_cantrips.append(c)

        skill_result = await db.execute(
            select(Skill).where(Skill.user_id == user_id)
        )
        all_skills = skill_result.scalars().all()

        lorebook_info = [
            {"id": lb.id, "name": lb.name, "weight": lb.budget_weight}
            for lb in active_lorebooks
        ]
        cantrip_info = [
            {"id": c.id, "name": c.name, "weight": c.budget_weight}
            for c in active_cantrips
        ]
        skill_info = [
            {"id": s.id, "name": s.name, "weight": s.budget_weight}
            for s in all_skills
        ]

    return cantrip_info, lorebook_info, skill_info


def allocate_budget(
    injection_budget: int,
    cantrip_info: list[dict[str, Any]],
    lorebook_info: list[dict[str, Any]],
    skill_info: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Allocate the injection budget across active resources by weight.

    Returns a dict with per-resource token shares and detail levels.
    """
    all_resources = cantrip_info + lorebook_info + (skill_info or [])
    total_weight = sum(r["weight"] for r in all_resources)

    if total_weight <= 0:
        total_weight = 1.0

    allocations: dict[str, dict[str, Any]] = {}
    remaining = injection_budget

    for resource in all_resources:
        share = int(injection_budget * resource["weight"] / total_weight)
        remaining -= share

        if share >= 4000:
            detail_level = "full"
        elif share >= 1500:
            detail_level = "summary"
        else:
            detail_level = "bullets"

        key = f"{resource['id']}"
        allocations[key] = {
            "name": resource["name"],
            "share": share,
            "weight": resource["weight"],
            "detail_level": detail_level,
        }

    if remaining != 0 and allocations:
        first_key = next(iter(allocations))
        allocations[first_key]["share"] += remaining

    return {
        "total": injection_budget,
        "remaining": injection_budget,
        "allocations": allocations,
    }


def build_cantrip_budget_context(
    budget_allocations: dict[str, Any],
    cantrip_id: str,
    cantrip_weight: float,
) -> dict[str, Any]:
    """Build the context.budget object for a specific cantrip.

    Updates remaining budget as cantrips consume their allocation.
    """
    alloc = budget_allocations.get("allocations", {}).get(cantrip_id, {})
    share = alloc.get("share", 0)
    detail_level = alloc.get("detail_level", "full")
    total = budget_allocations.get("total", 0)
    remaining = budget_allocations.get("remaining", total)

    if remaining <= 0:
        detail_level = "bullets"
        share = 0

    budget_allocations["remaining"] = max(0, remaining - share)

    return {
        "total": total,
        "remaining": remaining,
        "weight": cantrip_weight,
        "share": share,
        "detail_level": detail_level,
    }


async def prepare_budget(
    body_json: dict[str, Any], user_id: str, tags: list | None = None
) -> dict[str, Any] | None:
    """Full budget preparation: load settings, compute, allocate.

    Returns the budget allocations dict (also stored on body_json),
    or None if budgeting is disabled (percent <= 0).
    """
    budget_percent, window_override = await load_budget_config(user_id)
    if budget_percent <= 0:
        return None

    context_window = estimate_context_window(body_json, window_override)
    compute_budget(body_json, user_id, budget_percent, context_window)

    budget_info = body_json.get("_gitv_budget", {})
    injection_budget = budget_info.get("injection_budget", 0)
    if injection_budget <= 0:
        return None

    cantrip_info, lorebook_info, skill_info = await load_weighted_resources(user_id, tags)
    allocations = allocate_budget(injection_budget, cantrip_info, lorebook_info, skill_info)

    body_json["_gitv_budget_allocations"] = allocations

    logger.info(
        "Context budget: window=%d, injection=%d tokens, "
        "%d cantrips + %d lorebooks + %d skills allocated",
        context_window, injection_budget,
        len(cantrip_info), len(lorebook_info), len(skill_info),
    )

    return allocations
