"""Live self-check tools (sub-phase 26d).

There is no way to run the test suite from inside the running app, and a user
cannot read the source to know whether their own configuration does what they
think it does. These tools answer the same questions the suite verifies, but
against the caller's own live data: would this resource fire, what parameters
would this call actually use, is anything pointing at something that no longer
exists, does the judge actually judge.

Every check is a coroutine `run(ctx, args) -> list[CheckResult]`. Two rules
govern all of them:

1. Each opens its own short `async_session()` block rather than holding one
   across the whole check, and every query is filtered by `ctx.user_id` --
   nothing here may read another user's configuration.
2. Nothing here may raise. A failure becomes a single `CheckResult` carrying
   the exception text, the same way a failed upstream call degrades elsewhere
   in this product rather than turning into a 500.

`registry.py` wraps each `run` in a local `Tool`; the handler there converts
the returned list into plain dicts (`asdict`) because a dataclass is not JSON
by itself.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.database import async_session

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Same shape as `app.routers.diagnostics.DiagnosticResult`, renamed here
    because this module is not the diagnostics router and should not import
    a Pydantic response model just to reuse four field names."""

    name: str
    passed: bool
    message: str
    detail: str = ""


def _fail(name: str, exc: Exception) -> list[CheckResult]:
    logger.exception("Self-check '%s' failed", name)
    return [CheckResult(name=name, passed=False, message=f"{type(exc).__name__}: {exc}"[:500])]


