from __future__ import annotations

import re

from app.database import async_session  # noqa: F401 - patched by tests/conftest.py

MEMSTORE_TAG_PATTERN = re.compile(
    r"<memstore\s+key=[\"']([^\"']+)[\"']\s*>(.*?)</memstore>",
    re.DOTALL | re.IGNORECASE,
)

MEMSTORE_INLINE_PATTERN = re.compile(
    r"<memstore\s+key=[\"']([^\"']+)[\"']\s+value=[\"']([^\"']*)[\"']\s*/>",
    re.IGNORECASE,
)


def parse_memstore_tags(text: str) -> tuple[str, dict[str, str]]:
    memories: dict[str, str] = {}

    for match in MEMSTORE_TAG_PATTERN.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        memories[key] = value

    for match in MEMSTORE_INLINE_PATTERN.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        memories[key] = value

    cleaned = MEMSTORE_TAG_PATTERN.sub("", text)
    cleaned = MEMSTORE_INLINE_PATTERN.sub("", cleaned)

    return cleaned, memories


def build_memory_context_block(memories: dict[str, str]) -> str:
    if not memories:
        return ""

    lines = ["[PERSISTENT MEMORY]"]
    for key, value in sorted(memories.items()):
        lines.append(f"{key}: {value}")
    lines.append("[/PERSISTENT MEMORY]")

    return "\n".join(lines)
