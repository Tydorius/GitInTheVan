from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"<#([^#>]+)#>")

# The canonical resource types a `<#type-name#>` tag can address.
#
# Longest first, because these are matched against a hyphen-split tag and
# "memory-rule" must win over any shorter prefix. Adding a resource type means
# adding it here -- a type absent from this list parses as "unknown" and its
# tags silently never match, which is how `map` and `memory-rule` tags came to
# do nothing at all.
TAG_TYPES: tuple[str, ...] = (
    "memory-rule",
    "taggroup",
    "cantrip",
    "verify",
    "lore",
    "map",
)


def extract_tags(text: str) -> list[str]:
    if not text:
        return []
    return TAG_PATTERN.findall(text)


def strip_tags(text: str) -> str:
    if not text:
        return text
    return TAG_PATTERN.sub("", text)


def parse_tag(tag: str) -> dict[str, Any]:
    """Split `type-name` or `owner-type-name` into its parts.

    Types may themselves contain hyphens (`memory-rule`), so this matches
    against TAG_TYPES longest-first rather than assuming the type is one
    hyphen-delimited word.

    An unrecognised type yields `{"type": "unknown"}`, which never matches a
    resource. That is the intended behaviour for a typo, but it also means a
    resource type missing from TAG_TYPES is silently unaddressable.
    """
    raw = tag.strip()

    for resource_type in TAG_TYPES:
        prefix = f"{resource_type}-"
        if raw.startswith(prefix) and len(raw) > len(prefix):
            return {"type": resource_type, "name": raw[len(prefix):], "owner": None}

    # owner-type-name: the owner is everything before a recognised type.
    for resource_type in TAG_TYPES:
        marker = f"-{resource_type}-"
        idx = raw.find(marker)
        if idx > 0:
            name = raw[idx + len(marker):]
            if name:
                return {"type": resource_type, "name": name, "owner": raw[:idx]}

    return {"type": "unknown", "name": raw, "owner": None}


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
    """Decide whether one resource participates in this request.

    **This function is the Activation Hierarchy.** See the README's "Activation
    Hierarchy" section. The rules, in short:

    1. **Off is the default.** A resource participates only if something turns it
       on. Nothing here can turn a resource off.
    2. **Activation is a union.** Sources add; they never subtract. There is no
       ordering in which one source overrides another to "off", because no source
       ever produces "off" -- only "not yet on".
    3. **Proximity wins.** A tag in the persona, system prompt or message text is
       the most immediate expression of intent, so a matching tag activates the
       resource whatever its Active flag says.
    4. **The Active flag is the blanket source.** It applies when no tag has
       spoken for this resource.

    Every activation decision in the codebase must route through here. A caller
    that decides for itself -- for example by filtering `is_active` in its query
    before asking -- removes rule 3, because a resource excluded before the
    question is asked can never be activated by its tag.
    """
    for tag in tags:
        # A group tag stands in for the tags of everything in the group; the
        # group resolver rewrites it into typed tags, but the raw form is
        # tolerated here so ordering between the two cannot matter.
        if tag.get("type") != resource_type and tag.get("type") != "taggroup":
            continue

        if resource_tag and tag.get("name") == resource_tag:
            # Rule 3: an explicit tag activates regardless of the Active flag.
            if tag.get("owner"):
                return True
            if resource_is_public or resource_owner_id == current_user_id:
                return True

    # Rule 4: no tag spoke for this resource, so the blanket flag decides.
    return resource_is_active


def tag_matches_resource(
    resource_tag: str,
    resource_type: str,
    resource_is_public: bool,
    resource_owner_id: str,
    current_user_id: str,
    tags: list[dict[str, Any]],
) -> bool:
    """Rule 3 alone: did a tag in this request explicitly name this resource?

    Used by the **selection** resources -- maps and memory rules -- where only
    one can win. For those, `Active` cannot mean "blanket apply to every
    request": a map is a multi-stage pipeline, so blanket-running one would
    silently multiply every request's cost, and two active maps would race.
    Their blanket source is an explicit single-valued setting instead
    (`user_settings.default_map_id`, or the untagged default memory rule).

    This is the one documented departure from `should_activate_resource`, and it
    narrows activation rather than widening it, so it does not break rule 1.
    """
    if not resource_tag:
        return False

    for tag in tags:
        if tag.get("type") != resource_type and tag.get("type") != "taggroup":
            continue
        if tag.get("name") == resource_tag:
            if tag.get("owner"):
                return True
            if resource_is_public or resource_owner_id == current_user_id:
                return True

    return False
