import json
import logging
import time
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session
from app.models.lorebook import Lorebook
from app.services.cantrip import process_cantrips
from app.services.llm_params import (
    RESERVED_NAMES,
    ParamDef,
    apply_to_body,
    parse_params,
    resolve,
)
from app.services.lorebook import inject_entries
from app.services.routing import FailoverEndpoint, resolve_routing
from app.services.verification import run_verification_loop

logger = logging.getLogger(__name__)

FORWARD_HEADERS = {"content-type", "accept"}
SKIP_HEADERS = {"host", "authorization", "content-length", "transfer-encoding"}

# Parameters LiteLLM accepts as named kwargs. Anything outside this set that the
# user has deliberately configured is passed through `extra_body` instead;
# anything outside it that only the *client* sent is still dropped, because the
# raw body is not forwarded on this path and guessing which unknown client keys
# a provider will tolerate is how the Gemini `top_k` failures started.
#
# `reasoning_effort` and `max_completion_tokens` were absent until Phase 23,
# which is why a client that sent either lost it on every provider endpoint.
# `tools`, `tool_choice` and `parallel_tool_calls` were absent until Phase 26a,
# the same class of bug: a client-sent tool definition was silently dropped on
# every provider endpoint, so native function calling never reached the model.
LITELLM_NATIVE_PARAMS: frozenset[str] = frozenset({
    "temperature", "max_tokens", "top_p", "top_k", "stream",
    "stop", "frequency_penalty", "presence_penalty", "seed",
    "n", "logprobs", "user",
    "reasoning_effort", "max_completion_tokens", "response_format", "thinking",
    "tools", "tool_choice", "parallel_tool_calls",
})


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


def auth_headers(api_key: str) -> dict[str, str]:
    """Headers for internal LLM calls (judges, summarizers, map stages,
    lexicon compiles). A keyless endpoint (local LLMs, LAN gateways) must
    not get an Authorization header at all: httpcore rejects `Bearer ` with
    an empty credential as an illegal header value, so the unconditional
    form failed every internal call on keyless endpoints."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_upstream_url(base_url: str, path: str, api_base_path: str = "") -> str:
    if api_base_path:
        if api_base_path.endswith("/chat/completions"):
            if path == "/v1/chat/completions":
                path = api_base_path
            elif path.startswith("/v1/"):
                suffix = path[3:]
                base = api_base_path[: -len("/chat/completions")]
                path = base + suffix
            elif path == "/v1":
                path = api_base_path[: -len("/chat/completions")]
        elif path.startswith("/v1/"):
            path = api_base_path + path[3:]
        elif path == "/v1":
            path = api_base_path
    return f"{base_url}{path}"


def _log_request(method: str, url: str, model: str | None, stream: bool) -> None:
    logger.info("proxy %s %s model=%s stream=%s", method, url, model, stream)
    logger.debug("proxy headers: api_key_present=%s", "<redacted>" if "sk-" in str(url) else "check")


def _log_response(status_code: int, elapsed: float, response_body: str = "") -> None:
    logger.info("proxy response status=%d elapsed=%.2fs", status_code, elapsed)
    if status_code >= 400 and response_body:
        logger.debug("proxy error response body: %.500s", response_body[:500])


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

        from app.services.metrics import init_metrics
        init_metrics(body_json, user_id)

        from app.services.command_tags import (
            extract_action_commands_from_messages,
            extract_command_tags_from_messages,
            strip_action_commands_from_messages,
            strip_command_tags_from_messages,
            strip_gitv_notices_from_messages,
        )

        # jslorebook blocks are extracted and stripped for prompt hygiene only;
        # nothing executes them. Wiring them up is Phase 20 (dynamic lorebooks),
        # deferred — see Planning/project-plan.md phase 27 item 8.
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
            tags, activated_group_names = await resolve_group_tags(group_db, user_id, tags)
        if activated_group_names:
            body_json["_gitv_tags"] = tags
            body_json["_gitv_activated_groups"] = activated_group_names
            logger.info("Tag groups activated: %s", activated_group_names)
            if debug_on:
                debug_capture(body_json, "tag_group_resolution", "Tag Group Resolution",
                    detail=f"Activated groups: {', '.join(activated_group_names)}",
                    metadata={"groups": activated_group_names, "expanded_tag_count": len(tags)})

        from app.services.conversation import (
            load_memories_for_chat,
            resolve_conversation,
        )
        from app.services.memory import build_memory_context_block as _build_mem_block

        messages_for_conv = body_json.get("messages", [])
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
                from app.services.conversation import bump_turn_counter

                body_json["_gitv_turn_index"] = await bump_turn_counter(
                    user_id, turn_conversation_id
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

        from app.services.budget import prepare_budget
        await prepare_budget(body_json, user_id, tags)
        if debug_on:
            debug_capture(body_json, "budget_preparation", "Budget Preparation",
                detail="Context budget allocated",
                metadata={"budget": body_json.get("_gitv_budget", {})})

        from app.services.map_pipeline import (
            resolve_map,
            run_map_pipeline,
            stage_bound_cantrip_ids,
        )
        cmd_overrides_map = body_json.get("_gitv_command_overrides", {})
        map_candidate = None
        if cmd_overrides_map.get("map") is not False:
            map_candidate = await resolve_map(user_id, tags)

        # The global cantrip pass runs before map selection is finalized so a
        # cantrip can nominate a map (context.maps.activate). It excludes the
        # candidate map's stage-bound cantrips exactly as before; those run on
        # their own stage inside run_map_pipeline.
        body_json = await process_cantrips(
            body_json, user_id, request_headers, tags,
            internal_chat_id=internal_chat_id,
            exclude_ids=stage_bound_cantrip_ids(map_candidate) if map_candidate else None,
        )

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

            response_data = await run_map_pipeline(body_json, user_id, request_headers, map_active, map_timeout)

            if body_json.get("_gitv_chat_id"):
                response_data = await _extract_response_memories(response_data, user_id, body_json)
                response_data = _append_pending_notices(response_data, body_json)

            if body_json.get("_gitv_debug") and user_id:
                # The map pipeline returns its own verification result; passing
                # it through is what stops every map run storing empty
                # verification_data (and reporting has_verification=False).
                await _save_debug_exchange(
                    response_data, body_json, user_id,
                    vresult=body_json.pop("_gitv_map_vresult", None),
                )

            from app.services.metrics import flush_metrics
            await flush_metrics(body_json)

            if stream:
                ux_settings = await _load_ux_settings(user_id)
                return _convert_to_sse(
                    JSONResponse(status_code=200, content=response_data),
                    model or body_json.get("model", ""),
                    gitv_status=ux_settings.get("gitv_status", False),
                    simulated_speed=ux_settings.get("simulated_streaming_speed", 0),
                    preserve_thinking=ux_settings.get("preserve_thinking", True),
                )

            return JSONResponse(status_code=200, content=response_data)

        # The global pre-driver pass already ran above (before map selection,
        # so cantrip nominations could be counted). Anything between here and
        # the forward is post-cantrip prompt work.
        body_json["_gitv_messages_for_hash"] = body_json.get("messages", [])

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
                body_json["_gitv_driver_callable_tools"] = [
                    {"id": t.id, "name": t.name.lower()} for t in dc_config.tools
                ]
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
        from app.services.prefill import normalize_prefill

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
    layers: list[tuple[str, list[ParamDef]]] = [("user settings", user_layer)]
    if candidate is not None:
        label = f"endpoint '{candidate.endpoint_name}'" if candidate.endpoint_name else "endpoint"
        layers.append((label, candidate.parameters))
        layers.append((f"model '{model}'", candidate.params_for_model(model)))

    resolved = resolve(layers)
    out = apply_to_body(body_json, resolved)
    out["_gitv_llm_params"] = dict(resolved.values)
    out["_gitv_llm_param_sources"] = dict(resolved.sources)
    return out


def _strip_think_tags(content: str) -> str:
    """Remove <think>...</think> blocks from content."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)


