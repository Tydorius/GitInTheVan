from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    parts.append(str(text))
        return " ".join(parts)
    return str(content) if content else ""


def build_context(
    messages: list[dict[str, Any]],
    conversation_id: str = "",
    user_name: str = "User",
    character_name: str = "",
    character_description: str = "",
    first_message: str = "",
    alternate_greetings: list[str] | None = None,
) -> dict[str, Any]:
    last_messages = []
    for msg in messages:
        text = _extract_text(msg.get("content", ""))
        last_messages.append({"message": text})

    last_message = last_messages[-1]["message"] if last_messages else ""
    message_count = len(messages)
    message_created_at = datetime.now(UTC).isoformat()

    return {
        "chat": {
            "last_message": last_message,
            "last_messages": last_messages,
            "message_count": message_count,
            "conversation_id": conversation_id,
            "user_name": user_name,
            "message_created_at": message_created_at,
        },
        "character": {
            "name": character_name,
            "personality": "",
            "scenario": "",
            "example_dialogs": "",
            "description": character_description,
            "first_message": first_message,
            "alternate_greetings": alternate_greetings or [],
        },
    }


def extract_context_params(body: dict[str, Any], request_headers: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {}

    conversation_id = request_headers.get("x-conversation-id", "")
    if not conversation_id:
        conversation_id = request_headers.get("x-chat-id", "")
    params["conversation_id"] = conversation_id

    user_name = request_headers.get("x-user-name", "User")
    params["user_name"] = user_name

    return params


def apply_cantrip_result_to_messages(
    messages: list[dict[str, Any]],
    result_personality: str,
    result_scenario: str,
    result_example_dialogs: str,
) -> list[dict[str, Any]]:
    result = [msg.copy() for msg in messages]

    additions: list[tuple[str, str]] = []
    if result_scenario:
        additions.append(("scenario", result_scenario))
    if result_personality:
        additions.append(("personality", result_personality))
    if result_example_dialogs:
        additions.append(("example_dialogs", result_example_dialogs))

    if not additions:
        return result

    personality_parts = []
    scenario_parts = []
    dialog_parts = []

    for label, text in additions:
        if label == "personality":
            personality_parts.append(text)
        elif label == "scenario":
            scenario_parts.append(text)
        elif label == "example_dialogs":
            dialog_parts.append(text)

    system_idx = None
    last_user_idx = None
    for i, msg in enumerate(result):
        if msg.get("role") == "system" and system_idx is None:
            system_idx = i
        if msg.get("role") == "user":
            last_user_idx = i

    if personality_parts:
        combined = "\n".join(personality_parts)
        block = f"[Personality]\n{combined}\n[/Personality]"
        if system_idx is not None:
            result.insert(system_idx + 1, {"role": "system", "content": block})
        else:
            result.insert(0, {"role": "system", "content": block})

    if dialog_parts:
        combined = "\n".join(dialog_parts)
        block = f"[Example Dialogs]\n{combined}\n[/Example Dialogs]"
        insert_idx = last_user_idx if last_user_idx is not None else len(result)
        result.insert(insert_idx, {"role": "system", "content": block})

    if scenario_parts:
        combined = "\n".join(scenario_parts)
        block = f"[Scenario]\n{combined}\n[/Scenario]"
        insert_idx = last_user_idx if last_user_idx is not None else len(result)
        result.insert(insert_idx, {"role": "system", "content": block})

    return result
