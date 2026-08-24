from __future__ import annotations

import copy
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

        from app.models.user_settings import UserSettings
        us_result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        us = us_result.scalar_one_or_none()
        if us and us.default_map_id:
            default_map = next((m for m in all_maps if m.id == us.default_map_id), None)
            if default_map:
                logger.info("Using default map '%s' for user %s", default_map.name, user_id[:8])
                return default_map

        return None


def stage_bound_cantrip_ids(map_obj: Map) -> set[str]:
    """Cantrip ids attached to any stage of this map.

    A cantrip attached to a stage runs on that stage only, so the global
    pre-stage pass must exclude these. Without the split a cantrip with side
    effects -- dealing a card, moving a balance, incrementing a counter --
    fires once globally and again on every stage of the map.
    """
    return {
        r.resource_id
        for stage in map_obj.stages
        for r in stage.resources
        if r.resource_type == "cantrip"
    }


async def _resolve_stage_endpoint(
    db, stage: MapStage, user_id: str
) -> tuple[list[Endpoint], str]:
    """Resolve endpoint candidates and model for a map stage.

    Returns an ordered list of endpoints (for failover) plus the model. Tag
    resolution takes priority (stage.endpoint_tag), then the specific
    endpoint_id pin, then the user default, then the first enabled endpoint.
    Each resolution level returns as soon as it finds at least one match."""
    from app.services.routing import resolve_endpoints_by_tag

    model = stage.model_override or ""

    # 1. Tag-based resolution (Phase 16): endpoints matching stage.endpoint_tag.
    if stage.endpoint_tag:
        tagged = await resolve_endpoints_by_tag(db, user_id, stage.endpoint_tag)
        if tagged:
            return tagged, model

    # 2. Specific endpoint pin.
    if stage.endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == stage.endpoint_id,
                Endpoint.user_id == user_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()
        if endpoint:
            return [endpoint], model

    # 3. User default endpoint.
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
        if endpoint:
            return [endpoint], model

    # 4. First enabled endpoint (lowest priority, then earliest).
    ep_result = await db.execute(
        select(Endpoint)
        .where(Endpoint.user_id == user_id, Endpoint.enabled.is_(True))
        .order_by(Endpoint.priority, Endpoint.created_at)
        .limit(1)
    )
    endpoint = ep_result.scalar_one_or_none()
    if endpoint:
        return [endpoint], model

    return [], model


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
    body_json: dict[str, Any], resources: list, stage_name: str, user_id: str
) -> dict[str, Any]:
    """Inject lorebooks from this stage's resources plus any carried sticky ones."""

    stage_lorebook_ids = [
        r.resource_id for r in resources
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
                    stage_name, len(matched),
                )

    return body_json


