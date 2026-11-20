from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.services.upstream import _resolve_proxy_override, extract_response_text
from app.services.verification import run_verification_loop

logger = logging.getLogger(__name__)


async def _record_conversation_anchor(
    response_data: dict[str, Any], user_id: str, body_json: dict[str, Any]
) -> None:
    """0.42.0 (B6.1): conversation identity must survive `<MEMORY:off>`.

    Recording the post-hash used to hide inside _extract_response_memories,
    so turning memory off for a request also stopped the conversation from
    matching on the next one -- the chat silently started over. Memory
    extraction stays skipped; only the anchor is recorded, against the
    client-bound content (memstore tags are stripped before hashing, so
    the post-hash matches what the client sends back on its next
    request)."""
    content = extract_response_text(response_data)
    if not content:
        return
    chat_id = body_json.get("_gitv_chat_id", "")
    if not chat_id:
        return
    messages_for_hash = body_json.get("_gitv_messages_for_hash", body_json.get("messages", []))
    from app.services.conversation import record_post_hash

    await record_post_hash(messages_for_hash, content, chat_id, user_id)


async def _extract_response_memories(
    response_data: dict[str, Any], user_id: str, body_json: dict[str, Any]
) -> dict[str, Any]:
    content = extract_response_text(response_data)
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


async def _handle_response_memories(
    response_data: dict[str, Any], user_id: str, body_json: dict[str, Any]
) -> dict[str, Any]:
    """Extract/save memories unless <MEMORY:off> is active; when off, strip
    <memstore> tags so raw tags never leak and record the conversation anchor."""
    if body_json.get("_gitv_command_overrides", {}).get("memory") is not False:
        extract_fn = _resolve_proxy_override("_extract_response_memories", _extract_response_memories)
        return await extract_fn(response_data, user_id, body_json)

    content = extract_response_text(response_data)
    if content:
        from app.services.memory import parse_memstore_tags

        cleaned, _ = parse_memstore_tags(content)
        if cleaned != content:
            response_data["choices"][0]["message"]["content"] = cleaned

    if body_json.get("_gitv_chat_id"):
        anchor_fn = _resolve_proxy_override("_record_conversation_anchor", _record_conversation_anchor)
        await anchor_fn(response_data, user_id, body_json)
    return response_data


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
            content_after=extract_response_text(response_data),
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


async def _save_debug_exchange(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    vresult: Any = None,
) -> None:
    """Capture pipeline data and response for debug mode."""
    from app.services.debug import capture_exchange, debug_capture_response

    response_content = extract_response_text(response_data)
    response_thinking = ""
    choices = response_data.get("choices", []) if isinstance(response_data, dict) else []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
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


async def _apply_post_driver_processing(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
) -> dict[str, Any]:
    """Run pre-Navigator cantrips and forbidden word scan on Driver response."""
    content = extract_response_text(response_data)
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        # Full body, not a _gitv_-stripped copy: the pass needs _gitv_chat_id
        # (header-less clients' store writes), the sandbox/replay isolation
        # flags, the shared Passenger handler and the metrics accumulator.
        # The stripped copy silently dropped all four.
        from app.services.cantrip import embedded_jslb_scripts

        new_content = await process_cantrips_post_driver(
            content, body_json, user_id, request_headers,
            tags=tags, position="pre_navigator",
            embedded_scripts=embedded_jslb_scripts(body_json, "pre_navigator"),
        )
        if new_content != content:
            # 0.42.0 (N4a): capture the BEFORE text before it is overwritten.
            # Both snapshots used to read the post-cantrip content, so the
            # stage's diff always showed "no change".
            original_content = content
            response_data["choices"][0]["message"]["content"] = new_content
            content = new_content
            logger.info("Pre-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "pre_navigator_cantrips", "Pre-Navigator Cantrips",
                    content_before=original_content,
                    content_after=new_content,
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
    content = extract_response_text(response_data)
    if not content:
        return response_data

    from app.services.cantrip import process_cantrips_post_driver

    tags = body_json.get("_gitv_tags", [])
    debug_on = body_json.get("_gitv_debug") is not None

    try:
        # Full body — same four keys as the pre-navigator site above.
        from app.services.cantrip import embedded_jslb_scripts

        new_content = await process_cantrips_post_driver(
            content, body_json, user_id, request_headers,
            tags=tags, position="post_navigator",
            embedded_scripts=embedded_jslb_scripts(body_json, "post_navigator"),
        )
        if new_content != content:
            response_data["choices"][0]["message"]["content"] = new_content
            logger.info("Post-navigator cantrips modified response")
            if debug_on:
                from app.services.debug import debug_capture_response
                debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                    content_before=content, content_after=new_content,
                    detail="Response modified by post-navigator cantrips")
        elif debug_on:
            from app.services.debug import debug_capture_response
            debug_capture_response(body_json, "post_navigator_cantrips", "Post-Navigator Cantrips",
                detail="No changes")
    except Exception:
        logger.exception("Post-navigator cantrip processing failed")

    return response_data


