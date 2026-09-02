"""What the assistant may do without asking, and what it may not do at all.

The decision is taken here and nowhere else, before the executor is reached.
`effective()` is pure: it takes the tool, its group, the user's stored
preferences, the conversation's session allowances and its yolo flag, and
returns one of `deny`, `ask`, `allow`.

Order matters and is not negotiable:

1. Deny wins over everything, from either the tool or its group, including a
   group left unset whose built-in default is Deny (`packs`).
2. Always Ask asks even under yolo -- an explicit ask is the user saying "I
   want to see this one", and yolo is a convenience, not an override.
3. Always Allow allows.
4. Normal is the tool's own risk tier: reads run, everything else asks unless
   the user already allowed that tool for this session, or yolo is on and the
   group is a content group.

Nothing here raises. An unknown mode string is a corrupt or future blob, and
degrades to Normal with a log line rather than locking the user out or
silently widening access.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from app.services.assistant.registry import GROUPS, TOOLS, Group, Risk, Tool

logger = logging.getLogger(__name__)

Mode = Literal["deny", "always_ask", "always_allow", "normal", "inherit"]
Decision = Literal["deny", "ask", "allow"]

MODES: tuple[str, ...] = ("deny", "always_ask", "always_allow", "normal", "inherit")

# Group modes may not be `inherit`: a group has no parent to inherit from.
GROUP_MODES: tuple[str, ...] = ("deny", "always_ask", "always_allow", "normal")

_FREE: frozenset[Risk] = frozenset({Risk.READ})


def _normalize(raw: Any, *, where: str) -> str | None:
    """Coerce a stored mode string. None means 'not set'."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        logger.warning("Assistant permission for %s is not a string (%r); treating as Normal", where, raw)
        return "normal"
    value = raw.strip().lower()
    if not value:
        return None
    if value not in MODES:
        logger.warning("Unknown assistant permission mode %r for %s; treating as Normal", raw, where)
        return "normal"
    return value


def parse_prefs(json_str: str | None) -> dict[str, dict[str, str]]:
    """Read the stored permission blob. Garbage degrades to 'nothing set'."""
    empty: dict[str, dict[str, str]] = {"groups": {}, "tools": {}}
    if not json_str:
        return empty
    try:
        raw = json.loads(json_str)
    except Exception:
        logger.warning("Assistant permissions blob is not valid JSON; ignoring it")
        return empty
    if not isinstance(raw, dict):
        logger.warning("Assistant permissions blob is not an object; ignoring it")
        return empty

    out: dict[str, dict[str, str]] = {"groups": {}, "tools": {}}
    for section in ("groups", "tools"):
        block = raw.get(section)
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(key, str) and isinstance(value, str):
                out[section][key] = value
    return out


def validate_prefs(prefs: dict[str, Any]) -> list[str]:
    """Error strings for anything the registry does not recognise.

    Used by `PUT /api/assistant/permissions` so a typo is a 422 rather than a
    setting that silently does nothing.
    """
    errors: list[str] = []
    if not isinstance(prefs, dict):
        return ["permissions must be an object"]

    for section in prefs:
        if section not in ("groups", "tools"):
            errors.append(f"unknown section '{section}'")

    groups = prefs.get("groups") or {}
    if not isinstance(groups, dict):
        errors.append("groups must be an object")
        groups = {}
    for key, mode in groups.items():
        if key not in GROUPS:
            errors.append(f"unknown group '{key}'")
        if not isinstance(mode, str) or mode not in GROUP_MODES:
            errors.append(f"invalid mode '{mode}' for group '{key}'")

    tools = prefs.get("tools") or {}
    if not isinstance(tools, dict):
        errors.append("tools must be an object")
        tools = {}
    for key, mode in tools.items():
        if key not in TOOLS:
            errors.append(f"unknown tool '{key}'")
        if not isinstance(mode, str) or mode not in MODES:
            errors.append(f"invalid mode '{mode}' for tool '{key}'")

    return errors


def group_mode(group: Group, prefs: dict[str, Any]) -> str:
    """The group's effective mode, falling back to its built-in default."""
    stored = _normalize((prefs.get("groups") or {}).get(group.key), where=f"group '{group.key}'")
    if stored is None or stored == "inherit":
        return group.default_mode
    return stored


def tool_mode(tool: Tool, prefs: dict[str, Any]) -> str:
    """The tool's own mode, or 'inherit' when the user has not set one."""
    stored = _normalize((prefs.get("tools") or {}).get(tool.name), where=f"tool '{tool.name}'")
    return stored or "inherit"


def effective(
    tool: Tool,
    group: Group,
    prefs: dict[str, Any],
    *,
    session_allows: set[str],
    yolo: bool,
) -> Decision:
    """Resolve one tool to `deny`, `ask` or `allow`."""
    t_mode = tool_mode(tool, prefs)
    g_mode = group_mode(group, prefs)

    # 1. Deny is absolute, from either level.
    if t_mode == "deny" or g_mode == "deny":
        return "deny"

    mode = g_mode if t_mode == "inherit" else t_mode

    # 2. An explicit ask asks, yolo or not.
    if mode == "always_ask":
        return "ask"

    # 3. An explicit allow allows.
    if mode == "always_allow":
        return "allow"

    # 4. Normal (and anything unrecognised, already normalised to Normal).
    if tool.risk in _FREE:
        return "allow"
    if tool.name in session_allows:
        return "allow"
    if yolo and group.category == "content":
        return "allow"
    return "ask"


def effective_for(
    tool: Tool,
    prefs: dict[str, Any],
    *,
    session_allows: set[str] | None = None,
    yolo: bool = False,
) -> Decision:
    """`effective()` with the group looked up from the registry."""
    group = GROUPS[tool.group]
    return effective(tool, group, prefs, session_allows=session_allows or set(), yolo=yolo)
