import json
import logging
import time
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.services.cantrip import process_cantrips
from app.services.llm_params import (
    ParamDef,
    apply_to_body,
    parse_params,
    resolve,
)
from app.services.lorebook import inject_entries
from app.services.response_pipeline import (
    _append_pending_notices as _append_pending_notices,
)
from app.services.response_pipeline import (
    _apply_post_driver_processing as _apply_post_driver_processing,
)
from app.services.response_pipeline import (
    _apply_post_navigator_processing as _apply_post_navigator_processing,
)
from app.services.response_pipeline import (
    _extract_response_memories as _extract_response_memories,
)
from app.services.response_pipeline import (
    _handle_response_memories as _handle_response_memories,
)
from app.services.response_pipeline import (
    _record_conversation_anchor as _record_conversation_anchor,
)
from app.services.response_pipeline import (
    _save_debug_exchange as _save_debug_exchange,
)
from app.services.response_pipeline import (
    _serialize_violations as _serialize_violations,
)
from app.services.response_pipeline import (
    finalize_response as finalize_response,
)
from app.services.routing import FailoverEndpoint, resolve_routing
from app.services.upstream import (
    LITELLM_NATIVE_PARAMS as LITELLM_NATIVE_PARAMS,
)
from app.services.upstream import (
    _build_upstream_url as _build_upstream_url,
)
from app.services.upstream import (
    _convert_to_sse as _convert_to_sse,
)
from app.services.upstream import (
    _do_forward as _do_forward,
)
from app.services.upstream import (
    _do_forward_litellm as _do_forward_litellm,
)
from app.services.upstream import (
    _forward_with_failover as _forward_with_failover,
)
from app.services.upstream import (
    _log_request as _log_request,
)
from app.services.upstream import (
    _log_response as _log_response,
)
from app.services.upstream import (
    _record_call as _record_call,
)
from app.services.upstream import (
    _StreamAccumulator as _StreamAccumulator,
)
from app.services.upstream import (
    _strip_think_tags as _strip_think_tags,
)
from app.services.upstream import (
    auth_headers as auth_headers,
)
from app.services.upstream import (
    extract_response_text as extract_response_text,
)
from app.services.upstream import (
    forward_llm_chain as forward_llm_chain,
)

logger = logging.getLogger(__name__)

FORWARD_HEADERS = {"content-type", "accept"}
SKIP_HEADERS = {"host", "authorization", "content-length", "transfer-encoding"}


async def _flag_injection_content(user_id: str, text: str, source: str) -> None:
    """Check DB-sourced content immediately before it's concatenated into an
    outbound LLM request (memory/lorebook/skills/samples/driver-callable
    injection points). Flag-only, never alters text — see
    app.services.sanitization.sanitize_for_injection and
    Planning/security-control-document.md for why. Opens its own short-lived
    session only when there's something to log, matching the pattern used by
    app.services.conversation.save_memories_for_chat.
    """
    if not text or not user_id:
        return
    from app.services.admin import get_url_blocklist
    from app.services.sanitization import sanitize_for_injection

    blocklist = await get_url_blocklist()
    result = sanitize_for_injection(text, blocklist)
    if result.flagged:
        from app.services.audit import log_action
        details = "; ".join(f"{f.kind}: {f.detail}" for f in result.findings)
        async with async_session() as db:
            await log_action(db, user_id, "content_injection_flag", source, "", details, "")
            await db.commit()


def _build_forward_headers(request: Request, api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in SKIP_HEADERS:
            continue
        if lower in FORWARD_HEADERS:
            headers[key] = value
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _resolve_target(
    request: Request,
) -> tuple[str, str, str | None, str, str, str, str, str, list] | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        if token.startswith("gitv_"):
            async with async_session() as db:
                result = await resolve_routing(token, db)
                if result:
                    return (
                        result.base_url, result.api_key, result.user_id,
                        result.api_base_path, result.bypass_method, result.provider,
                        result.model, result.endpoint_id, result.failover_chain,
                    )
                return None

    base_url = settings.default_endpoint_url
    api_key = settings.default_endpoint_api_key
    api_base_path = settings.default_endpoint_api_base_path
    if base_url:
        return base_url, api_key, None, api_base_path, "none", "", "", "", []
    return None


async def forward_request(
    request: Request, target: tuple | None = None
) -> JSONResponse | StreamingResponse:
    """Run the proxy pipeline for a request.

    ``target`` pre-resolves routing, which replay supplies because it holds a
    session rather than a proxy API key. Everything downstream is identical, so
    a replayed run exercises the same pipeline as live traffic.
    """
    try:
        return await _forward_request_impl(request, target)
    except Exception as exc:
        logger.exception("Unhandled error in proxy pipeline")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": f"Internal proxy error: {type(exc).__name__}: {exc}",
                    "type": "proxy_error",
                }
            },
        )


