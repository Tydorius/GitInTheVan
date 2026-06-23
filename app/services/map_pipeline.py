from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.endpoint import Endpoint
from app.models.map import Map, MapStage

logger = logging.getLogger(__name__)


async def resolve_map(user_id: str, tags: list | None) -> Map | None:
    """Find the first matching map for the given tags.

    Tagged maps are checked first, then untagged default maps.
    Returns None if no maps match.
    """
    async with async_session() as db:
        result = await db.execute(
            select(Map)
            .where(Map.user_id == user_id, Map.is_active.is_(True))
            .options(selectinload(Map.stages).selectinload(MapStage.resources))
            .order_by(Map.updated_at)
        )
        all_maps = list(result.scalars().all())

        if not all_maps:
            return None

        from app.services.tagging import should_activate_resource

        tagged_map = None
        for m in all_maps:
            if m.tag and tags:
                if should_activate_resource(
                    m.tag, "map", m.is_active, m.is_public, m.user_id, user_id, tags
                ):
                    tagged_map = m
                    break

        if tagged_map:
            return tagged_map

        return None


async def _resolve_stage_endpoint(
    db, stage: MapStage, user_id: str
) -> tuple[Endpoint | None, str]:
    """Resolve the endpoint and model for a map stage."""
    endpoint = None
    if stage.endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == stage.endpoint_id,
                Endpoint.user_id == user_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        from app.models.user_settings import UserSettings
        us_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        us = us_result.scalar_one_or_none()
        if us and us.default_endpoint_id:
            ep_result = await db.execute(
                select(Endpoint).where(
                    Endpoint.id == us.default_endpoint_id,
                    Endpoint.user_id == user_id,
                    Endpoint.enabled.is_(True),
                )
            )
            endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        ep_result = await db.execute(
            select(Endpoint)
            .where(Endpoint.user_id == user_id, Endpoint.enabled.is_(True))
            .order_by(Endpoint.created_at)
            .limit(1)
        )
        endpoint = ep_result.scalar_one_or_none()

    model = stage.model_override or ""
    return endpoint, model


async def _resolve_verification_endpoint(
    db, stage: MapStage, user_id: str
) -> tuple[Endpoint | None, str]:
    """Resolve the verification endpoint for a map stage."""
    endpoint = None
    if stage.verification_endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == stage.verification_endpoint_id,
                Endpoint.user_id == user_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        from app.models.user_settings import UserSettings
        us_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        us = us_result.scalar_one_or_none()
        if us and us.verification_endpoint_id:
            ep_result = await db.execute(
                select(Endpoint).where(
                    Endpoint.id == us.verification_endpoint_id,
                    Endpoint.user_id == user_id,
                    Endpoint.enabled.is_(True),
                )
            )
            endpoint = ep_result.scalar_one_or_none()

    model = stage.verification_model or ""
    if not model:
        from app.models.user_settings import UserSettings
        us_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        us = us_result.scalar_one_or_none()
        if us:
            model = us.verification_model or ""

    return endpoint, model


async def _inject_stage_instructions(
    body_json: dict[str, Any], stage: MapStage, map_obj: Map
) -> dict[str, Any]:
    """Inject stage and global system instructions into the messages."""
    messages = body_json.get("messages", [])

    instructions_parts = []
    if map_obj.global_llm_instructions:
        instructions_parts.append(map_obj.global_llm_instructions)
    if stage.system_instructions:
        instructions_parts.append(stage.system_instructions)

    if instructions_parts:
        combined = "\n\n".join(instructions_parts)
        block = f"[STAGE INSTRUCTIONS]\n{combined}\n[/STAGE INSTRUCTIONS]"

        system_idx = None
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                system_idx = i
                break

        if system_idx is not None:
            messages = list(messages)
            messages.insert(system_idx + 1, {"role": "system", "content": block})
        else:
            messages.insert(0, {"role": "system", "content": block})

        body_json["messages"] = messages

    return body_json