def _guard(name: str, fn):
    """Wrap one check's implementation so it can never raise past this point."""

    async def run(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
        try:
            return await fn(ctx, args)
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch everything
            return _fail(name, exc)

    return run


# ---------------------------------------------------------------------------
# dry_run_activation
# ---------------------------------------------------------------------------


def _tag_name_matched(resource_tag: str, resource_type: str, tags: list[dict[str, Any]]) -> tuple[bool, bool]:
    """(name_matched, owner_qualified) for one resource against a tag list."""
    if not resource_tag:
        return False, False
    for tag in tags:
        if tag.get("type") not in (resource_type, "taggroup"):
            continue
        if tag.get("name") == resource_tag:
            return True, bool(tag.get("owner"))
    return False, False


def _activation_reason(
    would: bool,
    resource_tag: str,
    resource_type: str,
    is_active: bool,
    is_public: bool,
    owner_id: str,
    user_id: str,
    tags: list[dict[str, Any]],
) -> str:
    matched, owner_qualified = _tag_name_matched(resource_tag, resource_type, tags)
    if would:
        if matched:
            return "Tag matched" + (" via an owner-qualified tag" if owner_qualified else "")
        return "Active blanket flag (no tag needed)"
    if matched:
        return "Tag matched, but the resource is neither public nor owned by this user"
    if resource_tag:
        return "Tag absent from the message"
    return "Not active, and no tag is configured on this resource"


def _selection_reason(
    would: bool,
    resource_tag: str,
    resource_type: str,
    is_active: bool,
    owner_id: str,
    user_id: str,
    tags: list[dict[str, Any]],
) -> str:
    """Reason text for a selection resource (map, memory rule): match-only,
    no Active-flag fallback inside the tagging function itself."""
    matched, owner_qualified = _tag_name_matched(resource_tag, resource_type, tags)
    if would:
        return "Tag matched" + (" via an owner-qualified tag" if owner_qualified else "")
    if matched:
        return "Tag matched, but the resource is not owned by this user"
    if resource_tag:
        return "Tag absent from the message"
    if is_active:
        return "Untagged: eligible as the selection-resource default if nothing else wins"
    return "Untagged and not active: cannot be selected as the default"


async def _dry_run_activation(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.cantrip import Cantrip
    from app.models.endpoint import Endpoint
    from app.models.lorebook import Lorebook
    from app.models.map import Map
    from app.models.memory_rule import MemoryRule
    from app.models.skill import EndpointSkill, Skill
    from app.models.verification import VerificationRule
    from app.services import command_tags, group_resolver, tagging

    message = str(args.get("message") or "")
    raw_tags = args.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []

    messages = [{"role": "user", "content": message}]
    extracted = tagging.extract_all_tags_from_messages(messages)
    seen_raw = {t.get("raw") for t in extracted}
    for raw in raw_tags:
        raw = str(raw)
        if not raw or raw in seen_raw:
            continue
        parsed = tagging.parse_tag(raw)
        parsed["raw"] = raw
        extracted.append(parsed)
        seen_raw.add(raw)

    results: list[CheckResult] = []

    async with async_session() as db:
        expanded_tags, activated_groups = await group_resolver.resolve_group_tags(
            db, ctx.user_id, extracted
        )

    results.append(
        CheckResult(
            name="parsed tags",
            passed=True,
            message=(
                f"{len(extracted)} tag(s) parsed"
                + (f"; {len(activated_groups)} tag group(s) expanded" if activated_groups else "")
            ),
            detail=json.dumps(
                {"tags": extracted, "expanded_tags": expanded_tags, "activated_groups": activated_groups},
                default=str,
            ),
        )
    )

    parsed_commands = command_tags.parse_command_tags(message)
    results.append(
        CheckResult(
            name="command tags",
            passed=True,
            message=(
                f"{len(parsed_commands)} command tag(s) parsed from the message"
                if parsed_commands
                else "No command tags in the message"
            ),
            detail=json.dumps(
                [{"command": c.command, "setting": c.setting, "persist": c.persist} for c in parsed_commands]
            ),
        )
    )

    async with async_session() as db:
        overrides = await command_tags.load_persistent_overrides(db, ctx.user_id, ctx.conversation_id)
    results.append(
        CheckResult(
            name="persisted overrides",
            passed=True,
            message=(
                f"{len(overrides)} persisted command override(s) for this conversation"
                if overrides
                else "No persisted command overrides for this conversation"
            ),
            detail=json.dumps(overrides),
        )
    )

    async with async_session() as db:
        cantrips = (
            await db.execute(
                select(Cantrip).where(or_(Cantrip.user_id == ctx.user_id, Cantrip.is_public.is_(True)))
            )
        ).scalars().all()
        for c in cantrips:
            would = tagging.should_activate_resource(
                c.tag, "cantrip", c.is_active, c.is_public, c.user_id, ctx.user_id, expanded_tags
            )
            results.append(
                CheckResult(
                    name=f"cantrip: {c.name}",
                    passed=would,
                    message=_activation_reason(
                        would, c.tag, "cantrip", c.is_active, c.is_public, c.user_id, ctx.user_id, expanded_tags
                    ),
                    detail=json.dumps({"id": c.id, "tag": c.tag, "owned": c.user_id == ctx.user_id}),
                )
            )

        lorebooks = (
            await db.execute(
                select(Lorebook).where(or_(Lorebook.user_id == ctx.user_id, Lorebook.is_public.is_(True)))
            )
        ).scalars().all()
        for lb in lorebooks:
            would = tagging.should_activate_resource(
                lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, ctx.user_id, expanded_tags
            )
            results.append(
                CheckResult(
                    name=f"lorebook: {lb.name}",
                    passed=would,
                    message=_activation_reason(
                        would, lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, ctx.user_id, expanded_tags
                    ),
                    detail=json.dumps({"id": lb.id, "tag": lb.tag, "owned": lb.user_id == ctx.user_id}),
                )
            )

        rules = (
            await db.execute(select(VerificationRule).where(VerificationRule.user_id == ctx.user_id))
        ).scalars().all()
        for r in rules:
            would = tagging.should_activate_resource(
                r.tag, "verify", r.is_active, False, r.user_id, ctx.user_id, expanded_tags
            )
            results.append(
                CheckResult(
                    name=f"verification rule: {r.name}",
                    passed=would,
                    message=_activation_reason(
                        would, r.tag, "verify", r.is_active, False, r.user_id, ctx.user_id, expanded_tags
                    ),
                    detail=json.dumps({"id": r.id, "tag": r.tag}),
                )
            )

        maps = (
            await db.execute(select(Map).where(or_(Map.user_id == ctx.user_id, Map.is_public.is_(True))))
        ).scalars().all()
        for m in maps:
            would = tagging.tag_matches_resource(
                m.tag, "map", m.is_public, m.user_id, ctx.user_id, expanded_tags
            )
            results.append(
                CheckResult(
                    name=f"map: {m.name}",
                    passed=would,
                    message=_selection_reason(
                        would, m.tag, "map", m.is_active, m.user_id, ctx.user_id, expanded_tags
                    ),
                    detail=json.dumps({"id": m.id, "tag": m.tag, "owned": m.user_id == ctx.user_id}),
                )
            )

        memory_rules = (
            await db.execute(select(MemoryRule).where(MemoryRule.user_id == ctx.user_id))
        ).scalars().all()
        for mr in memory_rules:
            would = tagging.tag_matches_resource(
                mr.tag, "memory-rule", False, mr.user_id, ctx.user_id, expanded_tags
            )
            results.append(
                CheckResult(
                    name=f"memory rule: {mr.name}",
                    passed=would,
                    message=_selection_reason(
                        would, mr.tag, "memory-rule", mr.is_active, mr.user_id, ctx.user_id, expanded_tags
                    ),
                    detail=json.dumps({"id": mr.id, "tag": mr.tag}),
                )
            )

        # Skills carry no tag column and are never candidates for
        # `should_activate_resource` -- there is no "skill" entry in
        # `tagging.TAG_TYPES`. What actually controls whether a skill reaches a
        # request is whether it is attached to one of the caller's endpoints, so
        # that is what is reported here instead of a hierarchy this resource
        # does not participate in.
        skills = (await db.execute(select(Skill).where(Skill.user_id == ctx.user_id))).scalars().all()
        for s in skills:
            attached = (
                await db.execute(
                    select(EndpointSkill)
                    .join(Endpoint, EndpointSkill.endpoint_id == Endpoint.id)
                    .where(EndpointSkill.skill_id == s.id, Endpoint.user_id == ctx.user_id)
                )
            ).scalars().all()
            results.append(
                CheckResult(
                    name=f"skill: {s.name}",
                    passed=bool(attached),
                    message=(
                        f"Attached to {len(attached)} endpoint(s); skills have no tag-based "
                        "activation, only attach_skill/detach_skill"
                        if attached
                        else "Not attached to any endpoint, so it never reaches a request"
                    ),
                    detail=json.dumps({"id": s.id}),
                )
            )

    return results


# ---------------------------------------------------------------------------
# dry_run_lorebook
# ---------------------------------------------------------------------------


def _lorebook_skip_reason(entry: dict[str, Any], conversation_text: str, parse_json_list, keyword_matches) -> str:
    if entry.get("is_disabled"):
        return "Disabled"
    if entry.get("is_constant"):
        return "Budget exhausted before this constant entry"
    primary_keys = parse_json_list(entry.get("keys", "[]"))
    if not primary_keys:
        return "No keys configured"
    if not any(keyword_matches(k, conversation_text) for k in primary_keys):
        return "No key matched the text"
    if entry.get("is_selective"):
        secondary_keys = parse_json_list(entry.get("secondary_keys", "[]"))
        if secondary_keys and not any(keyword_matches(k, conversation_text) for k in secondary_keys):
            return "Selective secondary key not matched"
    return "Budget exhausted before this entry"


async def _dry_run_lorebook(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.lorebook import Lorebook
    from app.services import budget as budget_service
    from app.services.lorebook import (
        _extract_conversation_text,
        _keyword_matches,
        inject_entries,
        match_entries,
        parse_json_list,
    )

    text = str(args.get("text") or "")
    lorebook_id = str(args.get("lorebook_id") or "")
    messages = [{"role": "user", "content": text}]

    async with async_session() as db:
        q = select(Lorebook).where(Lorebook.user_id == ctx.user_id).options(selectinload(Lorebook.entries))
        if lorebook_id:
            q = q.where(Lorebook.id == lorebook_id)
        lorebooks = (await db.execute(q)).scalars().all()

    if lorebook_id and not lorebooks:
        return [CheckResult("dry_run_lorebook", False, f"No lorebook '{lorebook_id}' owned by this user")]

    all_entries: list[dict[str, Any]] = []
    for lb in lorebooks:
        for e in lb.entries:
            all_entries.append(
                {
                    "name": e.name,
                    "keys": e.keys,
                    "secondary_keys": e.secondary_keys,
                    "content": e.content,
                    "position": e.position,
                    "insertion_order": e.insertion_order,
                    "is_constant": e.is_constant,
                    "is_selective": e.is_selective,
                    "is_disabled": e.is_disabled,
                    "lorebook_id": lb.id,
                    "lorebook_name": lb.name,
                    "entry_id": e.id,
                }
            )

    if not all_entries:
        return [CheckResult("dry_run_lorebook", True, "No entries to evaluate: the lorebook(s) selected are empty")]

    try:
        percent, window_override = await budget_service.load_budget_config(ctx.user_id)
        window = budget_service.estimate_context_window({}, window_override)
        total_budget_chars = int(window * percent / 100.0 * 4) if percent > 0 else 0
    except Exception:
        total_budget_chars = 0

    matched = match_entries(messages, all_entries, total_budget_chars)
    matched_ids = {m.entry_id for m in matched}
    conversation_text = _extract_conversation_text(messages)

    results: list[CheckResult] = []
    running_tokens = 0
    for position, m in enumerate(matched, start=1):
        content_tokens = budget_service.estimate_tokens(m.content) if m.content else 0
        running_tokens += content_tokens
        results.append(
            CheckResult(
                name=f"fired: {m.name or m.entry_id}",
                passed=True,
                message=f"Fired at position {position}; running cost {running_tokens} token(s)",
                detail=json.dumps(
                    {
                        "lorebook": m.lorebook_name,
                        "entry_id": m.entry_id,
                        "position_rule": m.position,
                        "insertion_order": m.insertion_order,
                        "content_tokens": content_tokens,
                    }
                ),
            )
        )

    for entry in all_entries:
        if entry.get("entry_id") in matched_ids:
            continue
        reason = _lorebook_skip_reason(entry, conversation_text, parse_json_list, _keyword_matches)
        results.append(
            CheckResult(
                name=f"skipped: {entry.get('name') or entry.get('entry_id')}",
                passed=False,
                message=reason,
                detail=json.dumps({"lorebook": entry.get("lorebook_name"), "entry_id": entry.get("entry_id")}),
            )
        )

    injected = inject_entries(messages, matched)
    results.append(
        CheckResult(
            name="injected messages",
            passed=True,
            message=(
                f"{len(matched)} entr{'y' if len(matched) == 1 else 'ies'} injected into "
                f"{len(injected)} message(s)"
            ),
            detail=json.dumps(injected, default=str)[:4000],
        )
    )

    return results


# ---------------------------------------------------------------------------
# explain_parameters
# ---------------------------------------------------------------------------

_PARAM_SCOPES = ("endpoint", "verification_rule", "map_stage", "scenario_rule", "summarizer")


async def _explain_parameters(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.endpoint import Endpoint
    from app.models.user_settings import UserSettings
    from app.services.llm_params import apply_to_body, parse_params, resolve
    from app.services.routing import endpoint_parameters, model_parameter_map

    scope = str(args.get("scope") or "").strip()
    obj_id = str(args.get("id") or "").strip()
    model = str(args.get("model") or "").strip()

    if scope not in _PARAM_SCOPES:
        return [
            CheckResult(
                "explain_parameters",
                False,
                f"Unknown scope '{scope}'. Expected one of {', '.join(_PARAM_SCOPES)}",
            )
        ]

    async with async_session() as db:
        us = (
            await db.execute(select(UserSettings).where(UserSettings.user_id == ctx.user_id))
        ).scalar_one_or_none()

        layers: list[tuple[str, list]] = []
        endpoint: Endpoint | None = None

        if scope == "endpoint":
            if not obj_id:
                return [CheckResult("explain_parameters", False, "id (endpoint id) is required for scope 'endpoint'")]
            endpoint = (
                await db.execute(select(Endpoint).where(Endpoint.id == obj_id, Endpoint.user_id == ctx.user_id))
            ).scalar_one_or_none()
            if endpoint is None:
                return [CheckResult("explain_parameters", False, f"No endpoint '{obj_id}' owned by this user")]
            model = model or endpoint.default_model or ""
            layers.append(("user settings", parse_params(us.parameters_json, "user settings") if us else []))
            layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
            layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))

        elif scope == "verification_rule":
            from app.models.verification import VerificationRule

            rule = None
            if obj_id:
                rule = (
                    await db.execute(
                        select(VerificationRule).where(
                            VerificationRule.id == obj_id, VerificationRule.user_id == ctx.user_id
                        )
                    )
                ).scalar_one_or_none()
                if rule is None:
                    return [
                        CheckResult("explain_parameters", False, f"No verification rule '{obj_id}' owned by this user")
                    ]
            endpoint_id = rule.verification_endpoint_id if rule and rule.verification_endpoint_id else (
                us.verification_endpoint_id if us else None
            )
            if endpoint_id:
                endpoint = (
                    await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.enabled.is_(True)))
                ).scalar_one_or_none()
            model = (
                model
                or (rule.verification_model if rule else "")
                or (us.verification_model if us else "")
                or (endpoint.default_model if endpoint else "")
            )
            layers.append(
                (
                    "user settings (verification)",
                    parse_params(us.verification_parameters_json, "user settings (verification)") if us else [],
                )
            )
            if endpoint is not None:
                layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
                layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
            if rule is not None:
                layers.append((f"verification rule '{rule.name}'", parse_params(rule.parameters_json, "rule")))

        elif scope == "map_stage":
            from app.models.map import Map, MapStage
            from app.services.routing import resolve_endpoints_by_tag

            if not obj_id:
                return [CheckResult("explain_parameters", False, "id (map stage id) is required for scope 'map_stage'")]
            stage = (
                await db.execute(
                    select(MapStage)
                    .join(Map, MapStage.map_id == Map.id)
                    .where(MapStage.id == obj_id, Map.user_id == ctx.user_id)
                )
            ).scalar_one_or_none()
            if stage is None:
                return [CheckResult("explain_parameters", False, f"No map stage '{obj_id}' owned by this user")]
            if stage.endpoint_tag:
                tagged = await resolve_endpoints_by_tag(db, ctx.user_id, stage.endpoint_tag)
                endpoint = tagged[0] if tagged else None
            if endpoint is None and stage.endpoint_id:
                endpoint = (
                    await db.execute(select(Endpoint).where(Endpoint.id == stage.endpoint_id, Endpoint.enabled.is_(True)))
                ).scalar_one_or_none()
            model = model or stage.model_override or (endpoint.default_model if endpoint else "")
            layers.append(("user settings", parse_params(us.parameters_json, "user settings") if us else []))
            if endpoint is not None:
                layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
                layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
            layers.append((f"map stage '{stage.name}'", parse_params(stage.parameters_json, "map stage")))

        elif scope == "scenario_rule":
            from app.models.scenario_rule import ScenarioRule

            if not obj_id:
                return [CheckResult("explain_parameters", False, "id (scenario rule id) is required for scope 'scenario_rule'")]
            rule = (
                await db.execute(
                    select(ScenarioRule).where(ScenarioRule.id == obj_id, ScenarioRule.user_id == ctx.user_id)
                )
            ).scalar_one_or_none()
            if rule is None:
                return [CheckResult("explain_parameters", False, f"No scenario rule '{obj_id}' owned by this user")]
            if rule.endpoint_id:
                endpoint = (
                    await db.execute(
                        select(Endpoint).where(
                            Endpoint.id == rule.endpoint_id, Endpoint.user_id == ctx.user_id, Endpoint.enabled.is_(True)
                        )
                    )
                ).scalar_one_or_none()
            if endpoint is None and us and us.default_endpoint_id:
                endpoint = (
                    await db.execute(
                        select(Endpoint).where(
                            Endpoint.id == us.default_endpoint_id,
                            Endpoint.user_id == ctx.user_id,
                            Endpoint.enabled.is_(True),
                        )
                    )
                ).scalar_one_or_none()
            model = model or rule.model or (endpoint.default_model if endpoint else "")
            layers.append(
                (
                    "user settings (summarization)",
                    parse_params(us.summarization_parameters_json, "user settings (summarization)") if us else [],
                )
            )
            if endpoint is not None:
                layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
                layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
            layers.append((f"scenario rule '{rule.name}'", parse_params(rule.parameters_json, "scenario rule")))

        else:  # summarizer
            if us and us.summarization_endpoint_id:
                endpoint = (
                    await db.execute(
                        select(Endpoint).where(Endpoint.id == us.summarization_endpoint_id, Endpoint.enabled.is_(True))
                    )
                ).scalar_one_or_none()
            model = model or (us.summarization_model if us else "") or (endpoint.default_model if endpoint else "")
            layers.append(
                (
                    "user settings (summarization)",
                    parse_params(us.summarization_parameters_json, "user settings (summarization)") if us else [],
                )
            )
            if endpoint is not None:
                layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
                layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))

        resolved = resolve(layers)
        sample_body = {"model": model, "messages": [{"role": "user", "content": "(preview only, never sent)"}], "stream": False}
        preview = apply_to_body(sample_body, resolved)

    return [
        CheckResult(
            name=f"explain_parameters: {scope}",
            passed=True,
            message=(
                f"{len(resolved.values)} parameter(s) resolved across {len(layers)} layer(s)"
                if resolved.values
                else "No parameters configured at any layer"
            ),
            detail=json.dumps(
                {
                    "model": model,
                    "values": resolved.values,
                    "sources": resolved.sources,
                    "layers_considered": [label for label, _ in layers],
                    "outbound_body_preview": preview,
                },
                default=str,
            ),
        )
    ]