async def _inject_stage_skills(
    body_json: dict[str, Any], resources: list, stage_name: str, user_id: str
) -> dict[str, Any]:
    """Inject skills/samples from this stage's resources plus carried sticky ones."""

    stage_skill_ids = [
        r.resource_id for r in resources
        if r.resource_type in ("skill", "sample") and r.position == "pre_driver"
    ]

    if not stage_skill_ids:
        return body_json

    async with async_session() as db:
        from app.models.skill import Skill
        result = await db.execute(
            select(Skill)
            .where(Skill.id.in_(stage_skill_ids), Skill.user_id == user_id)
        )
        skills = result.scalars().all()

        if not skills:
            return body_json

        from app.services.skills import inject_samples, inject_skills

        skill_contents = [s.content for s in skills if s.type == "skill" and s.content]
        sample_contents = [s.content for s in skills if s.type == "sample" and s.content]

        messages = body_json.get("messages", [])
        if skill_contents:
            messages = inject_skills(messages, skill_contents)
        if sample_contents:
            messages = inject_samples(messages, sample_contents)

        body_json["messages"] = messages
        if skill_contents or sample_contents:
            logger.info(
                "Map stage '%s': %d skills + %d samples injected",
                stage_name, len(skill_contents), len(sample_contents),
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


async def _scan_stage_forbidden(
    content: str, stage: MapStage, stage_idx: int, user_id: str
) -> str:
    """Scan a stage's output for forbidden words.

    Returns a system-message body naming what was found, or "" if clean. The
    findings are handed to *later* stages rather than used as a gate: an early
    stage should write to the prompt without also policing a word list, and a
    dedicated editing stage can act on the findings where a pass/fail verdict
    could only force a blind regeneration.
    """
    from app.services.forbidden_words import scan_response

    try:
        scan_result = await scan_response(content, user_id)
    except Exception:
        logger.exception("Map stage '%s': forbidden words scan failed", stage.name)
        return ""

    if not scan_result.has_matches:
        return ""

    logger.info(
        "Map stage '%s': %d forbidden phrase(s) found, flagging for later stages",
        stage.name, len(scan_result.matches),
    )
    header = f"[FORBIDDEN WORDS FLAGGED IN STAGE {stage_idx + 1} OUTPUT: {stage.name}]"
    return "\n".join([
        header,
        scan_result.summary,
        "Replace each of these phrases with wording that carries the same meaning. "
        "Do not comment on this notice or mention that words were flagged.",
        f"[/FORBIDDEN WORDS FLAGGED IN STAGE {stage_idx + 1} OUTPUT]",
    ])


async def _forward_stage_llm(
    body_json: dict[str, Any],
    endpoints: list[Endpoint],
    model: str,
    timeout: httpx.Timeout,
) -> tuple[dict[str, Any], int]:
    """Forward a request to the stage's LLM endpoint(s) with failover.

    Iterates the candidate list in order. Any failure (status != 200 or
    exception) advances to the next endpoint. Only total exhaustion yields an
    error status."""
    if not endpoints:
        return ({"error": {"message": "No endpoint configured for this stage"}}, 503)

    forward_body = {k: v for k, v in body_json.items() if not k.startswith("_gitv_")}
    forward_body["stream"] = False

    if model:
        forward_body["model"] = model

    for idx, endpoint in enumerate(endpoints):
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

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)

            try:
                response_data = resp.json()
            except Exception:
                response_data = {"error": {"message": resp.text[:500]}}

            if resp.status_code == 200:
                return response_data, 200

            logger.info(
                "Map stage failover: endpoint '%s' returned %d, trying next",
                endpoint.name, resp.status_code,
            )
        except Exception:
            logger.exception(
                "Map stage failover: endpoint '%s' raised an exception, trying next",
                endpoint.name,
            )

    logger.warning("Map stage failover: all %d endpoint(s) exhausted", len(endpoints))
    return ({"error": {"message": "All stage endpoints failed"}}, 503)


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

    # Honours the same <FORBIDDEN:off> command override as the non-map path;
    # scan_response() itself is a no-op when the user setting is disabled.
    forbidden_enabled = (
        body_json.get("_gitv_command_overrides", {}).get("forbidden") is not False
    )

    logger.info(
        "Map pipeline '%s': %d stages (cap=%d)",
        map_obj.name, len(stages), caps["max_map_stages"],
    )

    sticky_context: list[dict[str, str]] = []

    # Every stage builds its request from this pristine snapshot rather than from
    # the previous stage's mutated list. Stage injections are not all removable
    # after the fact -- inject_skills appends into messages[0]["content"] and
    # inject_samples inserts a free-standing system message -- so without a reset
    # each stage inherits every earlier stage's skills, samples and lorebooks, and
    # the sticky context (re-appended in full each pass) compounds on top of that.
    base_messages = copy.deepcopy(body_json.get("messages", []))

    # Resources flagged sticky carry forward into every later stage (README:
    # "Resource Attachments"). Everything else is stage-only.
    sticky_resources: list = []

    for stage_idx, stage in enumerate(stages):
        is_last_stage = stage_idx == len(stages) - 1
        logger.info("Map stage %d/%d: '%s'", stage_idx + 1, len(stages), stage.name)

        body_json["messages"] = copy.deepcopy(base_messages)

        async with async_session() as db:
            endpoints, model = await _resolve_stage_endpoint(db, stage, user_id)

        if not endpoints:
            logger.error("Map stage '%s': no endpoint available", stage.name)
            return {
                "choices": [{"message": {"role": "assistant", "content": ""}}],
                "error": f"No endpoint configured for stage '{stage.name}'",
            }

        # A resource marked sticky on an earlier stage keeps being injected; a
        # stage-only one (the default) applies to its own stage and no other.
        # Deduplicated because a sticky resource re-attached to a later stage
        # would otherwise be injected twice into that stage.
        seen_ids: set[str] = set()
        active_resources = []
        for r in list(sticky_resources) + list(stage.resources):
            if r.resource_id in seen_ids:
                continue
            seen_ids.add(r.resource_id)
            active_resources.append(r)

        body_json = await _inject_stage_instructions(body_json, stage, map_obj)
        body_json = await _inject_stage_lorebooks(
            body_json, active_resources, stage.name, user_id
        )
        body_json = await _inject_stage_skills(
            body_json, active_resources, stage.name, user_id
        )

        for ctx in sticky_context:
            messages = body_json.get("messages", [])
            messages.append(ctx)
            body_json["messages"] = messages

        from app.services.cantrip import process_cantrips

        stage_cantrip_ids = {
            r.resource_id for r in active_resources if r.resource_type == "cantrip"
        }
        if stage_cantrip_ids:
            body_json = await process_cantrips(
                body_json, user_id, request_headers,
                tags=tags,
                internal_chat_id=body_json.get("_gitv_chat_id", ""),
                only_ids=stage_cantrip_ids,
            )

        if stage.driver_callable_turns > 0:
            # NOT IMPLEMENTED under maps. These flags are only read by
            # _run_driver_callable_loop in proxy.py, which the map branch never
            # reaches, and build_tool_notification is never called -- so the
            # stage's model is not told the tools exist and no tool loop runs.
            # Wiring it up means teaching the loop to forward through the
            # stage's failover chain instead of a single url/headers pair.
            # Setting the flags anyway so the intent survives, but warn rather
            # than let a configured stage quietly do nothing.
            logger.warning(
                "Map stage '%s' requests %d driver-callable turn(s), but the tool "
                "loop is not wired into the map pipeline -- no tools will be "
                "offered and no tool calls will execute on this stage.",
                stage.name, stage.driver_callable_turns,
            )
            body_json["_gitv_driver_callable"] = True
            body_json["_gitv_driver_callable_turns"] = min(
                stage.driver_callable_turns, caps["max_driver_callable_turns"]
            )

        response_data, status_code = await _forward_stage_llm(body_json, endpoints, model, timeout)

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

        if forbidden_enabled:
            notice = await _scan_stage_forbidden(content, stage, stage_idx, user_id)
            if notice:
                if is_last_stage:
                    # Nothing downstream can act on it, so say so rather than
                    # letting the finding disappear.
                    logger.warning(
                        "Map '%s': forbidden phrases in the FINAL stage '%s' reach "
                        "the client unedited -- no stage follows it. Move the "
                        "editing stage later, or add one.",
                        map_obj.name, stage.name,
                    )
                else:
                    sticky_context.append({"role": "system", "content": notice})

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

            sticky_resources.extend(r for r in stage.resources if r.sticky)

            # Message-level injections are undone by the snapshot reset at the top
            # of the next iteration; only the out-of-band cantrip bookkeeping needs
            # clearing here.
            body_json.pop("_gitv_map_stage_cantrip_ids", None)

        logger.info("Map stage '%s' completed: %d chars output", stage.name, len(content))

    # Scenario summarization POST: applies to the stage-augmented system message
    # after all stages complete, mirroring how the non-map pipeline runs POST
    # (proxy.py ~line 365). PRE already ran at proxy.py:276 before the map branch.
    if body_json.get("_gitv_command_overrides", {}).get("summary") is not False:
        from app.services.scenario_summarizer import maybe_summarize_scenario
        try:
            body_json = await maybe_summarize_scenario(body_json, user_id, "post")
        except Exception:
            logger.exception("Scenario summarization (POST, map) failed")

    return response_data