def _append_pending_notices(response_data: Any, body_json: dict[str, Any]) -> Any:
    """Append pipeline OOC notices to the reply so the player sees them.

    Notices ([OOC: GitV: ...], built in lexicon.build_notice) ride the final
    content out once and are stripped from messages on every subsequent
    request, so they never persist in the history the client resends.
    """
    notices = body_json.get("_gitv_pending_notices") or []
    if not notices or not isinstance(response_data, dict):
        return response_data
    content_before = ""
    appended = False
    try:
        choices = response_data.get("choices") or []
        if choices and isinstance(choices[0].get("message"), dict):
            message = choices[0]["message"]
            content_before = str(message.get("content") or "")
            joined = "\n".join(notices)
            message["content"] = f"{content_before}\n\n{joined}".strip() if content_before else joined
            appended = True
    except Exception:
        logger.exception("Failed to append pending notices")

    # Captured here rather than at the call sites so every path -- including a
    # future one -- reports the stage, and so a failed append (the player never
    # sees the notice) is visible in the Debug timeline instead of only the log.
    from app.services.debug import debug_capture_response
    if appended:
        debug_capture_response(
            body_json, "pending_notices", "Pending Notices",
            content_before=content_before,
            content_after=str(
                (response_data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            ),
            detail=f"Appended {len(notices)} OOC notice(s) to the reply",
            metadata={"notices": [n[:200] for n in notices]},
        )
    else:
        debug_capture_response(
            body_json, "pending_notices", "Pending Notices",
            detail=(
                f"{len(notices)} notice(s) pending but NOT appended "
                "(no parseable message content; the player will not see them)"
            ),
            metadata={"notices": [n[:200] for n in notices]},
        )
    return response_data


def _convert_to_sse(
    response: JSONResponse,
    model: str,
    gitv_status: bool = False,
    simulated_speed: int = 0,
    preserve_thinking: bool = True,
) -> StreamingResponse:
    import time

    data = json.loads(response.body)
    message = data.get("choices", [{}])[0].get("message", {})
    content = message.get("content", "")
    reasoning = message.get("reasoning_content", "") or message.get("thinking", "")
    chunk_id = data.get("id", "chatcmpl-converted")
    created = data.get("created", 0)

    if not preserve_thinking and content:
        content = _strip_think_tags(content)

    def make_chunk(delta_content: str, finish: str | None = None) -> str:
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": delta_content}, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    def generate():
        if gitv_status:
            status_text = "<think>\n<gitv>\nVerification complete. Streaming response to client.\n</gitv>\n</think>\n"
            yield make_chunk(status_text)

        if reasoning and preserve_thinking:
            yield make_chunk(reasoning)

        if simulated_speed > 0 and content:
            words = content.split(" ")
            for i in range(len(words)):
                if i == 0:
                    yield make_chunk(words[i])
                else:
                    yield make_chunk(" " + words[i])
                time.sleep(60.0 / simulated_speed / 10)
        elif content:
            yield make_chunk(content)

        yield make_chunk("", finish="stop")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _apply_lorebook_injection(
    body_json: dict[str, Any],
    user_id: str,
    tags: list | None = None,
    request_headers: dict[str, str] | None = None,
    internal_chat_id: str = "",
) -> dict[str, Any]:
    try:
        from app.services.tagging import should_activate_resource

        # The signature allows tags=None (meaning "no tags"); normalize so the
        # unconditional should_activate_resource call below never iterates
        # None. The old `if lb.tag and tags:` guard hid this -- with the
        # guard gone, None would raise inside this try and silently disable
        # lorebook injection for whichever caller relied on the default.
        tags = tags or []

        async with async_session() as db:
            result = await db.execute(
                select(Lorebook)
                .where(Lorebook.user_id == user_id)
                .options(selectinload(Lorebook.entries))
            )
            lorebooks = result.scalars().all()

            # Fifth activation source (Phase 29a): the conversation binding.
            # A union source -- it only ever adds. A bound lorebook joins the
            # candidates regardless of its tag or Active flag, so a lexicon
            # lorebook (created inactive, untagged) injects in its own chat
            # and in no other. No prompt tag can switch it off.
            from app.models.conversation_lorebook_binding import ConversationLorebookBinding
            from app.services.lexicon import resolve_action_identity

            conversation_id, _ = resolve_action_identity(request_headers or {}, internal_chat_id)
            bound_lorebook_ids: set[str] = set()
            if conversation_id:
                bound_rows = await db.execute(
                    select(ConversationLorebookBinding.lorebook_id).where(
                        ConversationLorebookBinding.user_id == user_id,
                        ConversationLorebookBinding.conversation_id == conversation_id,
                    )
                )
                bound_lorebook_ids = set(bound_rows.scalars().all())

            active_lorebooks = []
            for lb in lorebooks:
                # Bound lorebooks join unconditionally (fifth source,
                # union-only). Every other candidate goes through
                # should_activate_resource with no caller-side pre-check: the
                # old `if lb.tag and tags:` shape duplicated the hierarchy
                # here and only happened to agree with it.
                if lb.id in bound_lorebook_ids or should_activate_resource(
                    lb.tag, "lore", lb.is_active, lb.is_public, lb.user_id, user_id, tags
                ):
                    active_lorebooks.append(lb)

            if not any(lb.entries for lb in active_lorebooks):
                return body_json

            # Per-book matching (0.35.0): scan depth is a book property, and
            # books marked inject-after-cantrips must be kept out of this
            # pass's injection. Both buckets still sort by insertion_order
            # (stable) so cross-book ordering matches the old flattened
            # single-pass behavior exactly.
            from app.services.lorebook import match_book_entries

            messages = body_json.get("messages", [])
            pre_matched: list = []
            deferred_matched: list = []
            deferred_pending = False
            for lb in active_lorebooks:
                book_entries = [
                    {
                        # Provenance for the debug trace's matched-entry
                        # metadata and the per-entry metrics rows. Absent
                        # since the lorebook engine shipped, which left
                        # MatchedEntry.lorebook_id/entry_id always empty.
                        "lorebook_id": lb.id,
                        "lorebook_name": lb.name,
                        "entry_id": entry.id,
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
                    }
                    for entry in lb.entries
                ]
                book_matched = match_book_entries(messages, book_entries, lb.scan_depth)
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


async def _extract_response_memories(
    response_data: dict[str, Any], user_id: str, body_json: dict[str, Any]
) -> dict[str, Any]:
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.conversation import record_post_hash, save_memories_for_chat
    from app.services.memory import parse_memstore_tags

    cleaned, memories = parse_memstore_tags(content)

    chat_id = body_json.get("_gitv_chat_id", "")
    messages_for_hash = body_json.get("_gitv_messages_for_hash", body_json.get("messages", []))

    if cleaned != content:
        response_data["choices"][0]["message"]["content"] = cleaned

    if chat_id and memories and body_json.get("_gitv_replay"):
        # Guarded here rather than at the five call sites, so a new one cannot
        # miss it. Replay must not persist memories: a second replay would read
        # them back and inject them, so run B would differ from run A for a
        # reason that has nothing to do with what the user changed.
        logger.info("Replay: skipping %d memory write(s)", len(memories))
        memories = {}

    if chat_id and memories:
        # LLM-output-derived write path, not a router — same control-char stripping
        # as sanitize_input() applied at router write paths, since this content gets
        # re-injected into future requests via load_memories_for_chat. See
        # Planning/security-control-document.md.
        from app.services.sanitization import detect_zero_width, strip_control_chars
        memories = {k: strip_control_chars(v) for k, v in memories.items()}
        for k, v in memories.items():
            if detect_zero_width(v):
                logger.warning("Zero-width characters detected in extracted memory '%s' for chat %s", k, chat_id[:12])
        await save_memories_for_chat(chat_id, user_id, memories)
        logger.info("Memory extraction: %d keys saved for chat %s", len(memories), chat_id[:12])

    if chat_id and body_json.get("_gitv_debug") is not None:
        from app.services.debug import debug_capture_response
        debug_capture_response(body_json, "memory_extraction", "Memory Extraction",
            content_before=content[:500],
            content_after=cleaned[:500],
            detail=f"Extracted {len(memories)} memories" if memories else "No memories found",
            metadata={"memory_keys": list(memories.keys()) if memories else []})

    if chat_id:
        await record_post_hash(messages_for_hash, cleaned, chat_id, user_id)

    return response_data


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
) -> dict[str, Any] | None:
    """Run the Driver-Callable tool loop.

    Returns the final response_data dict if the loop ran, or None if
    driver-callable is not active for this request (caller should
    forward normally).

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

    async with async_session() as db:
        all_cantrips = await _load_active_cantrips(db, user_id, "pre_driver")
        tool_cantrips = {c.name.lower(): c for c in all_cantrips if c.run_driver_callable}
    if tool_only_ids is not None:
        tool_cantrips = {
            name: c for name, c in tool_cantrips.items() if c.id in tool_only_ids
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
            )
            _record_loop_call(started, response_data, status_code)
            return response_data, status_code

    rounds = 0
    max_rounds = max(turns_remaining, 1)

    while rounds < max_rounds:
        response_data, status_code = await forward(loop_body)

        if status_code != 200:
            logger.warning("Driver-callable loop: forward returned %d", status_code)
            return response_data

        choices = response_data.get("choices", [])
        if not choices:
            return response_data

        content = choices[0].get("message", {}).get("content", "")
        calls = parse_call_tags(content)

        if not calls or turns_remaining <= 0:
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned
            return response_data

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
        choices = response_data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            cleaned = strip_call_tags(content)
            if cleaned != content:
                response_data["choices"][0]["message"]["content"] = cleaned

    return response_data


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
                body_json["_gitv_driver_callable_completed"] = True
                response_data = loop_result
                status_code = 200

                if status_code == 200 and user_id:
                    response_data = await _apply_post_driver_processing(
                        response_data, body_json, user_id, request_headers
                    )
                    conversation_id = request_headers.get("x-conversation-id", "")
                    try:
                        response_data, vresult = await run_verification_loop(
                            response_data, body_json, method, url, headers, timeout,
                            user_id, conversation_id, body_json.get("_gitv_tags"), path,
                        )
                        if vresult:
                            logger.info(
                                "Verification: approved=%s retries=%d violations_total=%d",
                                vresult.approved,
                                vresult.retries_used,
                                sum(len(c.violations) for c in vresult.check_history),
                            )
                    except Exception:
                        logger.exception("Verification loop failed, returning original response")

                    try:
                        response_data = await _apply_post_navigator_processing(
                            response_data, body_json, user_id, request_headers
                        )
                    except Exception:
                        logger.exception("Post-navigator processing failed")

                if status_code == 200 and body_json.get("_gitv_chat_id"):
                    response_data = await _extract_response_memories(response_data, user_id, body_json)
                response_data = _append_pending_notices(response_data, body_json)

                if isinstance(response_data, dict):
                    response_data.pop("_gitv_forbidden_summary", None)

                if status_code == 200 and body_json.get("_gitv_bypass_method"):
                    from app.services.bypass import apply_bypass_decode
                    response_data = apply_bypass_decode(response_data, body_json["_gitv_bypass_method"])

                if body_json.get("_gitv_debug") and user_id:
                    await _save_debug_exchange(response_data, body_json, user_id, locals().get("vresult"))

                from app.services.metrics import flush_metrics
                await flush_metrics(body_json)

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

    if status_code == 200 and user_id:
        response_data = await _apply_post_driver_processing(
            response_data, body_json, user_id, request_headers
        )

        conversation_id = request_headers.get("x-conversation-id", "")
        try:
            response_data, vresult = await run_verification_loop(
                response_data, body_json, method, url, headers, timeout,
                user_id, conversation_id, body_json.get("_gitv_tags"), path,
            )
            if vresult:
                logger.info(
                    "Verification: approved=%s retries=%d violations_total=%d",
                    vresult.approved,
                    vresult.retries_used,
                    sum(len(c.violations) for c in vresult.check_history),
                )
        except Exception:
            logger.exception("Verification loop failed, returning original response")

        try:
            response_data = await _apply_post_navigator_processing(
                response_data, body_json, user_id, request_headers
            )
        except Exception:
            logger.exception("Post-navigator processing failed")

    if status_code == 200 and user_id and body_json.get("_gitv_chat_id"):
        if body_json.get("_gitv_command_overrides", {}).get("memory") is not False:
            response_data = await _extract_response_memories(response_data, user_id, body_json)
            response_data = _append_pending_notices(response_data, body_json)

    if isinstance(response_data, dict):
        response_data.pop("_gitv_forbidden_summary", None)

    if status_code == 200 and body_json.get("_gitv_bypass_method"):
        from app.services.bypass import apply_bypass_decode
        response_data = apply_bypass_decode(response_data, body_json["_gitv_bypass_method"])

    if body_json.get("_gitv_debug") and user_id:
        await _save_debug_exchange(response_data, body_json, user_id, locals().get("vresult"))

    from app.services.metrics import flush_metrics
    await flush_metrics(body_json)

    return JSONResponse(status_code=status_code, content=response_data)


async def _save_debug_exchange(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    vresult: Any = None,
) -> None:
    """Capture pipeline data and response for debug mode."""
    from app.services.debug import capture_exchange, debug_capture_response

    response_content = ""
    response_thinking = ""
    choices = response_data.get("choices", []) if isinstance(response_data, dict) else []
    if choices:
        message = choices[0].get("message", {})
        response_content = message.get("content", "")
        response_thinking = message.get("reasoning_content", "") or message.get("thinking", "")

    verification_data: dict[str, Any] = {}
    if vresult:
        verification_data = {
            "approved": vresult.approved,
            "retries_used": vresult.retries_used,
            "check_history": [
                {
                    "approved": c.approved,
                    "violations": _serialize_violations(c.violations),
                    "thinking": c.judgments[0].thinking if c.judgments else "",
                }
                for c in vresult.check_history
            ],
        }
        debug_capture_response(body_json, "verification", "Verification",
            content_after=response_content,
            detail=f"{'Approved' if vresult.approved else 'Rejected'}, {vresult.retries_used} retry/retries",
            metadata=verification_data)
    elif body_json.get("_gitv_debug"):
        debug_capture_response(body_json, "verification", "Verification",
            detail="Not enabled")

    if body_json.get("_gitv_bypass_method"):
        debug_capture_response(body_json, "bypass_decoding", "Bypass Decoding",
            content_after=response_content,
            detail=f"Method: {body_json['_gitv_bypass_method']}")

    # Content and reasoning are stored whole. A comparison built on text clipped
    # at 500 characters cannot show where two runs diverged, which was the point.
    # The size bound is applied to the serialized trace in capture_exchange.
    debug_capture_response(body_json, "llm_response", "LLM Response",
        content_after=response_content,
        detail="Final response from upstream LLM",
        metadata={"thinking": response_thinking} if response_thinking else None)

    pipeline_data = body_json.get("_gitv_debug", {})

    from app.services.debug_metrics import summarize_run
    started = body_json.get("_gitv_request_started")
    total_ms = (time.monotonic() - started) * 1000.0 if started else None
    if isinstance(pipeline_data, dict):
        pipeline_data.setdefault("run", {})["totals"] = summarize_run(
            pipeline_data, total_latency_ms=total_ms
        )

    await capture_exchange(
        user_id=user_id,
        chat_id=body_json.get("_gitv_chat_id", ""),
        model=body_json.get("model", ""),
        pipeline_data=pipeline_data,
        response_content=response_content,
        verification_data=verification_data,
    )


def _serialize_violations(violations: Any) -> list[dict[str, Any]]:
    """Convert VerificationJudgment dataclasses to plain dicts.

    These used to reach the database through json.dumps(default=str), which
    renders a dataclass as its Python repr. The UI printed that repr raw, and a
    diff between two runs would have compared repr strings character by
    character -- noise, not signal.
    """
    from dataclasses import asdict, is_dataclass

    out: list[dict[str, Any]] = []
    for v in violations or []:
        if is_dataclass(v) and not isinstance(v, type):
            out.append(asdict(v))
        elif isinstance(v, dict):
            out.append(v)
        else:
            out.append({"detail": str(v)})
    return out


async def _apply_post_driver_processing(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any]:
    """Run pre-Navigator cantrips and forbidden word scan on Driver response."""
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    body_json_for_cantrip = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        new_content = await process_cantrips_post_driver(
            content, body_json_for_cantrip, user_id, request_headers,
            tags=tags, position="pre_navigator",
        )
        if new_content != content:
            response_data["choices"][0]["message"]["content"] = new_content
            content = new_content
            logger.info("Pre-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "pre_navigator_cantrips", "Pre-Navigator Cantrips",
                    content_before=response_data["choices"][0]["message"]["content"][:500],
                    content_after=content[:500],
                    detail="Response modified by pre-navigator cantrips")
        elif debug_on:
            from app.services.debug import debug_capture_response
            debug_capture_response(body_json, "pre_navigator_cantrips", "Pre-Navigator Cantrips",
                detail="No changes")
    except Exception:
        logger.exception("Pre-navigator cantrip processing failed")

    try:
        from app.services.forbidden_words import scan_response
        cmd_overrides = body_json.get("_gitv_command_overrides", {})
        if cmd_overrides.get("forbidden") is not False:
            scan_result = await scan_response(content, user_id)
            if scan_result.has_matches:
                response_data["_gitv_forbidden_summary"] = scan_result.summary
                logger.info(
                    "Forbidden words: %d phrase(s) matched",
                    len(scan_result.matches),
                )
                if debug_on:
                    from app.services.debug import debug_capture_response
                    debug_capture_response(body_json, "forbidden_words", "Forbidden Words Scan",
                        detail=f"{len(scan_result.matches)} match(es) found",
                        metadata={"matches": [m.word if hasattr(m, 'word') else str(m) for m in scan_result.matches]})
            elif debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "forbidden_words", "Forbidden Words Scan",
                    detail="No matches")
    except Exception:
        logger.exception("Forbidden word scan failed")

    return response_data


async def _apply_post_navigator_processing(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any]:
    """Run post-Navigator cantrips (final cleanup) on the approved response."""
    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    body_json_for_cantrip = {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        new_content = await process_cantrips_post_driver(
            content, body_json_for_cantrip, user_id, request_headers,
            tags=tags, position="post_navigator",
        )
        if new_content != content:
            response_data["choices"][0]["message"]["content"] = new_content
            logger.info("Post-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                    content_before=content[:500], content_after=new_content[:500],
                    detail="Response modified by post-navigator cantrips")
        elif debug_on:
            from app.services.debug import debug_capture_response
            debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                detail="No changes")
    except Exception:
        logger.exception("Post-navigator cantrip processing failed")

    return response_data


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


def _record_call(
    body_json: dict[str, Any],
    candidate: FailoverEndpoint,
    attempt: int,
    started: float,
    *,
    status_code: int,
    response_data: dict[str, Any] | None,
    error: str = "",
) -> None:
    """Record one upstream attempt on the debug trace.

    Called for failures as well as successes: a run whose first endpoint 502'd
    and whose second answered took the sum of both latencies, and a comparison
    that only showed the winner would mis-attribute the wall-clock time.
    """
    from app.services.debug_metrics import PURPOSE_MAIN, record_llm_call

    record_llm_call(
        body_json,
        purpose=PURPOSE_MAIN,
        latency_ms=(time.monotonic() - started) * 1000.0,
        status_code=status_code,
        response_data=response_data,
        messages=body_json.get("messages"),
        endpoint_id=candidate.endpoint_id,
        endpoint_name=candidate.endpoint_name,
        provider=candidate.provider,
        model_requested=candidate.model or body_json.get("model", ""),
        failover_attempt=attempt,
        error=error,
    )


async def _forward_with_failover(
    body_json: dict[str, Any],
    chain: list[FailoverEndpoint],
    method: str,
    path: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    body_bytes: bytes,
) -> tuple[dict[str, Any], int]:
    """Forward a request through the failover chain.

    Iterates candidates in priority order. Any failure (any HTTP status != 200
    or any exception) advances to the next candidate — there is no
    retryable/non-retryable distinction, because different endpoints may carry
    different models, API keys, or providers (e.g. a free OpenRouter key
    failing over to a paid endpoint). Only total exhaustion returns an error.

    The `path` is the request path (e.g. '/v1/chat/completions'); each
    candidate's base_url + api_base_path produce a distinct upstream URL."""
    if not chain:
        return (
            {"error": {"message": "No endpoint configured for this request", "type": "proxy_error"}},
            503,
        )

    failover_notes: list[str] = []

    for idx, candidate in enumerate(chain):
        upstream_url = _build_upstream_url(candidate.base_url, path, candidate.api_base_path)

        # Re-stash per-candidate fields so _do_forward picks them up.
        body_json["_gitv_provider"] = candidate.provider
        body_json["_gitv_base_url"] = candidate.base_url
        body_json["_gitv_api_key"] = candidate.api_key
        body_json["_gitv_endpoint_id"] = candidate.endpoint_id

        effective_model = candidate.model or body_json.get("model", "")

        # Candidates may carry different endpoint- and model-level parameters, so
        # the first candidate's resolved set is not reusable. Re-resolve against
        # this one. The user-settings layer travels on body_json from the driver
        # path and does not change between candidates.
        if idx == 0:
            candidate_params = body_json.get("_gitv_llm_params", {})
        else:
            resolved = resolve(
                [
                    (
                        f"endpoint '{candidate.endpoint_name}'"
                        if candidate.endpoint_name
                        else "endpoint",
                        candidate.parameters,
                    ),
                    (f"model '{effective_model}'", candidate.params_for_model(effective_model)),
                ]
            )
            # Keep whatever the user-settings layer contributed on the first
            # pass, then let this candidate's own layers win over it.
            candidate_params = {**body_json.get("_gitv_llm_params", {}), **resolved.values}

        # Rebuild when this candidate changes the model or the parameters.
        forward_body = body_bytes
        needs_rebuild = (candidate.model and body_json.get("model") != candidate.model) or (
            idx > 0 and candidate_params != body_json.get("_gitv_llm_params", {})
        )
        if needs_rebuild:
            trial_body = dict(body_json)
            if candidate.model:
                trial_body["model"] = candidate.model
            trial_body = apply_to_body(trial_body, candidate_params)
            forward_body = json.dumps(
                {k: v for k, v in trial_body.items() if not k.startswith("_gitv")}
            ).encode()

        started = time.monotonic()
        try:
            response_data, status_code = await _do_forward(
                method, upstream_url, headers, forward_body, timeout,
                provider=candidate.provider,
                base_url=candidate.base_url,
                api_key=candidate.api_key,
                configured=candidate_params,
            )
        except Exception as exc:
            logger.exception(
                "Failover: endpoint '%s' (%s) raised an exception, trying next",
                candidate.endpoint_name, upstream_url,
            )
            _record_call(
                body_json, candidate, idx, started,
                status_code=0, response_data=None, error=str(exc)[:500],
            )
            failover_notes.append(f"'{candidate.endpoint_name}' failed with an exception")
            continue

        _record_call(body_json, candidate, idx, started,
                     status_code=status_code, response_data=response_data)

        if status_code == 200:
            if failover_notes:
                logger.info(
                    "Failover: succeeded on candidate %d/%d '%s' after %d failure(s)",
                    idx + 1, len(chain), candidate.endpoint_name, len(failover_notes),
                )
            return response_data, 200

        logger.info(
            "Failover: endpoint '%s' (%s) returned %d, trying next",
            candidate.endpoint_name, upstream_url, status_code,
        )
        failover_notes.append(f"'{candidate.endpoint_name}' returned HTTP {status_code}")

    logger.warning("Failover: all %d endpoint(s) exhausted", len(chain))
    return (
        {
            "error": {
                "message": f"All endpoints failed: {'; '.join(failover_notes)}",
                "type": "proxy_error",
            }
        },
        503,
    )