async def finalize_response(
    response_data: dict[str, Any],
    status_code: int,
    body_json: dict[str, Any],
    user_id: str | None,
    request_headers: dict[str, str] | None = None,
    *,
    method: str = "POST",
    url: str = "",
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    path: str = "",
    vresult: Any = None,
    run_post_driver: bool = True,
    run_verification: bool = True,
    run_response_capture: bool = True,
    run_post_navigator: bool = True,
    run_memories: bool = True,
    append_notices: bool = True,
    run_bypass_decode: bool = True,
    save_debug: bool = True,
    flush_metrics: bool = True,
) -> dict[str, Any]:
    """Run response-side post-processing stages in canonical order.

    Replaces the duplicated response-tail blocks across
    ``_forward_non_streaming_verified``, the driver-callable branch,
    ``_forward_streaming_with_memory``, and the map branch in ``proxy.py``.
    """
    req_headers = request_headers or {}

    if status_code == 200 and user_id:
        if run_post_driver:
            post_driver_fn = _resolve_proxy_override(
                "_apply_post_driver_processing", _apply_post_driver_processing
            )
            response_data = await post_driver_fn(
                response_data, body_json, user_id, req_headers
            )

        if run_verification:
            conversation_id = req_headers.get("x-conversation-id", "")
            try:
                verify_fn = _resolve_proxy_override("run_verification_loop", run_verification_loop)
                response_data, vresult = await verify_fn(
                    response_data, body_json, method, url, headers or {},
                    timeout or httpx.Timeout(60.0, connect=10.0),
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

        if run_response_capture:
            from app.services.response_tags import process_response_capture
            response_data = await process_response_capture(
                response_data, user_id, body_json
            )

        if run_post_navigator:
            try:
                post_nav_fn = _resolve_proxy_override(
                    "_apply_post_navigator_processing", _apply_post_navigator_processing
                )
                response_data = await post_nav_fn(
                    response_data, body_json, user_id, req_headers
                )
            except Exception:
                logger.exception("Post-navigator processing failed")

    if status_code == 200 and user_id and run_memories and body_json.get("_gitv_chat_id"):
        mem_fn = _resolve_proxy_override("_handle_response_memories", _handle_response_memories)
        response_data = await mem_fn(response_data, user_id, body_json)

    if status_code == 200 and append_notices:
        notices_fn = _resolve_proxy_override("_append_pending_notices", _append_pending_notices)
        response_data = notices_fn(response_data, body_json)

    if isinstance(response_data, dict):
        response_data.pop("_gitv_forbidden_summary", None)

    if status_code == 200 and run_bypass_decode and body_json.get("_gitv_bypass_method"):
        from app.services.bypass import apply_bypass_decode
        response_data = apply_bypass_decode(response_data, body_json["_gitv_bypass_method"])

    if save_debug and body_json.get("_gitv_debug") and user_id:
        debug_fn = _resolve_proxy_override("_save_debug_exchange", _save_debug_exchange)
        await debug_fn(response_data, body_json, user_id, vresult)

    if flush_metrics:
        from app.services.metrics import flush_metrics as _flush_metrics
        await _flush_metrics(body_json)

    return response_data