async def _forward_request_impl(
    request: Request, target: tuple | None = None
) -> JSONResponse | StreamingResponse:
    if target is None:
        target = await _resolve_target(request)

    if target is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "No endpoint configured for this request",
                    "type": "proxy_error",
                }
            },
        )

    base_url, api_key, user_id, api_base_path, endpoint_bypass, provider, endpoint_model, endpoint_id, failover_chain = target

    body = await request.body()
    path = request.url.path
    query = request.url.query
    upstream_url = _build_upstream_url(base_url, path, api_base_path)
    if query:
        upstream_url = f"{upstream_url}?{query}"

    headers = _build_forward_headers(request, api_key)

    logger.debug(
        "proxy resolved: base_url=%s path=%s api_base_path=%s upstream_url=%s api_key_present=%s provider=%s",
        base_url, path, api_base_path, upstream_url, bool(api_key), provider or "(none)",
    )

    body_json: dict[str, Any] = {}
    if body:
        try:
            body_json = json.loads(body)
        except Exception:
            pass

    model = body_json.get("model")
    if not model and endpoint_model:
        body_json["model"] = endpoint_model
        model = endpoint_model
    stream = body_json.get("stream", False)

    body_json["_gitv_provider"] = provider
    body_json["_gitv_base_url"] = base_url
    body_json["_gitv_api_key"] = api_key
    body_json["_gitv_endpoint_id"] = endpoint_id
    body_json["_gitv_failover_chain"] = failover_chain

    _log_request(request.method, upstream_url, model, stream)

    request_headers: dict[str, str] = {}
    if user_id and "messages" in body_json:
        request_headers = {k.lower(): v for k, v in request.headers.items()}
        # Package 3 (item 2): per-request id, minted once at the
        # authenticated gate so the debug run block and every cantrip
        # context see the same value.
        import uuid as _uuid

        body_json["_gitv_request_id"] = str(_uuid.uuid4())

        from app.services.metrics import init_metrics
        init_metrics(body_json, user_id)

        from app.services.command_tags import (
            extract_action_commands_from_messages,
            extract_command_tags_from_messages,
            strip_action_commands_from_messages,
            strip_command_tags_from_messages,
            strip_gitv_notices_from_messages,
        )

        # jslorebook blocks are extracted and stripped from the prompt; since
        # Phase 20 the extracted scripts execute sandboxed at their detected
        # position (embedded_jslb_scripts -> the cantrip pass machinery), with
        # context.lorebook available — the JanitorAI-compat route to dynamic
        # lorebooks.
        from app.services.jslorebook import extract_from_messages as extract_jslorebook
        from app.services.tagging import extract_all_tags_from_messages, strip_tags

        tags = extract_all_tags_from_messages(body_json.get("messages", []))
        if tags:
            logger.info("Tags detected: %s", [t["raw"] for t in tags])
            for msg in body_json["messages"]:
                content = msg.get("content", "")
                if isinstance(content, str):
                    msg["content"] = strip_tags(content)
                elif isinstance(content, list):
                    # Structured content carries text inside part dicts; a
                    # string-only rewrite leaves tags in those parts, leaking
                    # them upstream (Phase 29 fix).
                    msg["content"] = [
                        {**item, "text": strip_tags(item["text"])}
                        if isinstance(item, dict) and isinstance(item.get("text"), str)
                        else item
                        for item in content
                    ]

        jslb_scripts, cleaned_msgs = extract_jslorebook(body_json.get("messages", []))
        if jslb_scripts:
            logger.info("jslorebook: %d embedded script(s) extracted", len(jslb_scripts))
            body_json["messages"] = cleaned_msgs
            body_json["_gitv_jslb_scripts"] = jslb_scripts

        message_commands = extract_command_tags_from_messages(body_json.get("messages", []))
        if message_commands:
            logger.info("Command tags detected: %s", [
                f"{c.command}:{c.setting}" + (":persist" if c.persist else "") for c in message_commands
            ])
            body_json["messages"] = strip_command_tags_from_messages(body_json["messages"])

        from app.services.debug import debug_capture, init_debug, is_debug_mode
        debug_on = await is_debug_mode(user_id)
        if debug_on:
            init_debug(body_json, tags)
            # Wall-clock start. summarize_run subtracts the summed upstream
            # latency from this to get the pipeline's own overhead, which is the
            # one cost figure nothing else in the product surfaces.
            body_json["_gitv_request_started"] = time.monotonic()

        # Phase 29 action commands (<LEXICON:name>, <CANTRIP:description>) are
        # parsed here and acted on later in the pipeline; their free-text
        # argument never reaches the upstream model. GitV OOC notices from a
        # previous reply are stripped on every request so they reach the
        # player once and never persist in the resent history.
        action_commands = extract_action_commands_from_messages(body_json.get("messages", []))
        if action_commands:
            logger.info("Action commands detected: %s", [
                f"{a.command}: {a.text[:60]}" for a in action_commands
            ])
            body_json["messages"] = strip_action_commands_from_messages(body_json["messages"])
            body_json["_gitv_action_commands"] = [
                {"command": a.command, "text": a.text} for a in action_commands
            ]
        body_json["messages"] = strip_gitv_notices_from_messages(body_json["messages"])

        # Debug init runs before this stage so its original-messages snapshot
        # predates the strip; the capture then shows a real before/after delta
        # for the removed commands and notices instead of a no-op entry.
        if debug_on:
            debug_capture(
                body_json, "action_commands", "Action Commands",
                detail=(
                    f"{len(action_commands)} action command(s) extracted; GitV notices stripped"
                    if action_commands
                    else "No action commands; GitV notices stripped"
                ),
                metadata={
                    "commands": [
                        {"command": a["command"], "text": a["text"][:200]}
                        for a in body_json.get("_gitv_action_commands", [])
                    ]
                },
            )

        body_json["_gitv_tags"] = tags

        from app.services.group_resolver import resolve_group_tags
        async with async_session() as group_db:
            tags, activated_group_names, group_member_ids = await resolve_group_tags(
                group_db, user_id, tags
            )
        if activated_group_names:
            body_json["_gitv_tags"] = tags
            body_json["_gitv_activated_groups"] = activated_group_names
            if group_member_ids:
                # Seventh activation source (rule 19): ids of members of
                # activated groups that opted into direct activation. The
                # cantrip/lorebook decision sites OR these in.
                body_json["_gitv_group_member_ids"] = group_member_ids
            logger.info("Tag groups activated: %s", activated_group_names)
            if debug_on:
                debug_capture(body_json, "tag_group_resolution", "Tag Group Resolution",
                    detail=f"Activated groups: {', '.join(activated_group_names)}",
                    metadata={
                        "groups": activated_group_names,
                        "expanded_tag_count": len(tags),
                        "direct_member_count": sum(len(v) for v in group_member_ids.values()),
                    })

        from app.services.conversation import (
            load_memories_for_chat,
            resolve_conversation,
        )
        from app.services.memory import build_memory_context_block as _build_mem_block

        messages_for_conv = body_json.get("messages", [])

        # Conversation-hash anchor, captured BEFORE any prompt work (lorebook/
        # skill injection, cantrips, writing samples) touches the message list
        # -- exactly the list resolve_conversation hashed a moment ago and will
        # hash from the client's raw history on the next request. The old
        # capture point (post-injection, later in this function) recorded
        # messages the client never stores, so any injection landing inside
        # the 4-message hash window -- a before_last_message entry, writing
        # samples -- made every following request resolve as a brand-new
        # conversation, silently losing chat_data, memories and summaries.
        # Deep copy: stages may mutate message dicts in place, and this anchor
        # must outlive all of them untouched.
        body_json["_gitv_messages_for_hash"] = json.loads(
            json.dumps(messages_for_conv, default=str)
        )

        internal_chat_id, is_new_conv = await resolve_conversation(messages_for_conv, user_id)

        if body_json.get("_gitv_sandbox"):
            # A sandbox owns a forked conversation: its memories, chat data and
            # summary were copied at fork time and are keyed to this id. Writes
            # are *allowed* and land in the fork, so a second run sees what the
            # first one wrote. The conversation it was forked from is never
            # resolved and never touched.
            internal_chat_id = body_json.get("_gitv_sandbox_chat_id", "")
            logger.info("Sandbox run %s in chat %s",
                        body_json.get("_gitv_sandbox_id", "")[:8], internal_chat_id[:24])

        elif body_json.get("_gitv_replay"):
            # A replay resends messages the real conversation already contains,
            # so resolve_conversation matches that conversation. Left alone it
            # would write a second summary over it, re-extract its memories, and
            # advance its rolling hash -- corrupting the very state the user is
            # trying to hold still between A and B. A synthetic id keeps the
            # read paths identical and the writes off to one side.
            internal_chat_id = f"replay:{body_json.get('_gitv_replay_of', '')}"
            logger.info("Replay of run %s: isolated as chat %s",
                        body_json.get("_gitv_replay_of", "")[:8], internal_chat_id[:20])

        from app.services.command_tags import resolve_command_overrides
        command_overrides = await resolve_command_overrides(
            user_id, internal_chat_id, message_commands
        )
        body_json["_gitv_command_overrides"] = command_overrides.to_dict()

        from app.services.budget import estimate_messages_tokens
        from app.services.budget import estimate_tokens as _est_tokens
        from app.services.metrics import record_prompt_object, set_metrics_chat
        set_metrics_chat(body_json, internal_chat_id)
        # Every cantrip position reads the chat id from here (via
        # extract_context_params) when the client sends no conversation
        # header -- previously only the map branch set it, so post-driver
        # store writes from header-less clients silently dropped.
        body_json["_gitv_chat_id"] = internal_chat_id

        # Monotonic turn index (0.35.0): message_count stalls once the client
        # trims history, so stateful cantrips get a counter that only goes
        # up. Keyed by the Phase-29 identity (client header wins, internal
        # id fallback). Replays are excluded -- a replay must hold the
        # conversation still between A and B, not advance it. A sandbox bumps
        # its own forked id, like every other sandbox write. A counter
        # failure must never fail the request.
        from app.services.lexicon import resolve_action_identity

        turn_conversation_id, _turn_id_src = resolve_action_identity(
            request_headers or {}, internal_chat_id
        )
        if turn_conversation_id and not body_json.get("_gitv_replay"):
            try:
                from app.services.conversation import (
                    bump_turn_counter,
                    note_request_shape,
                )

                body_json["_gitv_turn_index"] = await bump_turn_counter(
                    user_id, turn_conversation_id
                )
                body_json["_gitv_is_regenerate"] = await note_request_shape(
                    user_id, turn_conversation_id, body_json.get("messages", [])
                )
            except Exception:
                logger.exception("Turn counter bump failed; cantrips see turn_index 0")
                body_json["_gitv_turn_index"] = 0

        # Phase 29b: <CANTRIP:description> queues a draft for the Assistant
        # pane (or auto-installs when both gates are on). Handled before the
        # map branch so drafts queue on map requests too; the resulting OOC
        # notice rides the response out at the return points.
        if user_id and body_json.get("_gitv_action_commands"):
            from app.services.lexicon import handle_cantrip_request
            for action in body_json["_gitv_action_commands"]:
                if action["command"] != "CANTRIP":
                    continue
                try:
                    await handle_cantrip_request(
                        body_json, user_id, request_headers, internal_chat_id, action["text"]
                    )
                except Exception:
                    logger.exception("Cantrip draft handling failed; request continues")

        cmd_overrides_mem = body_json.get("_gitv_command_overrides", {})
        memory_disabled = cmd_overrides_mem.get("memory") is False
        memories = {} if memory_disabled else await load_memories_for_chat(internal_chat_id, user_id)
        if memories:
            memory_block = _build_mem_block(memories)
            if memory_block:
                await _flag_injection_content(user_id, memory_block, "memory_injection")
                record_prompt_object(
                    body_json, "memory_injection", "memory",
                    0, _est_tokens(memory_block),
                )
                msgs = body_json["messages"]
                system_idx = None
                for i, msg in enumerate(msgs):
                    if msg.get("role") == "system":
                        system_idx = i
                        break
                if system_idx is not None:
                    msgs[system_idx] = {
                        **msgs[system_idx],
                        "content": msgs[system_idx]["content"] + "\n\n" + memory_block,
                    }
                else:
                    msgs.insert(0, {"role": "system", "content": memory_block})
                logger.info("Memory injection: %d keys for chat %s", len(memories), internal_chat_id[:12])
                if debug_on:
                    debug_capture(body_json, "memory_injection", "Memory Injection",
                        detail=f"Injected {len(memories)} memories for chat {internal_chat_id[:12]}",
                        setting="memory_enabled", setting_value=not memory_disabled,
                        metadata={"memory_keys": list(memories.keys())})
        elif debug_on:
            debug_capture(body_json, "memory_injection", "Memory Injection",
                detail="No memories found" if not memory_disabled else "Disabled by command override",
                setting="memory_enabled", setting_value=not memory_disabled)

        if user_id and command_overrides.to_dict().get("summary") is not False:
            from app.services.scenario_summarizer import maybe_summarize_scenario
            pre_summary_msgs = json.dumps(body_json.get("messages", []), default=str)
            pre_scn_tokens = estimate_messages_tokens(body_json.get("messages", []))
            body_json = await maybe_summarize_scenario(body_json, user_id, "pre")
            record_prompt_object(
                body_json, "summary_scenario", "pre",
                pre_scn_tokens, estimate_messages_tokens(body_json.get("messages", [])),
            )
            if debug_on:
                did_change = json.dumps(body_json.get("messages", []), default=str) != pre_summary_msgs
                debug_capture(body_json, "scenario_summarization_pre", "Scenario Summarization (Pre)",
                    detail="Summarized" if did_change else "No summarization triggered",
                    setting="scenario_summary_enabled", setting_value=True,
                    metadata={"changed": did_change})

        # Map candidacy is resolved here (hoisted from the cantrip-pass site
        # below) because the manual pipeline order needs it before deciding
        # whether to replace the lorebook-injection + cantrip-pass window.
        # Nothing between here and the pass mutates tags or maps, so the
        # hoist is behavior-neutral for the default path.
        from app.services.map_pipeline import (
            resolve_map,
            run_map_pipeline,
            stage_bound_cantrip_ids,
        )

        cmd_overrides_map = body_json.get("_gitv_command_overrides", {})
        map_candidate = None
        if cmd_overrides_map.get("map") is not False:
            map_candidate = await resolve_map(user_id, tags)

        # Pipeline ordering (0.41.0): manual interleaved order replaces the
        # [lorebook injection → global cantrip pass] window -- non-map
        # requests only (map stages keep their own attachment machinery).
        manual_order_plan = None
        if map_candidate is None and user_id:
            from app.services.pipeline_order import load_plan

            _order_plan = await load_plan(user_id)
            if _order_plan["enabled"]:
                manual_order_plan = _order_plan

        # 0.42.0 (B9b): the budget is computed BEFORE anything is injected.
        # It used to run after lorebook and skills injection, so injected
        # text was counted as conversation and the budget never constrained
        # it. Now current_tokens measures the raw client prompt, and each
        # book's share caps its own entry matching below.
        from app.services.budget import prepare_budget
        await prepare_budget(
            body_json, user_id, tags, endpoint_id=body_json.get("_gitv_endpoint_id", "")
        )
        if debug_on:
            debug_capture(body_json, "budget_preparation", "Budget Preparation",
                detail="Context budget allocated",
                metadata={"budget": body_json.get("_gitv_budget", {})})

        if manual_order_plan is not None:
            if debug_on:
                debug_capture(body_json, "lorebook_injection", "Lorebook Injection",
                    detail="Handled per-slot by the manual pipeline order",
                    metadata={"tags": [t.get("raw", "") for t in (tags or [])]})
        else:
            body_json = await _apply_lorebook_injection(
                body_json, user_id, tags, request_headers, internal_chat_id
            )
            if debug_on:
                entries = body_json.pop("_gitv_matched_entries", [])
                debug_capture(body_json, "lorebook_injection", "Lorebook Injection",
                    detail=(
                        f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} matched"
                        if entries else "No entries matched"
                    ),
                    metadata={
                        "tags": [t.get("raw", "") for t in (tags or [])],
                        "entries": entries,
                        "tokens": sum(e.get("tokens", 0) for e in entries),
                    })
            else:
                body_json.pop("_gitv_matched_entries", None)

        if user_id and endpoint_id:
            from app.services.skills import (
                inject_skills,
                load_skills_for_endpoint,
                take_loaded_detail,
            )
            async with async_session() as skills_db:
                skill_contents, sample_contents = await load_skills_for_endpoint(
                    endpoint_id, user_id, skills_db
                )
            loaded_detail = take_loaded_detail(endpoint_id)
            # Per-item prompt metrics for skills and samples. Emitted at load
            # time because the per-item token estimates live here; both are
            # always injected afterwards (skills immediately, samples on either
            # the map branch or the writing-samples stage below).
            from app.services.metrics import record_prompt_object
            for d in loaded_detail:
                record_prompt_object(body_json, d["type"], d["id"], 0, d["tokens"])
            if skill_contents:
                await _flag_injection_content(user_id, "\n".join(skill_contents), "skills_injection")
                body_json["messages"] = inject_skills(body_json["messages"], skill_contents)
            if sample_contents:
                body_json["_gitv_sample_contents"] = sample_contents
            if debug_on:
                debug_capture(body_json, "skills_injection", "Skills Injection",
                    detail=f"{len(skill_contents)} skill(s), {len(sample_contents)} sample(s) loaded",
                    setting="endpoint_skills", setting_value=len(skill_contents) > 0,
                    metadata={
                        "skill_count": len(skill_contents),
                        "sample_count": len(sample_contents),
                        # Bare counts cannot answer "which skill did this run
                        # use that the other did not", and the per-item tokens
                        # make the injection-cost breakdown attributable.
                        "skills": [d for d in loaded_detail if d["type"] == "skill"],
                        "samples": [d for d in loaded_detail if d["type"] == "sample"],
                        "tokens": sum(d["tokens"] for d in loaded_detail),
                    })
        elif debug_on:
            debug_capture(body_json, "skills_injection", "Skills Injection",
                detail="No endpoint configured", setting="endpoint_skills", setting_value=False)

        # Map candidacy was resolved above the lorebook-injection site (the
        # manual pipeline order needs it early; the hoist is
        # behavior-neutral). The global cantrip pass runs before map
        # selection is finalized so a cantrip can nominate a map
        # (context.maps.activate). It excludes the candidate map's
        # stage-bound cantrips exactly as before; those run on their own
        # stage inside run_map_pipeline. <jslorebook> scripts detected as
        # pre_driver run here too (Phase 20) — always, since they arrived in
        # the chat; map stages inherit their effects through the prompt
        # rather than re-running them.
        from app.services.cantrip import embedded_jslb_scripts

        if manual_order_plan is not None:
            from app.services.pipeline_order import run_pre_forward_ordered

            await run_pre_forward_ordered(
                manual_order_plan, body_json, user_id, request_headers, tags,
                internal_chat_id=internal_chat_id,
                embedded_scripts=embedded_jslb_scripts(body_json, "pre_driver"),
            )
        else:
            body_json = await process_cantrips(
                body_json, user_id, request_headers, tags,
                internal_chat_id=internal_chat_id,
                exclude_ids=stage_bound_cantrip_ids(map_candidate) if map_candidate else None,
                embedded_scripts=embedded_jslb_scripts(body_json, "pre_driver"),
            )
            # Pipeline ordering (0.41.0), default mode: strips collected by
            # the pass apply here -- before deferred lorebook entries are
            # matched and injected, so inject-after-cantrips books see
            # cleaned text too. Never touches the conversation-hash anchor.
            pending_strips = body_json.pop("_gitv_prompt_strips", None)
            if pending_strips:
                from app.services.cantrip import apply_prompt_strips

                apply_prompt_strips(body_json, pending_strips)

        map_active = map_candidate
        nominations = body_json.pop("_gitv_map_nominations", [])
        if nominations and cmd_overrides_map.get("map") is not False:
            # A nomination competes in selection (rule 19: maps pick exactly
            # one winner), it does not union-run. <MAP:off> suppresses it like
            # any other map activation.
            nominated_tags = list(tags or []) + [
                {"raw": f"<#map-{t}#>", "type": "map", "name": t} for t in nominations
            ]
            resolved = await resolve_map(user_id, nominated_tags)
            if resolved is not None and (
                map_candidate is None or resolved.id != map_candidate.id
            ):
                logger.info(
                    "Map nomination(s) %s selected map '%s'",
                    ", ".join(nominations), resolved.name,
                )
                ran_globally = {
                    row.get("object_id")
                    for row in body_json.get("_gitv_metrics", {}).get("rows_prompt", [])
                    if row.get("object_type") == "cantrip"
                }
                overlap = stage_bound_cantrip_ids(resolved) & ran_globally
                if overlap:
                    logger.warning(
                        "Map '%s' was nominated after the global cantrip pass, and "
                        "%d cantrip(s) bound to its stages already ran globally this "
                        "request -- they will run again on their stage. Avoid "
                        "nominating maps whose stages bind globally-active cantrips.",
                        resolved.name, len(overlap),
                    )
                map_active = resolved
        if debug_on:
            _capture_cantrip_pass(
                body_json, "pre_driver",
                "Cantrips (Global Pass, Pre-Map)" if map_active else "Cantrips (Pre-Driver)",
            )

        # 0.35.0: lorebooks marked inject-after-cantrips are applied here,
        # after the cantrip pass (cantrips could not read their entries) and
        # before the map branch and conversation summarization, so Full-mode
        # stage snapshots and the summarizer both still see them.
        deferred_pending = body_json.pop("_gitv_deferred_lorebook_pending", False)
        deferred_entries = body_json.pop("_gitv_deferred_lorebook_entries", None)
        if deferred_entries:
            await _flag_injection_content(
                user_id, "\n".join(e.content for e in deferred_entries), "lorebook_injection"
            )
            body_json["messages"] = inject_entries(
                body_json["messages"], deferred_entries
            )
            logger.info(
                "Lorebook injection (post-cantrip): %d entries matched for user %s",
                len(deferred_entries), user_id,
            )
        if debug_on and (deferred_pending or deferred_entries):
            debug_capture(
                body_json, "lorebook_injection_deferred", "Lorebook Injection (Post-Cantrip)",
                detail=(
                    f"{len(deferred_entries or [])} deferred entr"
                    f"{'y' if len(deferred_entries or []) == 1 else 'ies'} injected after cantrips"
                    if deferred_entries
                    else "Deferred books active, no entries matched"
                ),
                metadata={
                    "entries": [
                        {
                            "lorebook_name": e.lorebook_name,
                            "entry_name": e.name,
                            "position": e.position,
                        }
                        for e in (deferred_entries or [])
                    ]
                },
            )

        # Phase 20 (0.37.0): entries written by this turn's cantrips inject
        # this turn. Ops applied during the cantrip pass left their delta on
        # the body; this is the non-map injection point (map stages pop the
        # delta per stage inside run_map_pipeline, before their own forward).
        dynamic_delta = body_json.pop("_gitv_dynamic_lorebook_delta", None)
        served_entry_ids = body_json.pop("_gitv_main_matched_entry_ids", None)
        dynamic_delta_skipped = 0
        if dynamic_delta and served_entry_ids:
            # 0.42.0 (B7.4): drop delta rows for entries the main pass
            # already injected this turn (double-injection fix).
            kept = [
                e for e in dynamic_delta
                if not (e.entry_id and e.entry_id in served_entry_ids)
            ]
            dynamic_delta_skipped = len(dynamic_delta) - len(kept)
            if dynamic_delta_skipped:
                logger.info(
                    "Dynamic lorebook same-turn delta: skipped %d entr%s "
                    "already injected by the main pass (B7.4)",
                    dynamic_delta_skipped,
                    "y" if dynamic_delta_skipped == 1 else "ies",
                )
            dynamic_delta = kept
        if dynamic_delta:
            from app.services.metrics import record_prompt_object

            await _flag_injection_content(
                user_id, "\n".join(e.content for e in dynamic_delta), "lorebook_injection"
            )
            body_json["messages"] = inject_entries(body_json["messages"], dynamic_delta)
            logger.info(
                "Dynamic lorebook injection (same-turn): %d entries for user %s",
                len(dynamic_delta), user_id,
            )
            for e in dynamic_delta:
                record_prompt_object(
                    body_json, "lorebook_entry", e.entry_id,
                    0, max(1, len(e.content) // 4), parent_id=e.lorebook_id,
                )
        # Rule 16: the stage appears even when every row was skipped, so a
        # user can SEE that the delta was considered and why nothing landed.
        if debug_on and (dynamic_delta or dynamic_delta_skipped):
            debug_capture(
                body_json, "dynamic_lorebook_injection", "Dynamic Lorebook Injection (Same-Turn)",
                detail=(
                    f"{len(dynamic_delta)} entr"
                    f"{'y' if len(dynamic_delta) == 1 else 'ies'} written by cantrips injected this turn"
                    + (
                        f"; {dynamic_delta_skipped} skipped as already injected"
                        " by the main pass (B7.4)"
                        if dynamic_delta_skipped else ""
                    )
                ),
                metadata={
                    "entries": [
                        {
                            "lorebook_id": e.lorebook_id,
                            "lorebook_name": e.lorebook_name,
                            "entry_id": e.entry_id,
                            "entry_name": e.name,
                            "position": e.position,
                        }
                        for e in dynamic_delta
                    ],
                    **({"skipped_already_served": dynamic_delta_skipped}
                       if dynamic_delta_skipped else {}),
                },
            )

        if map_active:
            import httpx as _httpx
            map_timeout = _httpx.Timeout(settings.request_timeout, connect=10.0)
            logger.info("Map '%s' active — routing to map pipeline", map_active.name)

            # Writing samples are injected below on the non-map path, after this
            # branch has already returned. Do it here too, or a sample attached to
            # the endpoint silently never reaches any stage.
            map_samples = body_json.pop("_gitv_sample_contents", None)
            if map_samples:
                from app.services.skills import inject_samples
                await _flag_injection_content(
                    user_id, "\n".join(map_samples), "writing_samples"
                )
                body_json["messages"] = inject_samples(body_json["messages"], map_samples)

            # Macros expand on the map path too: without this, a lorebook
            # entry carrying {{user}} expands on plain requests but ships
            # literal to every stage (the map branch returns before the
            # non-map expansion site below). Runs after injections, same
            # as the non-map path, and the conversation-hash anchor was
            # captured pre-injection, so continuity is unaffected.
            from app.services.macros import expand_macros_in_messages
            expand_macros_in_messages(body_json, request_headers)

            response_data = await run_map_pipeline(body_json, user_id, request_headers, map_active, map_timeout)
            map_status = body_json.pop("_gitv_map_status_code", 200)

            has_chat = bool(body_json.get("_gitv_chat_id"))
            response_data = await finalize_response(
                response_data,
                map_status,
                body_json,
                user_id,
                request_headers,
                vresult=body_json.pop("_gitv_map_vresult", None),
                run_post_driver=False,
                run_verification=False,
                run_response_capture=has_chat,
                run_post_navigator=False,
                run_memories=has_chat,
                append_notices=True,
                run_bypass_decode=False,
                save_debug=True,
                flush_metrics=True,
            )

            if map_status == 200 and stream:
                ux_settings = await _load_ux_settings(user_id)
                return _convert_to_sse(
                    JSONResponse(status_code=200, content=response_data),
                    model or body_json.get("model", ""),
                    gitv_status=ux_settings.get("gitv_status", False),
                    simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
                    preserve_thinking=ux_settings.get("preserve_thinking", True),
                )

            return JSONResponse(status_code=map_status, content=response_data)

        # The global pre-driver pass already ran above (before map selection,
        # so cantrip nominations could be counted). Anything between here and
        # the forward is post-cantrip prompt work. The conversation-hash
        # anchor was captured pre-injection at resolve_conversation time;
        # re-capturing here would reintroduce the 0.33.0 continuity bug
        # (injections inside the hash window orphaning every conversation).

        from app.services.summarization import maybe_summarize
        if body_json.get("_gitv_command_overrides", {}).get("summary") is not False:
            pre_summ_msgs = json.dumps(body_json.get("messages", []), default=str)
            pre_summ_tokens = estimate_messages_tokens(body_json.get("messages", []))
            body_json = await maybe_summarize(body_json, user_id, internal_chat_id, tags)
            record_prompt_object(
                body_json, "summary_conversation", "conversation",
                pre_summ_tokens, estimate_messages_tokens(body_json.get("messages", [])),
            )
            if debug_on:
                summ_changed = json.dumps(body_json.get("messages", []), default=str) != pre_summ_msgs
                debug_capture(body_json, "conversation_summarization", "Conversation Summarization",
                    detail="Summarized" if summ_changed else "Not triggered",
                    setting="summarization_enabled", setting_value=True,
                    metadata={"changed": summ_changed})

        if user_id and body_json.get("_gitv_command_overrides", {}).get("summary") is not False:
            from app.services.scenario_summarizer import maybe_summarize_scenario
            pre_post_scn = json.dumps(body_json.get("messages", []), default=str)
            pre_post_scn_tokens = estimate_messages_tokens(body_json.get("messages", []))
            body_json = await maybe_summarize_scenario(body_json, user_id, "post")
            record_prompt_object(
                body_json, "summary_scenario", "post",
                pre_post_scn_tokens, estimate_messages_tokens(body_json.get("messages", [])),
            )
            if debug_on:
                post_scn_changed = json.dumps(body_json.get("messages", []), default=str) != pre_post_scn
                debug_capture(body_json, "scenario_summarization_post", "Scenario Summarization (Post)",
                    detail="Summarized" if post_scn_changed else "No summarization triggered",
                    setting="scenario_summary_enabled", setting_value=True,
                    metadata={"changed": post_scn_changed})

        # Phase 29a: <LEXICON:name> compiles a lorebook entry for the named
        # thing via one side LLM call, after summarization so the extraction
        # sees the compressed history + summary and every injection. The entry
        # takes effect from the next request on. Non-map path only (v1).
        if user_id and body_json.get("_gitv_action_commands"):
            from app.services.lexicon import run_lexicon_compile
            for action in body_json["_gitv_action_commands"]:
                if action["command"] != "LEXICON":
                    continue
                try:
                    await run_lexicon_compile(
                        body_json, user_id, request_headers, internal_chat_id, action["text"]
                    )
                except Exception:
                    logger.exception("Lexicon compile failed; request continues")

        sample_contents = body_json.pop("_gitv_sample_contents", None)
        if sample_contents:
            from app.services.skills import inject_samples
            await _flag_injection_content(user_id, "\n".join(sample_contents), "writing_samples")
            body_json["messages"] = inject_samples(body_json["messages"], sample_contents)
            if debug_on:
                debug_capture(body_json, "writing_samples", "Writing Samples Injection",
                    detail=f"{len(sample_contents)} sample(s) injected before last message",
                    setting="endpoint_samples", setting_value=True,
                    metadata={"sample_count": len(sample_contents)})
        elif debug_on:
            debug_capture(body_json, "writing_samples", "Writing Samples Injection",
                detail="No writing samples configured", setting="endpoint_samples", setting_value=False)

        from app.services.driver_callable import (
            build_tool_notification,
            load_driver_callable_config,
        )
        async with async_session() as db:
            dc_config = await load_driver_callable_config(db, user_id, tags)

        cmd_overrides_dc = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides_dc.get("driver_callable") is False:
            dc_config.enabled = False
        elif cmd_overrides_dc.get("driver_callable") is True and dc_config.tools:
            dc_config.enabled = True

        if dc_config.enabled:
            from app.services.admin import get_caps
            caps = await get_caps()
            dc_config.turns = min(dc_config.turns, caps["max_driver_callable_turns"])
            notification = build_tool_notification(dc_config.tools, dc_config.turns)
            if notification:
                await _flag_injection_content(user_id, notification, "driver_callable")
                body_json["_gitv_driver_callable"] = True
                body_json["_gitv_driver_callable_turns"] = dc_config.turns
                body_json["messages"] = inject_tool_notification_safe(
                    body_json.get("messages", []), notification
                )
                if debug_on:
                    debug_capture(body_json, "driver_callable", "Driver-Callable Tools",
                        detail=f"{len(dc_config.tools)} tool(s), {dc_config.turns} turn(s)",
                        setting="driver_callable_enabled", setting_value=True,
                        metadata={"tools": [t.name for t in dc_config.tools], "turns": dc_config.turns})
        elif debug_on:
            debug_capture(body_json, "driver_callable", "Driver-Callable Tools",
                detail="Disabled" if not dc_config.tools else "No tools configured",
                setting="driver_callable_enabled", setting_value=False)

        from app.services.bypass import apply_bypass_encode
        from app.services.macros import expand_macros_in_messages
        from app.services.prefill import normalize_prefill

        expand_macros_in_messages(body_json, request_headers)

        async with async_session() as db:
            from app.models.user_settings import UserSettings
            us_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
            us = us_result.scalar_one_or_none()
            prefill_on = us.prefill_enabled if us else False

        bypass_method = endpoint_bypass

        if prefill_on:
            body_json = normalize_prefill(body_json, base_url, enabled=True)
            if debug_on:
                debug_capture(body_json, "prefill", "Prefill Normalization",
                    detail="Prefill applied",
                    setting="prefill_enabled", setting_value=True)
        elif debug_on:
            debug_capture(body_json, "prefill", "Prefill Normalization",
                detail="Disabled", setting="prefill_enabled", setting_value=False)

        if bypass_method != "none":
            body_json = apply_bypass_encode(body_json, bypass_method)
            body_json["_gitv_bypass_method"] = bypass_method
            if debug_on:
                debug_capture(body_json, "bypass_encoding", "Bypass Encoding",
                    detail=f"Method: {bypass_method}",
                    setting="bypass_method", setting_value=bypass_method)
        elif debug_on:
            debug_capture(body_json, "bypass_encoding", "Bypass Encoding",
                detail="No bypass", setting="bypass_method", setting_value="none")

        # Parameters are applied last, immediately before serialization, so a
        # configured value cannot be undone by a later pipeline stage.
        user_param_layer = await _load_user_param_layer(user_id, "parameters_json")
        primary = failover_chain[0] if failover_chain else None
        body_json = _apply_llm_parameters(
            body_json, primary, user_param_layer, body_json.get("model", "")
        )
        if debug_on:
            resolved_values = body_json.get("_gitv_llm_params", {})
            sources = body_json.get("_gitv_llm_param_sources", {})
            debug_capture(body_json, "llm_parameters", "LLM Parameters",
                detail=(
                    ", ".join(f"{k}={v} (from {sources.get(k, '?')})" for k, v in resolved_values.items())
                    if resolved_values else "No parameters configured"
                ),
                metadata={"resolved": resolved_values, "sources": sources})

        if debug_on:
            debug_capture(body_json, "final_messages", "Final Messages (Pre-Forward)",
                detail="Messages ready to send to upstream LLM")

        body = json.dumps({k: v for k, v in body_json.items() if not k.startswith("_gitv")}).encode()

    timeout = httpx.Timeout(settings.request_timeout, connect=10.0)

    if stream and user_id:
        from app.services.verification import is_verification_enabled
        try:
            verification_on = await is_verification_enabled(user_id, body_json.get("_gitv_tags"))
        except Exception:
            verification_on = False

        cmd_overrides = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides.get("verify") is False:
            verification_on = False
        elif cmd_overrides.get("verify") is True:
            verification_on = True

        dc_active = body_json.get("_gitv_driver_callable", False)

        if verification_on or dc_active:
            ux_settings = await _load_ux_settings(user_id)
            body_json_verified = dict(body_json)
            body_json_verified["stream"] = False
            body_verified = json.dumps(
                {k: v for k, v in body_json_verified.items() if not k.startswith("_gitv")}
            ).encode()
            if dc_active:
                logger.info("Driver-callable active: converting streaming to non-streaming for tool loop")
            elif verification_on:
                logger.info("Verification enabled: converting streaming request to non-streaming for verification")
            response = await _forward_non_streaming_verified(
                request.method, upstream_url, headers, body_verified, body_json,
                timeout, user_id, request_headers, path,
            )
            if response.status_code == 200 and stream:
                response = _convert_to_sse(
                    response, model or body_json.get("model", ""),
                    gitv_status=ux_settings.get("gitv_status", False),
                    simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
                    preserve_thinking=ux_settings.get("preserve_thinking", True),
                )
            return response

    if stream:
        return await _forward_streaming(
            request.method, upstream_url, headers, body, timeout, user_id, body_json, path,
            request_headers=request_headers,
        )

    return await _forward_non_streaming_verified(
        request.method, upstream_url, headers, body, body_json, timeout, user_id, request_headers, path
    )


async def _load_ux_settings(user_id: str) -> dict[str, Any]:
    from app.models.user_settings import UserSettings
    try:
        async with async_session() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            s = result.scalar_one_or_none()
            if s:
                return {
                    "gitv_status": s.gitv_status,
                    "simulated_streaming_speed": s.simulated_streaming_speed,
                    "preserve_thinking": s.preserve_thinking,
                    "streaming_post_processing": s.streaming_post_processing_enabled,
                }
    except Exception:
        pass
    return {
        "gitv_status": False,
        "simulated_streaming_speed": 0,
        "preserve_thinking": True,
        # Fail toward enforcement: the stream is buffered either way, so a
        # settings read that cannot happen costs nothing by running the checks.
        "streaming_post_processing": True,
    }


async def _load_user_param_layer(user_id: str, field_name: str) -> list[ParamDef]:
    """Load one of the three user-settings parameter layers.

    Broader than the endpoint and narrower than the client payload. Failure to
    load is not fatal -- an unreachable settings row means one fewer layer, not
    a failed request.
    """
    from app.models.user_settings import UserSettings
    if not user_id:
        return []
    try:
        async with async_session() as db:
            result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            s = result.scalar_one_or_none()
            if s:
                return parse_params(getattr(s, field_name, ""), f"user settings ({field_name})")
    except Exception:
        logger.warning("Could not load %s for user %s", field_name, user_id[:8], exc_info=True)
    return []


def _apply_llm_parameters(
    body_json: dict[str, Any],
    candidate: FailoverEndpoint | None,
    user_layer: list[ParamDef],
    model: str,
) -> dict[str, Any]:
    """Resolve the driver's parameter layers and write them onto the body.

    Layer order is the whole contract: user settings, then endpoint, then the
    curated model. The client payload is not a layer -- it is the body being
    written over, so a key nobody configured survives untouched and a key that
    is configured is overwritten.

    Returns the merged body. The resolved set is stashed as `_gitv_llm_params`
    so the LiteLLM forwarder can pass keys it does not model through extra_body;
    it is stripped with every other `_gitv` key before serialization.
    """
    user_resolved = resolve([("user settings", user_layer)])
    layers: list[tuple[str, list[ParamDef]]] = [("user settings", user_layer)]
    if candidate is not None:
        label = f"endpoint '{candidate.endpoint_name}'" if candidate.endpoint_name else "endpoint"
        layers.append((label, candidate.parameters))
        layers.append((f"model '{model}'", candidate.params_for_model(model)))

    resolved = resolve(layers)
    out = apply_to_body(body_json, resolved)
    out["_gitv_user_llm_params"] = dict(user_resolved.values)
    out["_gitv_llm_params"] = dict(resolved.values)
    out["_gitv_llm_param_sources"] = dict(resolved.sources)
    return out


async def _apply_lorebook_injection(
    body_json: dict[str, Any],
    user_id: str,
    tags: list | None = None,
    request_headers: dict[str, str] | None = None,
    internal_chat_id: str = "",
) -> dict[str, Any]:
    try:
        # The signature allows tags=None (meaning "no tags"); normalize so
        # downstream activation never iterates None. The old `if lb.tag and
        # tags:` guard hid this -- with the guard gone, None would raise
        # inside this try and silently disable lorebook injection for
        # whichever caller relied on the default.
        tags = tags or []

        async with async_session() as db:
            # Shared loader (also feeds the manual-order orchestrator), so
            # both orderings activate lorebooks through one code path.
            from app.services.group_resolver import group_direct_members
            from app.services.lorebook import load_active_lorebooks

            active_lorebooks = await load_active_lorebooks(
                db,
                user_id,
                tags,
                request_headers,
                internal_chat_id,
                direct_member_ids=group_direct_members(body_json),
            )

            if not any(lb.entries for lb in active_lorebooks):
                return body_json

            # Per-book matching (0.35.0): scan depth is a book property, and
            # books marked inject-after-cantrips must be kept out of this
            # pass's injection. Both buckets still sort by insertion_order
            # (stable) so cross-book ordering matches the old flattened
            # single-pass behavior exactly.
            from app.services.lorebook import book_entries_to_dicts, match_book_entries

            messages = body_json.get("messages", [])
            pre_matched: list = []
            deferred_matched: list = []
            deferred_pending = False
            for lb in active_lorebooks:
                book_entries = book_entries_to_dicts(lb.entries, book=lb)
                book_matched = match_book_entries(
                    messages, book_entries, lb.scan_depth,
                    word_boundary=lb.word_boundary_keys,
                    # 0.42.0 (B9b): the book's budget share finally caps its
                    # injection (the cap existed but no caller passed it).
                    # Tokens -> chars via the canonical len//4 estimator.
                    total_budget=(
                        (
                            (body_json.get("_gitv_budget_allocations") or {})
                            .get("allocations", {})
                            .get(lb.id, {})
                            .get("share", 0)
                            * 4
                        )
                        if (lb.budget_weight > 0 or (getattr(lb, "reserved_tokens", 0) or 0) > 0)
                        and body_json.get("_gitv_budget_allocations")
                        else 0
                    ),
                )
                if lb.inject_after_cantrips:
                    deferred_pending = True
                    deferred_matched.extend(book_matched)
                else:
                    pre_matched.extend(book_matched)

            pre_matched.sort(key=lambda m: m.insertion_order)
            deferred_matched.sort(key=lambda m: m.insertion_order)

            if pre_matched:
                await _flag_injection_content(
                    user_id, "\n".join(e.content for e in pre_matched), "lorebook_injection"
                )
                body_json["messages"] = inject_entries(messages, pre_matched)
                logger.info(
                    "Lorebook injection: %d entries matched for user %s", len(pre_matched), user_id
                )

            if deferred_matched:
                # Applied by the caller right after the global cantrip pass:
                # cantrips read context.chat.last_messages, so entries not yet
                # injected at cantrip time are invisible to them -- which is
                # the entire point of the flag.
                body_json["_gitv_deferred_lorebook_entries"] = deferred_matched
            if deferred_pending:
                body_json["_gitv_deferred_lorebook_pending"] = True

            # Which entries matched was computed here and thrown away, so the
            # Debug timeline could only report a tag list. Stash it for the
            # caller's capture rather than widening the return type, which has
            # several call sites.
            def _detail(e, deferred: bool) -> dict:
                return {
                    "lorebook_id": e.lorebook_id,
                    "lorebook_name": e.lorebook_name,
                    "entry_id": e.entry_id,
                    "entry_name": e.name,
                    "position": e.position,
                    "tokens": max(1, len(e.content) // 4),
                    "deferred": deferred,
                }

            matched_detail = (
                [_detail(e, False) for e in pre_matched]
                + [_detail(e, True) for e in deferred_matched]
            )
            body_json["_gitv_matched_entries"] = matched_detail
            # 0.42.0 (B7.4): entry ids served by the main pass this turn.
            # The same-turn delta filters these out -- an entry that already
            # injected (old content) must not inject a second time via its
            # update's delta (new content). The rewrite serves from the next
            # turn's main pass, exactly like post-driver ops always have.
            body_json["_gitv_main_matched_entry_ids"] = {
                e["entry_id"] for e in matched_detail if e.get("entry_id")
            }

            # Per-entry prompt metrics (settled decision: granularity is the
            # entry, with the book id on parent_id so aggregates roll up).
            # Deferred entries are recorded here too: they reach the prompt a
            # few stages later, but they reach it.
            from app.services.metrics import record_prompt_object
            for e in matched_detail:
                record_prompt_object(
                    body_json, "lorebook_entry", e["entry_id"],
                    0, e["tokens"], parent_id=e["lorebook_id"],
                )

            return body_json
    except Exception:
        logger.exception("Lorebook injection failed, forwarding original request")
        return body_json


def inject_tool_notification_safe(
    messages: list[dict[str, Any]], notification: str
) -> list[dict[str, Any]]:
    from app.services.driver_callable import inject_tool_notification
    return inject_tool_notification(messages, notification)


async def _run_driver_callable_loop(
    body_json: dict[str, Any],
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    user_id: str,
    request_headers: dict[str, str],
    forward=None,
    tool_only_ids: set[str] | None = None,
    stage_index: int | None = None,
) -> tuple[dict[str, Any], int] | None:
    """Run the Driver-Callable tool loop.

    Returns the final (response_data, status_code) tuple if the loop ran,
    or None if driver-callable is not active for this request (caller
    should forward normally).

    ``forward`` overrides how each round reaches the LLM (map stages inject
    their failover-chain forwarder here); it receives the loop's body dict
    and returns ``(response_data, status_code)``, recording its own LLM
    calls. ``tool_only_ids`` restricts which cantrips are offered as tools
    (a map stage offers only its own). ``stage_index`` attributes Passenger
    LLM calls made by tool cantrips to the map stage running the loop.
    """
    if not body_json.get("_gitv_driver_callable"):
        return None

    from app.services.driver_callable import (
        build_tool_notification,
        execute_tool_call,
        format_tool_result,
        parse_call_tags,
        strip_call_tags,
    )

    turns_remaining = body_json.get("_gitv_driver_callable_turns", 0)

    from app.services.cantrip import _load_active_cantrips
    from app.services.group_resolver import group_direct_members
    from app.services.tagging import should_activate_resource

    direct_ids = group_direct_members(body_json)
    load_extra_ids = direct_ids | tool_only_ids if tool_only_ids else direct_ids
    tags = body_json.get("_gitv_tags") or []
    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(
            db, user_id, "pre_driver", direct_member_ids=load_extra_ids
        )
        # Stage binding is routing, not activation (rule 19): tool_only_ids
        # restricts which activated tools a stage offers, and the Activation
        # Hierarchy decides "activated" on the stage path exactly as on the
        # non-stage path.
        tool_cantrips = {
            c.name.lower(): c
            for c in all_cantrips
            if c.run_driver_callable
            and (tool_only_ids is None or c.id in tool_only_ids)
            and (
                c.id in direct_ids
                or should_activate_resource(
                    c.tag, "cantrip", c.is_active, c.is_public, c.user_id, user_id, tags
                )
            )
        }

    loop_messages = [msg.copy() for msg in body_json.get("messages", [])]
    conversation_id = body_json.get("_gitv_chat_id", "")

    # The per-request Passenger handler, so tool cantrips share the same call
    # budget as every other cantrip position.
    from app.services.passenger import get_passenger_handler

    passenger_handler = await get_passenger_handler(body_json, user_id)
    passenger_handler.stage_index = stage_index

    loop_body = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    loop_body["stream"] = False
    loop_body["messages"] = loop_messages

    from app.services.budget import estimate_tokens as _est_tokens
    from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call
    from app.services.metrics import record_prompt_object

    def _record_loop_call(started: float, response_data: dict[str, Any], status_code: int) -> None:
        # Every tool round is a real upstream call; unrecorded rounds were
        # invisible to the debug trace and to cost accounting alike.
        record_llm_call(
            body_json,
            purpose=PURPOSE_MAIN,
            latency_ms=(time.monotonic() - started) * 1000.0,
            status_code=status_code,
            response_data=response_data,
            messages=loop_body.get("messages"),
            endpoint_id=body_json.get("_gitv_endpoint_id", ""),
            provider=body_json.get("_gitv_provider", ""),
            model_requested=body_json.get("model", ""),
        )

    if forward is None:
        async def forward(loop_body_dict: dict[str, Any]) -> tuple[dict[str, Any], int]:
            body_bytes = json.dumps(loop_body_dict).encode()
            started = time.monotonic()
            response_data, status_code = await _do_forward(
                method, url, headers, body_bytes, timeout,
                provider=body_json.get("_gitv_provider", ""),
                base_url=body_json.get("_gitv_base_url", ""),
                api_key=body_json.get("_gitv_api_key", ""),
                configured=body_json.get("_gitv_llm_params"),
            )
            _record_loop_call(started, response_data, status_code)
            return response_data, status_code

    rounds = 0
    max_rounds = max(turns_remaining, 1)

    while rounds < max_rounds:
        response_data, status_code = await forward(loop_body)

        if status_code != 200:
            logger.warning("Driver-callable loop: forward returned %d", status_code)
            return response_data, status_code

        content = extract_response_text(response_data)
        if not content:
            return response_data, status_code
        calls = parse_call_tags(content)

        if not calls or turns_remaining <= 0:
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned
            return response_data, status_code

        turns_remaining -= 1
        rounds += 1

        assistant_content = strip_call_tags(content).strip()
        loop_messages.append({"role": "assistant", "content": assistant_content})

        for call in calls:
            tool = tool_cantrips.get(call.name)
            if tool is None:
                result_text = f"Error: tool '{call.name}' not found"
            else:
                result_text = await execute_tool_call(
                    tool, call, loop_messages, request_headers, user_id, conversation_id,
                    persist_shared=not (
                        body_json.get("_gitv_sandbox") or body_json.get("_gitv_replay")
                    ),
                    passenger_handler=passenger_handler,
                )

            tool_msg = format_tool_result(call.name, result_text)
            loop_messages.append({"role": "user", "content": tool_msg})
            record_prompt_object(
                body_json, "cantrip_tool", tool.id if tool else call.name,
                0, _est_tokens(tool_msg),
            )
            logger.info(
                "Driver-callable: executed '%s' (turn %d, %d remaining)",
                call.name, rounds, turns_remaining,
            )

        if turns_remaining > 0:
            notification = build_tool_notification(
                [tc for tc in tool_cantrips.values()], turns_remaining
            )
            loop_messages = inject_tool_notification_safe(loop_messages, notification)

        loop_body["messages"] = loop_messages

    response_data, status_code = await forward(loop_body)

    if status_code == 200:
        content = extract_response_text(response_data)
        if content:
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned

    return response_data, status_code


async def _forward_non_streaming_verified(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    body_json: dict[str, Any],
    timeout: httpx.Timeout,
    user_id: str | None,
    request_headers: dict[str, str],
    path: str = "",
) -> JSONResponse:
    if body_json.get("_gitv_driver_callable") and user_id:
        try:
            loop_result = await _run_driver_callable_loop(
                body_json, method, url, headers, timeout, user_id, request_headers,
            )
            if loop_result is not None:
                response_data, status_code = loop_result
                response_data = await finalize_response(
                    response_data,
                    status_code,
                    body_json,
                    user_id,
                    request_headers,
                    method=method,
                    url=url,
                    headers=headers,
                    timeout=timeout,
                    path=path,
                )
                return JSONResponse(status_code=status_code, content=response_data)
        except Exception:
            logger.exception("Driver-callable loop failed, falling through to normal forward")

    failover_chain = body_json.get("_gitv_failover_chain") or []
    if failover_chain and path:
        response_data, status_code = await _forward_with_failover(
            body_json, failover_chain, method, path, headers, timeout, body,
        )
    else:
        started = time.monotonic()
        response_data, status_code = await _do_forward(method, url, headers, body, timeout,
            provider=body_json.get("_gitv_provider", ""),
            base_url=body_json.get("_gitv_base_url", ""),
            api_key=body_json.get("_gitv_api_key", ""),
            configured=body_json.get("_gitv_llm_params"),
        )
        from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call
        record_llm_call(
            body_json,
            purpose=PURPOSE_MAIN,
            latency_ms=(time.monotonic() - started) * 1000.0,
            status_code=status_code,
            response_data=response_data,
            messages=body_json.get("messages"),
            endpoint_id=body_json.get("_gitv_endpoint_id", ""),
            provider=body_json.get("_gitv_provider", ""),
            model_requested=body_json.get("model", ""),
        )

    response_data = await finalize_response(
        response_data,
        status_code,
        body_json,
        user_id,
        request_headers,
        method=method,
        url=url,
        headers=headers,
        timeout=timeout,
        path=path,
    )
    return JSONResponse(status_code=status_code, content=response_data)


def _capture_cantrip_pass(body_json: dict[str, Any], position: str, label: str) -> None:
    """Add one timeline stage for a whole cantrip pass.

    One stage per pass rather than one per cantrip, because cantrips accumulate
    their output and it is applied to the message list once at the end -- a
    per-cantrip stage would show an identical before/after for every one of them
    and read as "nothing changed" when plenty did.

    The stage stores only ids; the detail lives in the run's `cantrips` list, so
    a cantrip's code and output are serialized once per trace rather than twice.
    """
    from app.services.debug import debug_capture

    run = body_json.get("_gitv_debug", {}).get("run", {})
    considered = [c for c in run.get("cantrips", []) if c.get("position") == position]
    if not considered:
        return

    fired = [c for c in considered if c.get("triggered")]
    errored = [c for c in fired if c.get("error")]

    detail = f"{len(fired)} of {len(considered)} ran"
    if errored:
        detail += f", {len(errored)} errored"

    debug_capture(
        body_json, f"cantrips_{position}", label,
        detail=detail,
        metadata={
            "position": position,
            "ran": [{"id": c["id"], "name": c["name"]} for c in fired],
            "skipped": [
                {"id": c["id"], "name": c["name"], "reason": c.get("reason", "")}
                for c in considered if not c.get("triggered")
            ],
            "total_duration_ms": round(
                sum(float(c.get("duration_ms") or 0.0) for c in fired), 1
            ),
        },
    )


def _record_stream_call(
    body_json: dict[str, Any],
    started: float,
    status_code: int,
    response_data: dict[str, Any],
    error: str,
) -> None:
    """Record the upstream call behind a passthrough stream.

    The raw path never went through _forward_with_failover, so nothing else
    records it. Endpoint identity comes off the body, which routing stashed.
    """
    from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call

    record_llm_call(
        body_json,
        purpose=PURPOSE_MAIN,
        latency_ms=(time.monotonic() - started) * 1000.0,
        status_code=status_code,
        response_data=response_data,
        messages=body_json.get("messages"),
        endpoint_id=body_json.get("_gitv_endpoint_id", ""),
        provider=body_json.get("_gitv_provider", ""),
        model_requested=body_json.get("model", ""),
        error=error,
    )


async def _forward_streaming(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: httpx.Timeout,
    user_id: str | None = None,
    body_json: dict[str, Any] | None = None,
    path: str = "",
    request_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    has_memory_processing = (
        user_id is not None
        and body_json is not None
        and body_json.get("_gitv_chat_id")
    )

    if not has_memory_processing:
        return await _forward_streaming_raw(
            method, url, headers, body, timeout,
            user_id=user_id, body_json=body_json,
        )

    return await _forward_streaming_with_memory(
        method, url, headers, body, timeout, user_id, body_json, path,
        request_headers=request_headers or {},
    )


async def _forward_streaming_raw(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout,
    user_id: str | None = None, body_json: dict[str, Any] | None = None,
) -> StreamingResponse:
    """True passthrough stream: bytes reach the client as they arrive.

    When debug is on the SSE deltas are also accumulated as they pass, so the
    exchange can be saved once the stream ends. Accumulation never gates a
    yield and its failure is swallowed -- a debug problem must not stall or
    corrupt the stream the user is reading.
    """
    client = httpx.AsyncClient(timeout=timeout)
    debug_on = bool(body_json is not None and body_json.get("_gitv_debug") and user_id)
    started = time.monotonic()

    async def stream_generator():
        accumulator = _StreamAccumulator() if debug_on else None
        status = 0
        error = ""
        try:
            async with client.stream(method, url, headers=headers, content=body) as response:
                status = response.status_code
                _log_response(response.status_code, 0)
                async for chunk in response.aiter_bytes():
                    if accumulator is not None:
                        accumulator.feed(chunk)
                    yield chunk
        except httpx.ConnectError:
            logger.error("proxy streaming connection error: %s", url)
            error = "Failed to connect to upstream endpoint"
            yield (
                b'data: {"error":{"message":"Failed to connect to upstream endpoint",'
                b'"type":"proxy_error"}}\n\n'
            )
        except httpx.TimeoutException:
            logger.error("proxy streaming timeout: %s", url)
            error = "Upstream endpoint timed out"
            yield (
                b'data: {"error":{"message":"Upstream endpoint timed out",'
                b'"type":"proxy_error"}}\n\n'
            )
        finally:
            await client.aclose()
            if accumulator is not None:
                # A stream that died partway still produced a trace worth
                # keeping -- often the most interesting one.
                try:
                    response_data = accumulator.as_response(body_json.get("model", ""))
                    _record_stream_call(body_json, started, status, response_data, error)
                    await _save_debug_exchange(response_data, body_json, user_id)
                except Exception:
                    logger.exception("Failed to save debug exchange for streamed response")
            if body_json is not None:
                from app.services.metrics import flush_metrics
                await flush_metrics(body_json)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _forward_streaming_with_memory(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: httpx.Timeout,
    user_id: str,
    body_json: dict[str, Any],
    path: str = "",
    request_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """Buffer a streaming response, run response-side stages, extract and
    strip <memstore> tags, record the post-hash, then re-emit as SSE.

    The upstream call is made non-streaming: this path buffers the full
    response before the client sees a byte either way, and an SSE body cannot
    survive `_do_forward`'s JSON parse -- forwarding the body with `stream:
    true` intact returned an empty response to the client on every raw
    (non-LiteLLM) endpoint from 0.9.x until 0.29.0. The client still receives
    SSE, re-emitted from the buffered response.
    """
    body_json = dict(body_json or {})
    body_json["stream"] = False
    body = json.dumps(
        {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    ).encode()

    failover_chain = body_json.get("_gitv_failover_chain") if body_json else None
    if failover_chain and path:
        response_data, status_code = await _forward_with_failover(
            body_json, failover_chain, method, path, headers, timeout, body,
        )
    else:
        started = time.monotonic()
        response_data, status_code = await _do_forward(method, url, headers, body, timeout,
            provider=body_json.get("_gitv_provider", "") if body_json else "",
            base_url=body_json.get("_gitv_base_url", "") if body_json else "",
            api_key=body_json.get("_gitv_api_key", "") if body_json else "",
            configured=body_json.get("_gitv_llm_params") if body_json else None,
        )
        from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call
        record_llm_call(
            body_json,
            purpose=PURPOSE_MAIN,
            latency_ms=(time.monotonic() - started) * 1000.0,
            status_code=status_code,
            response_data=response_data,
            messages=body_json.get("messages"),
            endpoint_id=body_json.get("_gitv_endpoint_id", ""),
            provider=body_json.get("_gitv_provider", ""),
            model_requested=body_json.get("model", ""),
        )

    if status_code == 200:
        ux_settings = await _load_ux_settings(user_id)
        post_proc = ux_settings.get("streaming_post_processing", True)
        response_data = await finalize_response(
            response_data,
            status_code,
            body_json,
            user_id,
            request_headers,
            run_post_driver=post_proc,
            run_verification=False,
            run_response_capture=True,
            run_post_navigator=post_proc,
            run_memories=True,
            append_notices=True,
            run_bypass_decode=True,
            save_debug=True,
            flush_metrics=True,
        )

        model = body_json.get("model", "")
        return _convert_to_sse(
            JSONResponse(status_code=200, content=response_data),
            model,
            gitv_status=ux_settings.get("gitv_status", False),
            simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
            preserve_thinking=ux_settings.get("preserve_thinking", True),
        )

    response_data = await finalize_response(
        response_data,
        status_code,
        body_json,
        user_id,
        request_headers,
        run_post_driver=False,
        run_verification=False,
        run_response_capture=False,
        run_post_navigator=False,
        run_memories=False,
        append_notices=False,
        run_bypass_decode=False,
        save_debug=True,
        flush_metrics=True,
    )
    return JSONResponse(status_code=status_code, content=response_data)