# ---------------------------------------------------------------------------
# lint_configuration
# ---------------------------------------------------------------------------


def _param_blob_finding(label: str, raw: str | None) -> CheckResult | None:
    """None when the blob is clean; a failing result naming what was dropped."""
    from app.services.llm_params import RESERVED_NAMES, parse_params

    if not raw or raw == "[]":
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return CheckResult(f"parameter blob: {label}", False, "Malformed JSON; the whole blob is silently ignored at runtime")
    if not isinstance(data, list):
        return CheckResult(f"parameter blob: {label}", False, "Not a JSON list; the whole blob is silently ignored at runtime")

    dropped: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            dropped.append(repr(entry)[:40])
            continue
        name = str(entry.get("name", "")).strip()
        if not name or name in RESERVED_NAMES or name.startswith("_gitv"):
            dropped.append(name or "(no name)")

    kept = parse_params(raw, label)
    if dropped or len(kept) < len([e for e in data if isinstance(e, dict)]):
        return CheckResult(
            f"parameter blob: {label}",
            False,
            f"{len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} silently dropped",
            detail=json.dumps({"dropped": dropped}),
        )
    return None


async def _lint_configuration(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.endpoint import Endpoint, EndpointModel
    from app.models.forbidden_word import ForbiddenWord
    from app.models.lorebook import Lorebook
    from app.models.map import Map, MapStage
    from app.models.memory import Memory
    from app.models.scenario_rule import ScenarioRule
    from app.models.skill import EndpointSkill, Skill
    from app.models.tag_group import TagGroup
    from app.models.user_settings import UserSettings
    from app.models.verification import VerificationRule
    from app.services import map_transfer
    from app.services.admin import get_url_blocklist
    from app.services.safety_scanner import scan_cantrip, scan_lorebook, scan_map_report
    from app.services.sanitization import sanitize_for_injection

    results: list[CheckResult] = []

    async with async_session() as db:
        us = (await db.execute(select(UserSettings).where(UserSettings.user_id == ctx.user_id))).scalar_one_or_none()
        endpoints = (await db.execute(select(Endpoint).where(Endpoint.user_id == ctx.user_id))).scalars().all()
        endpoints_by_id = {e.id: e for e in endpoints}
        enabled_tags = {e.role_tag for e in endpoints if e.enabled}
        models_all = (
            await db.execute(select(EndpointModel).where(EndpointModel.endpoint_id.in_([e.id for e in endpoints])))
        ).scalars().all() if endpoints else []
        verification_rules = (
            await db.execute(select(VerificationRule).where(VerificationRule.user_id == ctx.user_id))
        ).scalars().all()
        maps = (
            await db.execute(
                select(Map)
                .where(Map.user_id == ctx.user_id)
                .options(selectinload(Map.stages).selectinload(MapStage.resources))
            )
        ).scalars().all()
        scenario_rules = (
            await db.execute(select(ScenarioRule).where(ScenarioRule.user_id == ctx.user_id))
        ).scalars().all()
        skills = (await db.execute(select(Skill).where(Skill.user_id == ctx.user_id))).scalars().all()
        skill_attachments = (
            await db.execute(
                select(EndpointSkill).where(EndpointSkill.skill_id.in_([s.id for s in skills]))
            )
        ).scalars().all() if skills else []
        forbidden_words = (
            await db.execute(select(ForbiddenWord).where(ForbiddenWord.user_id == ctx.user_id))
        ).scalars().all()
        tag_groups = (
            await db.execute(
                select(TagGroup).where(TagGroup.user_id == ctx.user_id).options(selectinload(TagGroup.members))
            )
        ).scalars().all()
        lorebooks = (
            await db.execute(
                select(Lorebook).where(Lorebook.user_id == ctx.user_id).options(selectinload(Lorebook.entries))
            )
        ).scalars().all()
        from app.models.cantrip import Cantrip

        cantrips = (await db.execute(select(Cantrip).where(Cantrip.user_id == ctx.user_id))).scalars().all()
        cantrip_ids = {c.id for c in cantrips}
        lorebook_ids = {lb.id for lb in lorebooks}
        memories = (await db.execute(select(Memory).where(Memory.user_id == ctx.user_id))).scalars().all()
        blocklist = await get_url_blocklist()

        # --- dropped parameter blobs -------------------------------------
        blob_findings: list[CheckResult] = []
        if us:
            for label, raw in (
                ("user settings", us.parameters_json),
                ("user settings (verification)", us.verification_parameters_json),
                ("user settings (summarization)", us.summarization_parameters_json),
                ("assistant settings", us.assistant_parameters_json),
            ):
                found = _param_blob_finding(label, raw)
                if found:
                    blob_findings.append(found)
        for e in endpoints:
            found = _param_blob_finding(f"endpoint '{e.name}'", e.parameters_json)
            if found:
                blob_findings.append(found)
        for m in models_all:
            ep = endpoints_by_id.get(m.endpoint_id)
            found = _param_blob_finding(f"model '{m.name}' on '{ep.name if ep else m.endpoint_id}'", m.parameters_json)
            if found:
                blob_findings.append(found)
        for r in verification_rules:
            found = _param_blob_finding(f"verification rule '{r.name}'", r.parameters_json)
            if found:
                blob_findings.append(found)
        for mp in maps:
            for st in mp.stages:
                for label, raw in ((f"map stage '{st.name}'", st.parameters_json), (f"map stage '{st.name}' verification", st.verification_parameters_json)):
                    found = _param_blob_finding(label, raw)
                    if found:
                        blob_findings.append(found)
        for sr in scenario_rules:
            found = _param_blob_finding(f"scenario rule '{sr.name}'", sr.parameters_json)
            if found:
                blob_findings.append(found)

        if blob_findings:
            results.extend(blob_findings)
        else:
            results.append(CheckResult("parameter blobs", True, "No dropped parameter entries found"))

        # --- forbidden-word regexes ---------------------------------------
        bad_regexes: list[CheckResult] = []
        for fw in forbidden_words:
            if not fw.is_regex:
                continue
            try:
                re.compile(fw.phrase)
            except re.error as exc:
                bad_regexes.append(
                    CheckResult(
                        f"forbidden word regex: {fw.phrase[:60]}",
                        False,
                        f"Does not compile, so it is silently skipped at runtime: {exc}",
                    )
                )
        if bad_regexes:
            results.extend(bad_regexes)
        else:
            results.append(CheckResult("forbidden word regexes", True, "Every regex entry compiles"))

        # --- dangling / disabled endpoint references ----------------------
        endpoint_findings: list[CheckResult] = []

        def _check_endpoint_ref(label: str, endpoint_id: str | None) -> None:
            if not endpoint_id:
                return
            ep = endpoints_by_id.get(endpoint_id)
            if ep is None:
                endpoint_findings.append(CheckResult(f"endpoint reference: {label}", False, "Points at an endpoint that no longer exists"))
            elif not ep.enabled:
                endpoint_findings.append(CheckResult(f"endpoint reference: {label}", False, f"Points at disabled endpoint '{ep.name}'"))

        if us:
            _check_endpoint_ref("default endpoint (settings)", us.default_endpoint_id)
            _check_endpoint_ref("verification endpoint (settings)", us.verification_endpoint_id)
            _check_endpoint_ref("summarization endpoint (settings)", us.summarization_endpoint_id)
            _check_endpoint_ref("assistant endpoint (settings)", us.assistant_endpoint_id)
        for r in verification_rules:
            _check_endpoint_ref(f"verification rule '{r.name}'", r.verification_endpoint_id)
        for mp in maps:
            for st in mp.stages:
                _check_endpoint_ref(f"map stage '{st.name}'", st.endpoint_id)
                _check_endpoint_ref(f"map stage '{st.name}' verification", st.verification_endpoint_id)
        for sr in scenario_rules:
            _check_endpoint_ref(f"scenario rule '{sr.name}'", sr.endpoint_id)
        for att in skill_attachments:
            ep = endpoints_by_id.get(att.endpoint_id)
            skill = next((s for s in skills if s.id == att.skill_id), None)
            if ep is not None and not ep.enabled and skill is not None:
                endpoint_findings.append(
                    CheckResult(f"skill attachment: {skill.name}", False, f"Attached to disabled endpoint '{ep.name}'")
                )

        if endpoint_findings:
            results.extend(endpoint_findings)
        else:
            results.append(CheckResult("endpoint references", True, "Every endpoint reference points at an existing, enabled endpoint"))

        # --- map stage endpoint_tag with no enabled endpoint --------------
        tag_findings: list[CheckResult] = []
        for mp in maps:
            for st in mp.stages:
                if st.endpoint_tag and st.endpoint_tag not in enabled_tags:
                    tag_findings.append(
                        CheckResult(
                            f"map stage endpoint tag: {mp.name} / {st.name}",
                            False,
                            f"endpoint_tag '{st.endpoint_tag}' matches no enabled endpoint",
                        )
                    )
        if tag_findings:
            results.extend(tag_findings)
        else:
            results.append(CheckResult("map stage endpoint tags", True, "Every stage endpoint_tag matches an enabled endpoint"))

        # --- tag group members ---------------------------------------------
        member_findings: list[CheckResult] = []
        for tg in tag_groups:
            for member in tg.members:
                if member.member_type == "lorebook" and member.member_id not in lorebook_ids:
                    member_findings.append(
                        CheckResult(f"tag group member: {tg.name}", False, f"References a missing lorebook ({member.member_id})")
                    )
                elif member.member_type == "cantrip" and member.member_id not in cantrip_ids:
                    member_findings.append(
                        CheckResult(f"tag group member: {tg.name}", False, f"References a missing cantrip ({member.member_id})")
                    )
        if member_findings:
            results.extend(member_findings)
        else:
            results.append(CheckResult("tag group members", True, "Every tag group member resolves to an existing resource"))

        # --- safety re-scan --------------------------------------------------
        safety_findings: list[CheckResult] = []
        for c in cantrips:
            scan = scan_cantrip(c.code)
            if not scan.safe:
                safety_findings.append(CheckResult(f"safety scan: cantrip '{c.name}'", False, scan.summary, detail=json.dumps([f.description for f in scan.findings])))
        for lb in lorebooks:
            entries = [{"name": e.name, "content": e.content} for e in lb.entries]
            scan = scan_lorebook(entries)
            if not scan.safe:
                safety_findings.append(CheckResult(f"safety scan: lorebook '{lb.name}'", False, scan.summary, detail=json.dumps([f.description for f in scan.findings])))
        for mp in maps:
            try:
                export = await map_transfer.serialize_map_to_export(db, mp, mode="embedded")
                report = scan_map_report(export)
                if not report.safe:
                    safety_findings.append(
                        CheckResult(f"safety scan: map '{mp.name}'", False, report.aggregate.summary, detail=json.dumps([f.description for f in report.aggregate.findings]))
                    )
            except Exception as exc:
                safety_findings.append(CheckResult(f"safety scan: map '{mp.name}'", False, f"Could not scan: {exc}"))
        if safety_findings:
            results.extend(safety_findings)
        else:
            results.append(CheckResult("safety re-scan", True, "No cantrip, lorebook or map flagged unsafe under current rules"))

        # --- sanitization sweep ----------------------------------------------
        sanitize_findings: list[CheckResult] = []

        def _sweep(label: str, text: str) -> None:
            if not text:
                return
            result = sanitize_for_injection(text, blocklist)
            if result.flagged:
                sanitize_findings.append(
                    CheckResult(
                        f"sanitization: {label}",
                        False,
                        f"{len(result.findings)} finding(s)",
                        detail=json.dumps([{"kind": f.kind, "detail": f.detail} for f in result.findings]),
                    )
                )

        for lb in lorebooks:
            for e in lb.entries:
                _sweep(f"lorebook entry '{e.name}' ({lb.name})", e.content)
        for s in skills:
            _sweep(f"skill '{s.name}'", s.content)
        for mem in memories:
            _sweep(f"memory '{mem.key}'", mem.value)
        for sr in scenario_rules:
            _sweep(f"scenario rule '{sr.name}'", sr.prompt)
        for c in cantrips:
            _sweep(f"cantrip description '{c.name}'", c.description)

        if sanitize_findings:
            results.extend(sanitize_findings)
        else:
            results.append(CheckResult("sanitization sweep", True, "No zero-width, blocklisted-URL or injection-marker content found"))

    return results


# ---------------------------------------------------------------------------
# report_routing
# ---------------------------------------------------------------------------


async def _report_routing(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.api_key import ApiKey
    from app.models.endpoint import Endpoint
    from app.services.routing import _resolve_default_endpoint, resolve_endpoints_by_tag

    results: list[CheckResult] = []
    async with async_session() as db:
        endpoints = (await db.execute(select(Endpoint).where(Endpoint.user_id == ctx.user_id))).scalars().all()
        tags = sorted({e.role_tag or "default" for e in endpoints})

        if not tags:
            results.append(CheckResult("failover chains", True, "No endpoints configured"))

        for tag in tags:
            chain = await resolve_endpoints_by_tag(db, ctx.user_id, tag)
            results.append(
                CheckResult(
                    name=f"failover chain: {tag}",
                    passed=bool(chain),
                    message=(f"{len(chain)} endpoint(s) in priority order" if chain else "No enabled endpoint for this tag"),
                    detail=json.dumps(
                        [
                            {
                                "name": e.name,
                                "model": e.default_model,
                                "tag": e.role_tag,
                                "priority": e.priority,
                                "api_key_set": bool(e.api_key),
                            }
                            for e in chain
                        ]
                    ),
                )
            )

        default_ep = await _resolve_default_endpoint(db, ctx.user_id)
        results.append(
            CheckResult(
                name="no-key fallback",
                passed=default_ep is not None,
                message=(f"Falls back to '{default_ep.name}'" if default_ep else "No enabled endpoint available as a fallback"),
                detail=json.dumps({"name": default_ep.name, "api_key_set": bool(default_ep.api_key)} if default_ep else {}),
            )
        )

        keys = (
            await db.execute(
                select(ApiKey)
                .where(ApiKey.user_id == ctx.user_id, ApiKey.is_active.is_(True))
                .options(selectinload(ApiKey.endpoint))
            )
        ).scalars().all()
        for k in keys:
            routed_to = k.endpoint.name if k.endpoint else (default_ep.name if default_ep else None)
            results.append(
                CheckResult(
                    name=f"api key: {k.label}",
                    passed=routed_to is not None,
                    message=(f"Routes to '{routed_to}'" if routed_to else "Has no bound endpoint and no fallback is available"),
                    detail="",
                )
            )

    return results


# ---------------------------------------------------------------------------
# run_sandbox_smoke
# ---------------------------------------------------------------------------


async def _run_sandbox_smoke(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.cantrip import Cantrip
    from app.services.cantrip import test_cantrip
    from app.services.cantrip_context import build_context
    from app.services.deno_runner import CantripExecutionError, CantripTimeoutError, _find_deno

    if not _find_deno():
        return [CheckResult("run_sandbox_smoke", True, "Deno is not installed; sandbox checks skipped")]

    results: list[CheckResult] = []
    context = build_context(messages=[{"role": "user", "content": "Hello"}], conversation_id="selfcheck")

    trivial_code = "context.tool_result = 'selfcheck-ok';"
    try:
        trivial = await test_cantrip(code=trivial_code, context=context, timeout_ms=5000)
        results.append(
            CheckResult(
                name="sandbox smoke test",
                passed=not trivial.has_error,
                message=trivial.error or "The Deno sandbox runs and returns a modified context",
            )
        )
    except (CantripTimeoutError, CantripExecutionError) as exc:
        results.append(CheckResult("sandbox smoke test", False, str(exc)))

    cantrip_id = str(args.get("cantrip_id") or "")
    async with async_session() as db:
        q = select(Cantrip).where(Cantrip.user_id == ctx.user_id)
        q = q.where(Cantrip.id == cantrip_id) if cantrip_id else q.where(Cantrip.is_active.is_(True))
        cantrips = (await db.execute(q)).scalars().all()

    if cantrip_id and not cantrips:
        results.append(CheckResult(f"cantrip: {cantrip_id}", False, "No cantrip with that id owned by this user"))

    for c in cantrips:
        try:
            outcome = await test_cantrip(code=c.code, context=context, timeout_ms=c.timeout_ms)
            results.append(
                CheckResult(
                    name=f"cantrip: {c.name}",
                    passed=not outcome.has_error,
                    message=outcome.error or "Ran cleanly",
                    detail="\n".join(outcome.debug_logs)[:2000],
                )
            )
        except CantripTimeoutError:
            results.append(CheckResult(f"cantrip: {c.name}", False, f"Timed out after {c.timeout_ms}ms"))
        except CantripExecutionError as exc:
            results.append(CheckResult(f"cantrip: {c.name}", False, str(exc)))

    return results


# ---------------------------------------------------------------------------
# probe_endpoint
# ---------------------------------------------------------------------------


async def _models_named_by_rules_and_stages(user_id: str, endpoint_id: str) -> set[str]:
    from app.models.map import Map, MapStage
    from app.models.verification import VerificationRule

    names: set[str] = set()
    async with async_session() as db:
        rules = (
            await db.execute(
                select(VerificationRule).where(
                    VerificationRule.user_id == user_id, VerificationRule.verification_endpoint_id == endpoint_id
                )
            )
        ).scalars().all()
        names |= {r.verification_model for r in rules if r.verification_model}

        stages = (
            await db.execute(
                select(MapStage).join(Map, MapStage.map_id == Map.id).where(Map.user_id == user_id, MapStage.endpoint_id == endpoint_id)
            )
        ).scalars().all()
        names |= {s.model_override for s in stages if s.model_override}
    return names


async def _probe_endpoint(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    import httpx

    from app.models.endpoint import Endpoint
    from app.services.proxy import _build_upstream_url, _do_forward

    endpoint_id = str(args.get("endpoint_id") or "")
    streaming = bool(args.get("streaming", False))
    if not endpoint_id:
        return [CheckResult("probe_endpoint", False, "endpoint_id is required")]

    async with async_session() as db:
        endpoint = (
            await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.user_id == ctx.user_id))
        ).scalar_one_or_none()
    if endpoint is None:
        return [CheckResult("probe_endpoint", False, f"No endpoint '{endpoint_id}' owned by this user")]

    results: list[CheckResult] = []
    model = endpoint.default_model or "test"
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
    url = _build_upstream_url(endpoint.base_url, "/v1/chat/completions", endpoint.api_base_path or "")
    headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}

    data, status = await _do_forward(
        "POST",
        url,
        headers,
        json.dumps(body).encode(),
        httpx.Timeout(15.0, connect=10.0),
        provider=endpoint.provider or "",
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
    )
    results.append(
        CheckResult(
            name="1-token probe",
            passed=status == 200,
            message=f"Endpoint returned {status}",
            detail=json.dumps(data, default=str)[:1000],
        )
    )

    if streaming:
        try:
            first_chunk = None
            stream_body = json.dumps({**body, "stream": True}).encode()
            async with httpx.AsyncClient(timeout=15.0) as client, client.stream(
                "POST", url, headers=headers, content=stream_body
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.strip():
                        first_chunk = line
                        break
            results.append(
                CheckResult(
                    name="streaming probe",
                    passed=first_chunk is not None,
                    message=(f"First SSE chunk: {first_chunk[:200]}" if first_chunk else "No data received within the timeout"),
                )
            )
        except Exception as exc:
            results.append(CheckResult("streaming probe", False, f"{type(exc).__name__}: {exc}"))

    try:
        models_url = _build_upstream_url(endpoint.base_url, "/v1/models", endpoint.api_base_path or "")
        async with httpx.AsyncClient(timeout=15.0) as client:
            models_resp = await client.get(models_url, headers={"Authorization": f"Bearer {endpoint.api_key}"})
        live_models: set[str] = set()
        if models_resp.status_code == 200:
            payload = models_resp.json()
            live_models = {m.get("id") for m in payload.get("data", []) if isinstance(m, dict) and m.get("id")}
        curated = {m.name for m in endpoint.models}
        named = await _models_named_by_rules_and_stages(ctx.user_id, endpoint.id)
        missing_curated = curated - live_models if live_models else set()
        missing_named = named - live_models if live_models else set()
        results.append(
            CheckResult(
                name="model list comparison",
                passed=not (missing_curated or missing_named) if live_models else True,
                message=(
                    "Could not compare: the endpoint returned no live model list"
                    if not live_models
                    else "Live model list covers the curated and referenced models"
                    if not (missing_curated or missing_named)
                    else f"{len(missing_curated)} curated / {len(missing_named)} referenced model(s) missing from the live list"
                ),
                detail=json.dumps({"live_count": len(live_models), "missing_curated": sorted(missing_curated), "missing_named": sorted(missing_named)}),
            )
        )
    except Exception as exc:
        results.append(CheckResult("model list comparison", False, f"{type(exc).__name__}: {exc}"))

    return results


# ---------------------------------------------------------------------------
# probe_judge
# ---------------------------------------------------------------------------


async def _probe_judge(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.endpoint import Endpoint
    from app.models.user_settings import UserSettings
    from app.models.verification import VerificationRule
    from app.services.verification import check_response

    rule_id = str(args.get("rule_id") or "")

    async with async_session() as db:
        rule: VerificationRule
        if rule_id:
            found = (
                await db.execute(
                    select(VerificationRule).where(VerificationRule.id == rule_id, VerificationRule.user_id == ctx.user_id)
                )
            ).scalar_one_or_none()
            if found is None:
                return [CheckResult("probe_judge", False, f"No verification rule '{rule_id}' owned by this user")]
            rule = found
        else:
            rule = VerificationRule(
                user_id=ctx.user_id,
                name="selfcheck-throwaway",
                prompt="The response must not contain the word BANANA.",
                is_active=True,
                max_retries=0,
                execution_order=0,
            )

        us = (await db.execute(select(UserSettings).where(UserSettings.user_id == ctx.user_id))).scalar_one_or_none()
        endpoint_id = rule.verification_endpoint_id or (us.verification_endpoint_id if us else None)
        if not endpoint_id:
            return [CheckResult("probe_judge", False, "No verification endpoint configured")]
        endpoint = (
            await db.execute(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.enabled.is_(True)))
        ).scalar_one_or_none()
        if endpoint is None:
            return [CheckResult("probe_judge", False, "The configured verification endpoint was not found or is disabled")]
        model = rule.verification_model or (us.verification_model if us else "") or endpoint.default_model or ""

    results: list[CheckResult] = []

    violating = await check_response("BANANA BANANA BANANA", [rule], endpoint, model)
    j = violating.judgments[0] if violating.judgments else None
    results.append(
        CheckResult(
            name="violating probe",
            passed=bool(j and j.violation and not j.errored),
            message=(
                "No judgment returned"
                if j is None
                else f"Judge errored: {j.reason}"
                if j.errored
                else "Judge correctly reported a violation"
                if j.violation
                else "Judge said CLEAN for a probe that should have violated"
            ),
            detail=(j.reason if j else ""),
        )
    )

    clean = await check_response("The weather today is pleasant and unremarkable.", [rule], endpoint, model)
    jc = clean.judgments[0] if clean.judgments else None
    results.append(
        CheckResult(
            name="clean probe",
            passed=bool(jc and not jc.violation and not jc.errored),
            message=(
                "No judgment returned"
                if jc is None
                else f"Judge errored: {jc.reason}"
                if jc.errored
                else "Judge correctly reported no violation"
                if not jc.violation
                else "Judge flagged a violation on clean text"
            ),
            detail=(jc.reason if jc else ""),
        )
    )
    return results


# ---------------------------------------------------------------------------
# probe_summarizer
# ---------------------------------------------------------------------------


async def _probe_summarizer(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.endpoint import Endpoint
    from app.models.user_settings import UserSettings
    from app.services.summarization import summarize_messages, summarizer_parameters

    async with async_session() as db:
        us = (await db.execute(select(UserSettings).where(UserSettings.user_id == ctx.user_id))).scalar_one_or_none()
        if not us or not us.summarization_endpoint_id:
            return [CheckResult("probe_summarizer", False, "No summarization endpoint configured")]
        endpoint = (
            await db.execute(select(Endpoint).where(Endpoint.id == us.summarization_endpoint_id, Endpoint.enabled.is_(True)))
        ).scalar_one_or_none()
        if endpoint is None:
            return [CheckResult("probe_summarizer", False, "The configured summarization endpoint was not found or is disabled")]
        model = us.summarization_model or endpoint.default_model or ""
        configured = summarizer_parameters(us, endpoint, model)
        prompt = us.summarization_prompt

    canned = [
        {"role": "user", "content": "I arrive at the tavern looking for the innkeeper."},
        {"role": "assistant", "content": "The innkeeper, a stout woman named Greta, waves you over."},
        {"role": "user", "content": "I ask her about rumors of a dragon in the hills."},
        {"role": "assistant", "content": "Greta lowers her voice and tells you of strange lights on Mount Kaldris."},
        {"role": "user", "content": "I thank her and order a mug of ale."},
        {"role": "assistant", "content": "She pours you a frothy ale and wishes you luck on your journey."},
    ]
    summary = await summarize_messages(canned, endpoint, model, prompt, configured=configured)
    return [
        CheckResult(
            name="probe_summarizer",
            passed=bool(summary.strip()),
            message=(f"Summary returned ({len(summary)} chars)" if summary.strip() else "No summary was returned"),
            detail=summary[:1000],
        )
    ]


# ---------------------------------------------------------------------------
# probe_map
# ---------------------------------------------------------------------------


async def _probe_map(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    import httpx

    from app.models.map import Map, MapStage
    from app.services.map_pipeline import _resolve_stage_endpoint, run_map_pipeline

    map_id = str(args.get("map_id") or "")
    if not map_id:
        return [CheckResult("probe_map", False, "map_id is required")]

    async with async_session() as db:
        map_obj = (
            await db.execute(
                select(Map)
                .where(Map.id == map_id, Map.user_id == ctx.user_id)
                .options(selectinload(Map.stages).selectinload(MapStage.resources))
            )
        ).scalar_one_or_none()
    if map_obj is None:
        return [CheckResult("probe_map", False, f"No map '{map_id}' owned by this user")]

    results: list[CheckResult] = []
    async with async_session() as db:
        for stage in sorted(map_obj.stages, key=lambda s: s.stage_order):
            try:
                endpoints, model = await _resolve_stage_endpoint(db, stage, ctx.user_id)
                results.append(
                    CheckResult(
                        name=f"stage resolution: {stage.name}",
                        passed=bool(endpoints),
                        message=(f"Resolves to '{endpoints[0].name}'" if endpoints else "No endpoint resolves for this stage"),
                    )
                )
            except Exception as exc:
                results.append(CheckResult(f"stage resolution: {stage.name}", False, f"{type(exc).__name__}: {exc}"))

    body_json = {"messages": [{"role": "user", "content": "Hello, this is a self-check probe."}]}
    started = time.monotonic()
    try:
        response = await run_map_pipeline(body_json, ctx.user_id, {}, map_obj, httpx.Timeout(30.0, connect=10.0))
        elapsed_ms = (time.monotonic() - started) * 1000.0
        ok = "error" not in response and bool(response.get("choices"))
        results.append(
            CheckResult(
                name="pipeline run",
                passed=ok,
                message=(f"Completed in {elapsed_ms:.0f}ms" if ok else f"Failed: {response.get('error', 'unknown error')}"),
                detail=json.dumps(response, default=str)[:1500],
            )
        )
    except Exception as exc:
        results.append(CheckResult("pipeline run", False, f"{type(exc).__name__}: {exc}"))

    return results


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "dry_run_activation": _guard("dry_run_activation", _dry_run_activation),
    "dry_run_lorebook": _guard("dry_run_lorebook", _dry_run_lorebook),
    "explain_parameters": _guard("explain_parameters", _explain_parameters),
    "lint_configuration": _guard("lint_configuration", _lint_configuration),
    "report_routing": _guard("report_routing", _report_routing),
    "run_sandbox_smoke": _guard("run_sandbox_smoke", _run_sandbox_smoke),
    "probe_endpoint": _guard("probe_endpoint", _probe_endpoint),
    "probe_judge": _guard("probe_judge", _probe_judge),
    "probe_summarizer": _guard("probe_summarizer", _probe_summarizer),
    "probe_map": _guard("probe_map", _probe_map),
}

# Catalogue for `list_self_checks`. Kept beside HANDLERS so the two cannot
# silently drift apart -- a check added to one and not the other fails
# `test_assistant_selfcheck.py`.
_CATALOGUE: tuple[tuple[str, str, str], ...] = (
    ("dry_run_activation", "read", "Would a resource activate for a synthetic message, and why."),
    ("dry_run_lorebook", "read", "Which lorebook entries would fire for sample text, in order, with skip reasons."),
    ("explain_parameters", "read", "The resolved LLM parameters for one scope and which layer won each one."),
    ("lint_configuration", "read", "Dead references, dropped parameters, and a safety/sanitization re-scan."),
    ("report_routing", "read", "The failover chain per role tag and what each API key routes to."),
    ("run_sandbox_smoke", "read", "A trivial script and the user's active cantrips through the real sandbox."),
    ("probe_endpoint", "external_cost", "A live 1-token request to an endpoint plus a model-list comparison."),
    ("probe_judge", "external_cost", "A violating and a clean probe through the configured verification judge."),
    ("probe_summarizer", "external_cost", "A canned transcript through the configured summarizer."),
    ("probe_map", "external_cost", "A map run end to end with a one-line prompt."),
)


def list_self_checks() -> dict[str, Any]:
    return {
        "checks": [
            {"name": name, "risk": risk, "summary": summary} for name, risk, summary in _CATALOGUE
        ]
    }