async def _do_forward(
    method: str, url: str, headers: dict[str, str], body: bytes, timeout: httpx.Timeout,
    provider: str = "", base_url: str = "", api_key: str = "",
    configured: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Forward one request upstream.

    `configured` is the resolved LLM parameter set. The raw httpx path does not
    need it -- the caller already wrote those keys into `body`, which is
    forwarded verbatim. LiteLLM does need it, because it builds its call from
    named kwargs rather than the body, so it has to be told which keys are
    deliberate rather than incidental.
    """
    if provider:
        return await _do_forward_litellm(body, provider, base_url, api_key, timeout, configured)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.request(method, url, headers=headers, content=body)
        except httpx.ConnectError:
            logger.error("proxy connection error: %s", url)
            return (
                {"error": {"message": "Failed to connect to upstream endpoint", "type": "proxy_error"}},
                502,
            )
        except httpx.TimeoutException:
            logger.error("proxy timeout: %s", url)
            return (
                {"error": {"message": "Upstream endpoint timed out", "type": "proxy_error"}},
                504,
            )

    _log_response(response.status_code, response.elapsed.total_seconds(),
                  response.text if response.status_code >= 400 else "")

    try:
        return response.json(), response.status_code
    except Exception:
        return (
            {"error": {"message": response.text, "type": "upstream_error"}},
            response.status_code,
        )


async def _do_forward_litellm(
    body: bytes, provider: str, base_url: str, api_key: str, timeout: httpx.Timeout,
    configured: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Forward request via LiteLLM for provider-specific compatibility.

    Unlike the raw httpx path, which forwards the body verbatim, this builds the
    call from named kwargs -- so anything not named here is silently dropped.
    That is how a client's `reasoning_effort` reached a raw endpoint intact and
    vanished the moment the endpoint had a provider set.
    """
    try:
        import litellm
    except ImportError:
        return (
            {"error": {"message": "LiteLLM is not installed. Provider-based endpoints require it. Run: pip install litellm==1.89.4", "type": "proxy_error"}},
            500,
        )

    try:
        body_json = json.loads(body)
    except Exception:
        return {"error": {"message": "Invalid JSON body", "type": "proxy_error"}}, 400

    model = body_json.get("model", "")
    messages = body_json.get("messages", [])
    stream = body_json.get("stream", False)

    if not model:
        return {"error": {"message": "Model not specified", "type": "proxy_error"}}, 400

    litellm_model = f"{provider}/{model}" if not model.startswith(f"{provider}/") else model

    kwargs: dict[str, Any] = {"api_key": api_key}

    if provider in ("openai_compatible", "custom_openai"):
        litellm_model = f"openai/{model}"
        kwargs["api_base"] = base_url

    for param in LITELLM_NATIVE_PARAMS:
        if param in body_json:
            kwargs[param] = body_json[param]

    # Configured parameters are applied after the client's, so a value the user
    # set on an endpoint, model, rule or map stage wins. Names LiteLLM models
    # natively go as kwargs; anything else rides in extra_body, which is how
    # LiteLLM forwards provider-specific keys it has no opinion about. Without
    # this an arbitrary user-named parameter could never reach a provider.
    for name, value in (configured or {}).items():
        if name in RESERVED_NAMES or name.startswith("_gitv"):
            continue
        if name in LITELLM_NATIVE_PARAMS:
            kwargs[name] = value
        else:
            kwargs.setdefault("extra_body", {})[name] = value

    if stream:
        kwargs["stream"] = True

    timeout_seconds = timeout.read if hasattr(timeout, "read") and timeout.read else settings.request_timeout

    logger.info("litellm forward: provider=%s model=%s stream=%s", provider, model, stream)

    try:
        response = await litellm.acompletion(
            model=litellm_model,
            messages=messages,
            timeout=timeout_seconds,
            **kwargs,
        )

        if stream:
            chunks: list[dict[str, Any]] = []
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            final_metadata: dict[str, Any] = {}
            async for chunk in response:
                chunk_dict = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                chunks.append(chunk_dict)
                if chunk_dict.get("choices"):
                    delta = chunk_dict["choices"][0].get("delta", {})
                    if delta.get("content"):
                        content_parts.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning_parts.append(delta["reasoning_content"])
                if chunk_dict.get("usage"):
                    final_metadata["usage"] = chunk_dict["usage"]
                if chunk_dict.get("model"):
                    final_metadata["model"] = chunk_dict["model"]

            combined = {
                "id": chunks[0].get("id", "chatcmpl-litellm") if chunks else "chatcmpl-litellm",
                "object": "chat.completion",
                "model": final_metadata.get("model", model),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts),
                        **({"reasoning_content": "".join(reasoning_parts)} if reasoning_parts else {}),
                    },
                    "finish_reason": chunks[-1]["choices"][0].get("finish_reason", "stop") if chunks and chunks[-1].get("choices") else "stop",
                }],
                **final_metadata,
            }
            _log_response(200, 0.0)
            return combined, 200

        result = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        _log_response(200, 0.0)
        return result, 200

    except Exception as exc:
        import litellm as _litellm

        status_code = 500
        error_msg = str(exc)
        error_type = type(exc).__name__

        for exc_class, code, msg in [
            ("AuthenticationError", 401, "Authentication failed"),
            ("RateLimitError", 429, "Rate limited"),
            ("BadRequestError", 400, None),
            ("NotFoundError", 404, None),
            ("Timeout", 504, "Request timed out"),
            ("APIConnectionError", 502, "Failed to connect to provider"),
            ("InternalServerError", 500, None),
        ]:
            exc_cls = getattr(_litellm.exceptions, exc_class, None)
            if exc_cls and isinstance(exc, exc_cls):
                status_code = code
                error_msg = msg or str(exc)
                break

        if status_code < 500:
            logger.warning("litellm forward error (%d): %s", status_code, error_msg[:200])
        else:
            logger.exception("litellm forward error: %s", exc)

        _log_response(status_code, 0.0, error_msg)
        return {"error": {"message": error_msg, "type": error_type}}, status_code