async def _inject_stage_lorebooks(
    body_json: dict[str, Any], stage: MapStage, user_id: str
) -> dict[str, Any]:
    """Inject lorebooks attached to this stage."""

    stage_lorebook_ids = [
        r.resource_id for r in stage.resources
        if r.resource_type == "lorebook" and r.position == "pre_driver"
    ]

    if not stage_lorebook_ids:
        return body_json

    async with async_session() as db:
        from app.models.lorebook import Lorebook
        result = await db.execute(
            select(Lorebook)
            .where(Lorebook.id.in_(stage_lorebook_ids), Lorebook.user_id == user_id)
            .options(selectinload(Lorebook.entries))
        )
        lorebooks = result.scalars().all()

        if not lorebooks:
            return body_json

        from app.services.lorebook import inject_entries, match_entries
        all_entries: list[dict] = []
        for lb in lorebooks:
            for entry in lb.entries:
                all_entries.append({
                    "name": entry.name,
                    "keys": entry.keys,
                    "secondary_keys": entry.secondary_keys,
                    "content": entry.content,
                    "position": entry.position,
                    "insertion_order": entry.insertion_order,
                    "is_constant": entry.is_constant,
                    "is_selective": entry.is_selective,
                    "is_disabled": entry.is_disabled,
                    "character_limit": entry.character_limit,
                })

        if all_entries:
            messages = body_json.get("messages", [])
            matched = match_entries(messages, all_entries)
            if matched:
                body_json["messages"] = inject_entries(messages, matched)
                logger.info(
                    "Map stage '%s': %d lorebook entries injected",
                    stage.name, len(matched),
                )

    return body_json


async def _build_stage_verification_body(
    content: str, stage: MapStage
) -> list[dict[str, str]]:
    """Build verification messages for a stage's response."""
    messages = [
        {
            "role": "system",
            "content": (
                "You are a response verification AI. Evaluate whether the following "
                "response meets the requirements. Respond with JSON: "
                '{"violation": false/true, "reason": "explanation"}'
            ),
        },
    ]

    if stage.verification_instructions:
        messages.append({
            "role": "system",
            "content": f"Verification rule:\n{stage.verification_instructions}",
        })

    messages.append({
        "role": "user",
        "content": f"Response to evaluate:\n\n{content[:4000]}",
    })

    return messages


async def _verify_stage(
    content: str,
    stage: MapStage,
    user_id: str,
) -> tuple[bool, str]:
    """Run verification on a stage's response.

    Returns (approved, reason).
    """
    async with async_session() as db:
        v_endpoint, v_model = await _resolve_verification_endpoint(db, stage, user_id)

    if not v_endpoint:
        logger.warning("Map stage '%s': verification enabled but no endpoint", stage.name)
        return True, ""

    messages = await _build_stage_verification_body(content, stage)

    headers = {
        "Authorization": f"Bearer {v_endpoint.api_key}",
        "Content-Type": "application/json",
    }
    api_base_path = v_endpoint.api_base_path or ""
    if api_base_path.endswith("/chat/completions"):
        url = f"{v_endpoint.base_url}{api_base_path}"
    else:
        path_prefix = api_base_path or "/v1"
        url = f"{v_endpoint.base_url}{path_prefix}/chat/completions"

    body = {
        "model": v_model or "gpt-4",
        "messages": messages,
        "max_tokens": 200,
        "temperature": 0.1,
        "stream": False,
    }

    max_retries = stage.verification_max_retries
    from app.services.admin import get_caps
    caps = await get_caps()
    max_retries = min(max_retries, caps["max_verification_retries"])

    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15.0)) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "Map stage '%s' verification: endpoint returned %d",
                        stage.name, resp.status_code,
                    )
                    return True, f"Verification endpoint error ({resp.status_code})"

                data = resp.json()
                llm_content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )

                violation = False
                reason = ""
                try:
                    parsed = json.loads(llm_content)
                    violation = parsed.get("violation", False)
                    reason = parsed.get("reason", "")
                except (json.JSONDecodeError, TypeError):
                    if "violation" in llm_content.lower():
                        violation = True
                        reason = llm_content[:200]

                if not violation:
                    logger.info("Map stage '%s' verification: approved (attempt %d)", stage.name, attempt + 1)
                    return True, ""

                if attempt >= max_retries:
                    logger.warning(
                        "Map stage '%s' verification: failed after %d retries: %s",
                        stage.name, max_retries, reason,
                    )
                    return False, reason

                logger.info(
                    "Map stage '%s' verification: violation on attempt %d, retrying: %s",
                    stage.name, attempt + 1, reason,
                )

            except Exception:
                logger.exception("Map stage '%s' verification failed", stage.name)
                return True, ""

    return True, ""


