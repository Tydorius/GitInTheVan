from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.user_settings import UserSettings

logger = logging.getLogger(__name__)

TOS_WARNING = (
    "WARNING: Content bypass plugins modify requests to work around provider content filters. "
    "These actions may violate your service provider's Terms of Service. "
    "Use at your own risk."
)

BYPASS_PLUGINS = {
    "none": {
        "name": "None",
        "description": "No bypass encoding. Requests are sent as-is.",
    },
    "space_separation": {
        "name": "Space Separation",
        "description": "Inserts zero-width spaces between characters in sensitive words. "
                       "Makes content less detectable by keyword-based filters while remaining "
                       "readable to the LLM.",
    },
    "dot_separation": {
        "name": "Dot Separation",
        "description": "Inserts periods between characters in sensitive words. "
                       "More aggressive than space separation.",
    },
    "character_replacement": {
        "name": "Character Replacement",
        "description": "Replaces common trigger characters with visually similar alternatives "
                       "(e.g., 'a' to 'а' Cyrillic). Most aggressive bypass.",
    },
}

SENSITIVE_PATTERNS = [
    r"\b(sex|sexual|explicit|nude|naked|nsfw)\b",
    r"\b(kill|murder|death|blood|gore|violence)\b",
    r"\b(fuck|shit|damn|bitch|asshole)\b",
    r"\b(rape|assault|molest)\b",
    r"\b(suicide|self.?harm)\b",
    r"\b(drug|cocaine|heroin|meth)\b",
]

CHAR_REPLACEMENTS = {
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с",
    "x": "х", "y": "у", "A": "А", "E": "Е", "O": "О",
}

_ZERO_WIDTH_SPACE = "\u200b"
_DOT_SEP = "."


def _encode_space_separation(text: str) -> str:
    for pattern in SENSITIVE_PATTERNS:
        text = re.sub(
            pattern,
            lambda m: _ZERO_WIDTH_SPACE.join(list(m.group(0))),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _encode_dot_separation(text: str) -> str:
    for pattern in SENSITIVE_PATTERNS:
        text = re.sub(
            pattern,
            lambda m: _DOT_SEP.join(list(m.group(0))),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _encode_character_replacement(text: str) -> str:
    result = []
    for char in text:
        result.append(CHAR_REPLACEMENTS.get(char, char))
    return "".join(result)


_ENCODERS = {
    "space_separation": _encode_space_separation,
    "dot_separation": _encode_dot_separation,
    "character_replacement": _encode_character_replacement,
}


def encode_outgoing(text: str, method: str) -> str:
    """Encode outgoing message content using the specified bypass method."""
    encoder = _ENCODERS.get(method)
    if encoder is None:
        return text
    return encoder(text)


def decode_incoming(text: str, method: str) -> str:
    """Decode incoming response content.

    Zero-width spaces and dot separations are removed. Character replacement
    is not reversed because the LLM may have naturally used those characters.
    """
    if method == "space_separation":
        return text.replace(_ZERO_WIDTH_SPACE, "")
    if method == "dot_separation":
        return re.sub(r"(?<=[a-zA-Z])\.(?=[a-zA-Z])", "", text)
    return text


async def get_bypass_method(user_id: str) -> str:
    """Get the user's configured bypass method. Returns 'none' if disabled."""
    async with async_session() as db:
        result = await db.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()
        if settings is None:
            return "none"
        return getattr(settings, "bypass_method", "none") or "none"


def apply_bypass_encode(
    body_json: dict[str, Any], method: str
) -> dict[str, Any]:
    """Apply bypass encoding to all user messages in the request."""
    if method == "none" or method not in _ENCODERS:
        return body_json

    messages = body_json.get("messages", [])
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                msg["content"] = encode_outgoing(content, method)

    logger.info("Bypass encoding applied: %s", method)
    return body_json


def apply_bypass_decode(
    response_data: dict[str, Any], method: str
) -> dict[str, Any]:
    """Apply bypass decoding to the response content."""
    if method == "none" or method not in _ENCODERS:
        return response_data

    choices = response_data.get("choices", [])
    if not choices:
        return response_data

    content = choices[0].get("message", {}).get("content", "")
    if content:
        decoded = decode_incoming(content, method)
        response_data["choices"][0]["message"]["content"] = decoded

    return response_data
