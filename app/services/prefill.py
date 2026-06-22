from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PREFILL_PROVIDERS = {
    "openai": "openai",
    "openrouter": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "mistral": "openai",
    "groq": "openai",
    "together": "openai",
    "generic": "openai",
}


def detect_provider(base_url: str, model: str = "") -> str:
    """Detect the provider type from the endpoint URL or model name."""
    url_lower = base_url.lower()

    if "openrouter.ai" in url_lower:
        return "openai"
    if "anthropic.com" in url_lower:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url_lower or "gemini" in model.lower():
        return "google"
    if "mistral.ai" in url_lower:
        return "openai"
    if "groq.com" in url_lower:
        return "openai"
    if "together.xyz" in url_lower or "together.ai" in url_lower:
        return "openai"

    return "openai"


def has_trailing_assistant(messages: list[dict[str, Any]]) -> bool:
    """Check if the last message is an assistant message (prefill pattern)."""
    if not messages:
        return False
    return messages[-1].get("role") == "assistant"


def normalize_prefill(
    body_json: dict[str, Any],
    base_url: str,
    enabled: bool = False,
) -> dict[str, Any]:
    """Normalize assistant message prefilling for the target provider.

    Some providers (Anthropic, Google) support assistant prefill — a trailing
    assistant message that the model continues from. OpenAI-compatible APIs
    typically ignore or drop trailing assistant messages.

    When enabled, this function:
    - For OpenAI-compatible providers: converts trailing assistant to a
      system instruction so the content isn't lost
    - For Anthropic/Google: passes through as-is (native support)
    """
    if not enabled:
        return body_json

    messages = body_json.get("messages", [])
    if not has_trailing_assistant(messages):
        return body_json

    model = body_json.get("model", "")
    provider = detect_provider(base_url, model)

    if provider in ("anthropic", "google"):
        return body_json

    if provider == "openai":
        trailing = messages[-1]
        prefill_content = trailing.get("content", "")

        if not prefill_content.strip():
            return body_json

        body_json["messages"] = messages[:-1]

        system_idx = None
        for i, msg in enumerate(body_json["messages"]):
            if msg.get("role") == "system":
                system_idx = i
                break

        prefill_block = f"[ASSISTANT PREFILL]\nThe assistant's response should begin with the following text. Continue naturally from this point:\n{prefill_content}\n[/ASSISTANT PREFILL]"

        if system_idx is not None:
            body_json["messages"][system_idx] = {
                **body_json["messages"][system_idx],
                "content": body_json["messages"][system_idx]["content"] + "\n\n" + prefill_block,
            }
        else:
            body_json["messages"].insert(0, {"role": "system", "content": prefill_block})

        logger.info("Prefill normalized: converted trailing assistant message to system instruction")
        return body_json

    return body_json