async def _forward_stage_llm(
    body_json: dict[str, Any],
    endpoint: Endpoint,
    model: str,
    timeout: httpx.Timeout,
) -> tuple[dict[str, Any], int]:
    """Forward a request to the stage's LLM endpoint."""
    forward_body = {k: v for k, v in body_json.items() if not k.startswith("_gitv_")}
    forward_body["stream"] = False

    if model:
        forward_body["model"] = model

    api_base_path = endpoint.api_base_path or ""
    if api_base_path.endswith("/chat/completions"):
        url = f"{endpoint.base_url}{api_base_path}"
    else:
        path_prefix = api_base_path or "/v1"
        url = f"{endpoint.base_url}{path_prefix}/chat/completions"

    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }

    body_bytes = json.dumps(forward_body).encode()

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, content=body_bytes, headers=headers)

    try:
        response_data = resp.json()
    except Exception:
        response_data = {"error": {"message": resp.text[:500]}}

    return response_data, resp.status_code


async def run_map_pipeline(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    map_obj: Map,
    timeout: httpx.Timeout,
) -> dict[str, Any]:
    """Execute the multi-stage map pipeline.

    Returns the final response_data from the last stage.
    """
    from app.services.admin import get_caps
    caps = await get_caps()
    max_stages = min(len(map_obj.stages), caps["max_map_stages"])

    if max_stages == 0:
        logger.warning("Map '%s' has no stages, returning empty response", map_obj.name)
        return {
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "error": "Map has no stages",
        }

    stages = sorted(map_obj.stages, key=lambda s: s.stage_order)[:max_stages]
    tags = body_json.get("_gitv_tags", [])

    logger.info(
        "Map pipeline '%s': %d stages (cap=%d)",
        map_obj.name, len(stages), caps["max_map_stages"],
    )

    sticky_context: list[dict[str, str]] = []

    for stage_idx, stage in enumerate(stages):
        is_last_stage = stage_idx == len(stages) - 1
        logger.info("Map stage %d/%d: '%s'", stage_idx + 1, len(stages), stage.name)

        async with async_session() as db:
            endpoint, model = await _resolve_stage_endpoint(db, stage, user_id)

        if not endpoint:
            logger.error("Map stage '%s': no endpoint available", stage.name)
            return {
                "choices": [{"message": {"role": "assistant", "content": ""}}],
                "error": f"No endpoint configured for stage '{stage.name}'",
            }

        body_json = await _inject_stage_instructions(body_json, stage, map_obj)
        body_json = await _inject_stage_lorebooks(body_json, stage, user_id)

        for ctx in sticky_context:
            messages = body_json.get("messages", [])
            messages.append(ctx)
            body_json["messages"] = messages

        from app.services.cantrip import process_cantrips

        body_json = await process_cantrips(
            body_json, user_id, request_headers,
            tags=tags,
            internal_chat_id=body_json.get("_gitv_chat_id", ""),
        )

        if stage.driver_callable_turns > 0:
            body_json["_gitv_driver_callable"] = True
            body_json["_gitv_driver_callable_turns"] = min(
                stage.driver_callable_turns, caps["max_driver_callable_turns"]
            )

        response_data, status_code = await _forward_stage_llm(body_json, endpoint, model, timeout)

        if status_code != 200:
            logger.warning("Map stage '%s': LLM returned %d", stage.name, status_code)
            return response_data

        content = response_data.get("choices", [{}])[0].get("message", {}).get("content", "")

        if stage.verification_enabled:
            approved, reason = await _verify_stage(content, stage, user_id)
            if not approved:
                logger.warning(
                    "Map stage '%s': verification failed after retries: %s. Continuing to next stage.",
                    stage.name, reason,
                )

        if not is_last_stage:
            if stage.output_mode == "persist":
                sticky_context.append({"role": "assistant", "content": content})
            elif stage.output_mode == "sanitize":
                sticky_context.append({
                    "role": "system",
                    "content": f"[STAGE {stage_idx + 1} OUTPUT: {stage.name}]\n{content}\n[/STAGE OUTPUT]",
                })
            elif stage.output_mode == "discard":
                pass

            body_json = _strip_stage_injections(body_json, stage)

        logger.info("Map stage '%s' completed: %d chars output", stage.name, len(content))

    return response_data


def _strip_stage_injections(
    body_json: dict[str, Any], stage: MapStage
) -> dict[str, Any]:
    """Remove non-sticky stage injections from messages before the next stage."""
    messages = body_json.get("messages", [])

    filtered = [
        msg for msg in messages
        if not (
            msg.get("role") == "system"
            and isinstance(msg.get("content"), str)
            and "[STAGE INSTRUCTIONS]" in msg.get("content", "")
        )
    ]

    body_json["messages"] = filtered
    body_json.pop("_gitv_map_stage_cantrip_ids", None)

    return body_json