class _StreamAccumulator:
    """Reassembles an SSE stream into a non-streaming response shape.

    Only what the debug trace needs: content, reasoning, finish reason and the
    usage block some providers append to the final chunk. Chunk boundaries fall
    anywhere, including mid-line, so a partial tail is carried between feeds.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.usage: dict[str, Any] | None = None
        self.finish_reason = ""
        self.model = ""

    def feed(self, chunk: bytes) -> None:
        try:
            self._buffer += chunk.decode("utf-8", "replace")
        except Exception:
            return

        # Keep the last partial line; SSE events end with a newline.
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                continue
            self._absorb(event)

    def _absorb(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        if event.get("model"):
            self.model = event["model"]
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.content.append(delta["content"])
            if delta.get("reasoning_content"):
                self.reasoning.append(delta["reasoning_content"])
            elif delta.get("thinking"):
                self.reasoning.append(delta["thinking"])

    def as_response(self, fallback_model: str) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self.content),
        }
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)

        response: dict[str, Any] = {
            "object": "chat.completion",
            "model": self.model or fallback_model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self.finish_reason or "stop",
            }],
        }
        if self.usage:
            response["usage"] = self.usage
        return response


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

        # Response-side stages, when enabled (0.29.0, default on). The stream
        # is already buffered, so these cost only their own execution -- and
        # without this block a forbidden-word list or a post-navigator cantrip
        # silently never ran on any streaming request that did not also enable
        # verification. Order mirrors the non-streaming path: post-driver
        # processing, post-navigator cantrips, then memory extraction.
        if ux_settings.get("streaming_post_processing", True):
            response_data = await _apply_post_driver_processing(
                response_data, body_json, user_id, request_headers or {}
            )
            response_data = await _apply_post_navigator_processing(
                response_data, body_json, user_id, request_headers or {}
            )
            if isinstance(response_data, dict):
                response_data.pop("_gitv_forbidden_summary", None)

        response_data = await _extract_response_memories(response_data, user_id, body_json)
        response_data = _append_pending_notices(response_data, body_json)

        # This path buffers the whole response before re-emitting it as SSE, so
        # the exchange is as complete as the non-streaming one. It was simply
        # never saved -- which is why a user whose client streams saw an empty
        # Debug tab while the pipeline was working normally.
        if body_json.get("_gitv_debug"):
            await _save_debug_exchange(response_data, body_json, user_id)

        from app.services.metrics import flush_metrics
        await flush_metrics(body_json)

        model = body_json.get("model", "")
        return _convert_to_sse(
            JSONResponse(status_code=200, content=response_data),
            model,
            preserve_thinking=ux_settings.get("preserve_thinking", True),
        )

    from app.services.metrics import flush_metrics
    await flush_metrics(body_json)

    return JSONResponse(status_code=status_code, content=response_data)
