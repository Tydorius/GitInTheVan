"""Multi-turn whole-stack cantrip tester (feature-request item 14, shape 1).

Drives a script of turns -- each a user message plus a canned model reply --
through the real cantrip passes with store state threaded entirely in memory
(``store_seeds``): nothing is loaded from or written to the chat_data /
user_data / cantrip_data tables, the body is sandbox-marked so Passenger
calls are recorded but excluded from default aggregates and dynamic-lorebook
ops drop, and the driver makes zero LLM calls. Covers ladders, clocks and
chains -- the "thirty in-world days" case -- without touching live state.

Deliberately not covered (shape-2 territory, maintainer decision 2026-11-19):
maps and stages, verification, the driver-callable tool loop, live lorebook /
memory injection and summarization. Cantrips run with their stored timeouts.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from typing import Any

from app.services.cantrip import (
    embedded_jslb_scripts,
    process_cantrips,
    process_cantrips_post_driver,
)
from app.services.metrics import flush_metrics, init_metrics

logger = logging.getLogger(__name__)

# Editable post-install (house style, like the RAG template's throttle): a
# run approaching this wall-clock budget stops between turns and returns
# partial results. 50 turns x several cantrips x their own timeouts can
# legitimately take minutes; this only caps the runaway case.
TOTAL_BUDGET_S = 600.0


def _strip_tagged_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any]]:
    """Mirror the proxy's pre-pipeline text cleanup on one turn's messages.

    Activation tags are extracted and stripped (cantrips never see
    ``<#...#>``), command tags are stripped, and <jslorebook> blocks are
    extracted for the embedded-script passes. Returns the cleaned message
    list, the parsed tags, and the extracted scripts.
    """
    from app.services.command_tags import strip_command_tags_from_messages
    from app.services.jslorebook import extract_from_messages as extract_jslorebook
    from app.services.tagging import extract_all_tags_from_messages, strip_tags

    tags = extract_all_tags_from_messages(messages)

    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            cleaned.append({**msg, "content": strip_tags(content)})
        elif isinstance(content, list):
            cleaned.append({
                **msg,
                "content": [
                    {**item, "text": strip_tags(item["text"])}
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                    else item
                    for item in content
                ],
            })
        else:
            cleaned.append(dict(msg))

    cleaned = strip_command_tags_from_messages(cleaned)
    scripts, cleaned = extract_jslorebook(cleaned)
    return cleaned, tags, scripts


def _cantrip_records(body_json: dict[str, Any]) -> list[dict[str, Any]]:
    debug = body_json.get("_gitv_debug")
    if not isinstance(debug, dict):
        return []
    run = debug.get("run")
    if not isinstance(run, dict):
        return []
    records = run.get("cantrips")
    return records if isinstance(records, list) else []


def _slim_record(record: dict[str, Any]) -> dict[str, Any]:
    """Per-turn cantrip report: identity and outcome, not code and deltas."""
    return {
        "name": record.get("name", ""),
        "position": record.get("position", ""),
        "triggered": bool(record.get("triggered")),
        "reason": record.get("reason", ""),
        "error": record.get("error", ""),
        "debug_logs": list(record.get("debug_logs") or []),
        "duration_ms": record.get("duration_ms", 0.0),
    }


async def run_sequence(
    user_id: str,
    *,
    conversation_id: str = "",
    user_name: str = "User",
    model: str = "",
    initial_messages: list[dict[str, Any]] | None = None,
    seed_chat_data: dict[str, Any] | None = None,
    seed_user_data: dict[str, Any] | None = None,
    seed_cantrip_data: dict[str, dict[str, Any]] | None = None,
    start_turn_index: int = 0,
    only_cantrip_ids: set[str] | None = None,
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the turn script. Returns per-turn records and final store states.

    Never raises for a cantrip-level failure (the turn records the error and
    the run continues, like the pipeline degrades). A run that exceeds the
    total wall-clock budget stops between turns and reports
    ``stopped_at_turn`` / ``stop_reason`` alongside the partial results.
    """
    run_started = time.monotonic()
    conv_id = conversation_id or f"tester:{uuid.uuid4()}"

    body_json: dict[str, Any] = {
        "model": model or "",
        "stream": False,
        "messages": [],
        "_gitv_sandbox": True,
        "_gitv_chat_id": conv_id,
        "_gitv_turn_index": start_turn_index,
        "_gitv_is_regenerate": False,
    }
    init_metrics(body_json, user_id)
    from app.services.debug import init_debug

    init_debug(body_json, [])

    seeds: dict[str, Any] = {
        "chat_data": dict(seed_chat_data or {}),
        "user_data": dict(seed_user_data or {}),
        "cantrip_data": {
            cid: dict(vals) for cid, vals in (seed_cantrip_data or {}).items()
        },
    }

    history: list[dict[str, Any]] = [dict(m) for m in (initial_messages or [])]
    turn_records: list[dict[str, Any]] = []
    stopped_at_turn: int | None = None
    stop_reason = ""

    try:
        for index, turn in enumerate(turns or []):
            if time.monotonic() - run_started > TOTAL_BUDGET_S:
                stopped_at_turn = index
                stop_reason = (
                    f"total budget of {TOTAL_BUDGET_S:.0f}s exceeded; "
                    "partial results returned"
                )
                break

            # The script's message list for this turn: an explicit override
            # replaces the history (trimming / swipe scenarios), otherwise
            # the turn's user message is appended.
            if turn.get("messages_override") is not None:
                turn_messages = [dict(m) for m in turn["messages_override"]]
            else:
                turn_messages = [*history, {"role": "user", "content": turn.get("user_message", "")}]

            cleaned, tags, jslb_scripts = _strip_tagged_messages(turn_messages)

            from app.services import cantrip as _cantrip_svc
            from app.services.cantrip import apply_prompt_strips
            from app.services.group_resolver import resolve_group_tags

            async with _cantrip_svc.async_session() as group_db:
                tags, _activated_groups, group_member_ids = await resolve_group_tags(
                    group_db, user_id, tags
                )
            if group_member_ids:
                body_json["_gitv_group_member_ids"] = group_member_ids

            req_headers = {"x-user-name": user_name} if user_name else {}
            body_json["messages"] = cleaned
            body_json["_gitv_jslb_scripts"] = jslb_scripts
            body_json["_gitv_tags"] = tags
            body_json["_gitv_turn_index"] = start_turn_index + index
            body_json["_gitv_is_regenerate"] = bool(turn.get("is_regenerate", False))
            raw_tags = [t.get("raw", "") for t in tags]

            mark = len(_cantrip_records(body_json))
            await process_cantrips(
                body_json, user_id, req_headers, tags=tags, position="pre_driver",
                only_ids=only_cantrip_ids,
                embedded_scripts=embedded_jslb_scripts(body_json, "pre_driver"),
                store_seeds=seeds,
            )
            pending_strips = body_json.pop("_gitv_prompt_strips", None)
            if pending_strips:
                apply_prompt_strips(body_json, pending_strips)
            pre_driver = [_slim_record(r) for r in _cantrip_records(body_json)[mark:]]

            # Pipeline order per turn: pre-Navigator on the raw reply, then
            # response capture (extract + strip), then post-Navigator on the
            # stripped content with this turn's tags seeded (proxy.py's
            # _apply_post_driver_processing -> verification -> capture ->
            # _apply_post_navigator_processing).
            raw_reply = turn.get("reply", "")
            mark = len(_cantrip_records(body_json))
            after_pre_navigator = await process_cantrips_post_driver(
                raw_reply, body_json, user_id, req_headers, tags=tags,
                position="pre_navigator", only_ids=only_cantrip_ids,
                embedded_scripts=embedded_jslb_scripts(body_json, "pre_navigator"),
                store_seeds=seeds,
            )
            pre_navigator = [_slim_record(r) for r in _cantrip_records(body_json)[mark:]]

            from app.services.response_tags import process_response_capture

            response_data: dict[str, Any] = {
                "choices": [{"message": {"role": "assistant", "content": after_pre_navigator}}]
            }
            await process_response_capture(response_data, user_id, body_json)
            extracted_tags = body_json.get("_gitv_response_tags") or {}
            stripped_reply = response_data["choices"][0]["message"]["content"]

            mark = len(_cantrip_records(body_json))
            final_reply = await process_cantrips_post_driver(
                stripped_reply, body_json, user_id, req_headers, tags=tags,
                position="post_navigator", only_ids=only_cantrip_ids,
                embedded_scripts=embedded_jslb_scripts(body_json, "post_navigator"),
                store_seeds=seeds,
            )
            post_navigator = [_slim_record(r) for r in _cantrip_records(body_json)[mark:]]

            # Per-turn delivery ends with the turn; the next turn re-seeds.
            body_json.pop("_gitv_response_tags", None)
            body_json.pop("_gitv_map_nominations", None)
            body_json.pop("_gitv_dynamic_lorebook_delta", None)
            body_json.pop("_gitv_prompt_strips", None)
            body_json.pop("_gitv_group_member_ids", None)

            turn_records.append({
                "index": index,
                "tags": raw_tags,
                "is_regenerate": bool(turn.get("is_regenerate", False)),
                "cantrips": {
                    "pre_driver": pre_driver,
                    "pre_navigator": pre_navigator,
                    "post_navigator": post_navigator,
                },
                "response_tags": extracted_tags,
                "reply": final_reply,
                "chat_data": copy.deepcopy(seeds["chat_data"]),
                "user_data": copy.deepcopy(seeds["user_data"]),
                "cantrip_data": copy.deepcopy(seeds["cantrip_data"]),
            })

            if turn.get("messages_override") is not None:
                history = [dict(m) for m in turn["messages_override"]]
            else:
                history = [
                    *history,
                    {"role": "user", "content": turn.get("user_message", "")},
                    {"role": "assistant", "content": final_reply},
                ]
    finally:
        await flush_metrics(body_json)

    return {
        "conversation_id": conv_id,
        "turns": turn_records,
        "stopped_at_turn": stopped_at_turn,
        "stop_reason": stop_reason,
        "final_chat_data": seeds["chat_data"],
        "final_user_data": seeds["user_data"],
        "final_cantrip_data": seeds["cantrip_data"],
    }
