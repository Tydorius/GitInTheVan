from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

TOOL_OPEN_TAG = "[TOOL ACCESS]"
TOOL_CLOSE_TAG = "[/TOOL ACCESS]"
TOOL_RESULT_OPEN = "[TOOL RESULT]"
TOOL_RESULT_CLOSE = "[/TOOL RESULT]"

CALL_TAG_PATTERN = re.compile(
    r"<call:(\w+)([^>]*)>",
    re.IGNORECASE,
)
ARG_PATTERN = re.compile(r"(\w+)=[\"']([^\"']*)[\"']", re.IGNORECASE)
TAG_STRIP_PATTERN = re.compile(
    r"<call:\w+[^>]*/?>",
    re.IGNORECASE,
)


@dataclass
class ToolCall:
    name: str
    args: dict[str, str]


@dataclass
class DriverCallableConfig:
    enabled: bool
    turns: int
    tools: list[Cantrip]


def parse_call_tags(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for match in CALL_TAG_PATTERN.finditer(text):
        name = match.group(1).lower()
        args_str = match.group(2)
        args = {k.lower(): v for k, v in ARG_PATTERN.findall(args_str)}
        calls.append(ToolCall(name=name, args=args))
    return calls


def strip_call_tags(text: str) -> str:
    return TAG_STRIP_PATTERN.sub("", text)


def build_tool_notification(tools: list[Cantrip], turns_remaining: int) -> str:
    if not tools or turns_remaining <= 0:
        return ""

    lines = [TOOL_OPEN_TAG]
    lines.append(f"You have {turns_remaining} turn(s) remaining.")
    lines.append(
        "To call a tool, include the call tag in your response followed by your "
        "final answer. Example: <call:tool_name arg1=\"value\">"
    )
    lines.append("")
    lines.append("Available tools:")
    for tool in tools:
        desc = tool.llm_instructions.strip() if tool.llm_instructions else ""
        if not desc:
            desc = tool.description.strip() if tool.description else "No description"
        lines.append(f"- {tool.name.lower()}: {desc}")
    lines.append(TOOL_CLOSE_TAG)
    return "\n".join(lines)


def inject_tool_notification(
    messages: list[dict[str, Any]], notification: str
) -> list[dict[str, Any]]:
    if not notification:
        return messages

    result = [msg.copy() for msg in messages]
    for msg in result:
        content = msg.get("content", "")
        if isinstance(content, str) and TOOL_OPEN_TAG in content:
            msg["content"] = re.sub(
                re.escape(TOOL_OPEN_TAG) + r".*?" + re.escape(TOOL_CLOSE_TAG),
                notification,
                content,
                flags=re.DOTALL,
            )
            return result

    system_idx = None
    for i, msg in enumerate(result):
        if msg.get("role") == "system":
            system_idx = i
            break

    if system_idx is not None:
        result[system_idx] = {
            **result[system_idx],
            "content": result[system_idx]["content"] + "\n\n" + notification,
        }
    else:
        result.insert(0, {"role": "system", "content": notification})

    return result


def format_tool_result(tool_name: str, result_text: str) -> str:
    return f"{TOOL_RESULT_OPEN}\n{tool_name}: {result_text}\n{TOOL_RESULT_CLOSE}"


async def load_driver_callable_config(
    db: AsyncSession, user_id: str, tags: list | None = None
) -> DriverCallableConfig:
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    user_settings = settings_result.scalar_one_or_none()
    if user_settings is None:
        return DriverCallableConfig(enabled=False, turns=0, tools=[])

    turns = user_settings.driver_callable_turns
    if turns <= 0:
        return DriverCallableConfig(enabled=False, turns=0, tools=[])

    result = await db.execute(
        select(Cantrip).where(
            Cantrip.user_id == user_id,
            Cantrip.is_active.is_(True),
            Cantrip.run_driver_callable.is_(True),
        ).order_by(Cantrip.execution_order, Cantrip.created_at)
    )
    all_tools = list(result.scalars().all())

    if not all_tools:
        return DriverCallableConfig(enabled=False, turns=0, tools=[])

    from app.services.tagging import should_activate_resource

    tools = []
    for t in all_tools:
        if t.tag and tags:
            if should_activate_resource(
                t.tag, "cantrip", t.is_active, t.is_public, t.user_id, user_id, tags
            ):
                tools.append(t)
        else:
            tools.append(t)

    if not tools:
        return DriverCallableConfig(enabled=False, turns=0, tools=[])

    return DriverCallableConfig(enabled=True, turns=turns, tools=tools)


async def execute_tool_call(
    tool: Cantrip,
    call: ToolCall,
    messages: list[dict[str, Any]],
    request_headers: dict[str, str],
    user_id: str,
    conversation_id: str = "",
) -> str:
    from app.services.cantrip import (
        _load_cantrip_data,
        _load_chat_data,
        _load_user_data,
        _save_cantrip_data,
        _save_chat_data,
        _save_user_data,
    )
    from app.services.cantrip_context import build_context
    from app.services.deno_runner import run_cantrip

    params: dict[str, Any] = {}
    params["conversation_id"] = conversation_id or request_headers.get("x-conversation-id", "")
    params["user_name"] = request_headers.get("x-user-name", "User")

    context = build_context(messages, **params)
    context["tool_call"] = {"name": call.name, "args": call.args}
    context["tool_result"] = ""

    async with async_session() as db:
        chat_data = await _load_chat_data(db, user_id, params["conversation_id"])
        user_data = await _load_user_data(db, user_id)
        cantrip_data = await _load_cantrip_data(db, user_id, tool.id)

        try:
            result = await run_cantrip(
                code=tool.code,
                context=context,
                chat_data=chat_data,
                user_data=user_data,
                cantrip_data=cantrip_data,
                timeout_ms=tool.timeout_ms,
            )
        except Exception:
            logger.exception("Driver-callable cantrip '%s' failed", tool.name)
            return "Error: tool execution failed"

        if result.has_error:
            logger.warning("Driver-callable cantrip '%s' error: %s", tool.name, result.error)
            return f"Error: {result.error}"

        if params["conversation_id"]:
            await _save_chat_data(db, user_id, params["conversation_id"], result.chat_data)
        await _save_user_data(db, user_id, result.user_data)
        if result.cantrip_data != cantrip_data:
            await _save_cantrip_data(db, user_id, tool.id, result.cantrip_data)

    tool_result = ""
    if hasattr(result, "tool_result") and result.tool_result:
        tool_result = result.tool_result
    elif result.scenario:
        tool_result = result.scenario
    elif result.personality:
        tool_result = result.personality
    else:
        tool_result = "Tool completed (no output)"

    return tool_result.strip()


def extract_tool_result_content(content: str) -> tuple[str, str]:
    """Extract tool result text and return (cleaned_content, result_text).

    cleaned_content has tool result blocks removed.
    result_text is the content of the last tool result block.
    """
    result_blocks = re.findall(
        re.escape(TOOL_RESULT_OPEN) + r"(.*?)" + re.escape(TOOL_RESULT_CLOSE),
        content,
        re.DOTALL,
    )
    result_text = result_blocks[-1].strip() if result_blocks else ""
    cleaned = re.sub(
        re.escape(TOOL_RESULT_OPEN) + r".*?" + re.escape(TOOL_RESULT_CLOSE),
        "",
        content,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, result_text
