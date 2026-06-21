from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"<#([^#>]+)#>")


def extract_tags(text: str) -> list[str]:
    if not text:
        return []
    return TAG_PATTERN.findall(text)


def strip_tags(text: str) -> str:
    if not text:
        return text
    return TAG_PATTERN.sub("", text)


def parse_tag(tag: str) -> dict[str, Any]:
    parts = tag.strip().split("-")

    if len(parts) >= 3 and parts[0] in ("lore", "cantrip", "verify", "taggroup"):
        resource_type = parts[0]
        resource_name = "-".join(parts[1:])
        return {"type": resource_type, "name": resource_name, "owner": None}

    if len(parts) >= 4 and parts[1] in ("lore", "cantrip", "verify", "taggroup"):
        owner = parts[0]
        resource_type = parts[1]
        resource_name = "-".join(parts[2:])
        return {"type": resource_type, "name": resource_name, "owner": owner}

    if len(parts) == 2 and parts[0] in ("lore", "cantrip", "verify", "taggroup"):
        return {"type": parts[0], "name": parts[1], "owner": None}

    return {"type": "unknown", "name": tag, "owner": None}


def extract_all_tags_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    all_tags: list[dict[str, Any]] = []
    seen: set[str] = set()

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text_parts.append(str(item.get("text", "")))
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            content = str(content) if content else ""

        raw_tags = extract_tags(content)
        for raw in raw_tags:
            if raw not in seen:
                seen.add(raw)
                parsed = parse_tag(raw)
                parsed["raw"] = raw
                all_tags.append(parsed)

    return all_tags


def should_activate_resource(
    resource_tag: str,
    resource_type: str,
    resource_is_active: bool,
    resource_is_public: bool,
    resource_owner_id: str,
    current_user_id: str,
    tags: list[dict[str, Any]],
) -> bool:
    if not resource_tag:
        return resource_is_active

    for tag in tags:
        if tag.get("type") != resource_type and tag.get("type") != "taggroup":
            continue

        if tag.get("name") == resource_tag:
            if tag.get("owner"):
                return True
            if resource_is_public or resource_owner_id == current_user_id:
                return True

    return resource_is_active
