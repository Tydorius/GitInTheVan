from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.memory import Memory
from app.services.messages import extract_text, strip_from_messages

logger = logging.getLogger(__name__)

COMMAND_TAG_PATTERN = re.compile(
    r"<(VERIFY|SUMMARY|FORBIDDEN|MEMORY|DRIVER|MAP)"
    r":(on|off|reset)"
    r"(?::(persist))?>",
    re.IGNORECASE,
)

COMMAND_TAG_STRIP_PATTERN = re.compile(
    r"<(?:VERIFY|SUMMARY|FORBIDDEN|MEMORY|DRIVER|MAP)"
    r":(?:on|off|reset)"
    r"(?::(?:persist))?>",
    re.IGNORECASE,
)

VALID_COMMANDS = {"VERIFY", "SUMMARY", "FORBIDDEN", "MEMORY", "DRIVER", "MAP"}
PERSIST_PREFIX = "__cmd_persist_"

# Phase 29 action commands: unlike the boolean switches above, these carry a
# free-text argument (`<LEXICON:Ironhold>`, `<CANTRIP:a dice roller>`). The
# argument may not contain angle brackets; first occurrence per command wins,
# matching switch-command behaviour.
ACTION_COMMAND_PATTERN = re.compile(r"<(LEXICON|CANTRIP):([^<>]*)>", re.IGNORECASE)

# OOC notices GitV appends to replies so the player sees a pipeline result.
# Only notices carrying this marker are stripped from later requests -- a
# blanket "[OOC: ...]" strip would eat player-authored OOC notes, which are
# common in roleplay and must reach the model.
GITV_NOTICE_PATTERN = re.compile(r"\[OOC: GitV:[^\]]*\]")


@dataclass
class ParsedCommand:
    command: str
    setting: str
    persist: bool

    @property
    def key(self) -> str:
        return self.command.upper()


@dataclass
class ParsedActionCommand:
    command: str
    text: str

    @property
    def key(self) -> str:
        return self.command.upper()


def parse_command_tags(text: str) -> list[ParsedCommand]:
    if not text:
        return []
    commands = []
    seen: set[str] = set()

    for match in COMMAND_TAG_PATTERN.finditer(text):
        cmd = match.group(1).upper()
        setting = match.group(2).lower()
        persist = match.group(3) is not None

        if cmd in seen and not persist:
            continue
        seen.add(cmd)
        commands.append(ParsedCommand(command=cmd, setting=setting, persist=persist))

    return commands


def extract_command_tags_from_messages(
    messages: list[dict[str, Any]],
) -> list[ParsedCommand]:
    all_commands: list[ParsedCommand] = []
    seen: set[str] = set()

    for msg in messages:
        content = extract_text(msg.get("content", ""))

        for cmd in parse_command_tags(content):
            if cmd.command not in seen or cmd.persist:
                seen.add(cmd.command)
                all_commands.append(cmd)

    return all_commands


def parse_action_commands(text: str) -> list[ParsedActionCommand]:
    if not text:
        return []
    commands: list[ParsedActionCommand] = []
    seen: set[str] = set()
    for match in ACTION_COMMAND_PATTERN.finditer(text):
        cmd = match.group(1).upper()
        if cmd in seen:
            continue
        seen.add(cmd)
        commands.append(ParsedActionCommand(command=cmd, text=match.group(2).strip()))
    return commands


def extract_action_commands_from_messages(
    messages: list[dict[str, Any]],
) -> list[ParsedActionCommand]:
    all_actions: list[ParsedActionCommand] = []
    seen: set[str] = set()
    for msg in messages:
        content = extract_text(msg.get("content", ""))
        for action in parse_action_commands(content):
            if action.command not in seen:
                seen.add(action.command)
                all_actions.append(action)
    return all_actions


def strip_action_commands(text: str) -> str:
    if not text:
        return text
    return ACTION_COMMAND_PATTERN.sub("", text)


def strip_gitv_notices(text: str) -> str:
    if not text:
        return text
    return GITV_NOTICE_PATTERN.sub("", text)


_strip_from_messages = strip_from_messages


def strip_command_tags(text: str) -> str:
    if not text:
        return text
    return COMMAND_TAG_STRIP_PATTERN.sub("", text)


def strip_command_tags_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _strip_from_messages(messages, strip_command_tags)


def strip_action_commands_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _strip_from_messages(messages, strip_action_commands)


def strip_gitv_notices_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _strip_from_messages(messages, strip_gitv_notices)


async def load_persistent_overrides(
    db: AsyncSession, user_id: str, internal_chat_id: str
) -> dict[str, str]:
    """Load persistent command overrides from memory table.

    Returns dict mapping command name (uppercase) to setting ('on'/'off').
    """
    if not internal_chat_id:
        return {}

    result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == internal_chat_id,
        )
    )
    rows = result.scalars().all()

    overrides: dict[str, str] = {}
    for row in rows:
        if row.key.startswith(PERSIST_PREFIX):
            cmd_name = row.key[len(PERSIST_PREFIX):].upper()
            if cmd_name in VALID_COMMANDS:
                overrides[cmd_name] = row.value

    return overrides


async def save_persistent_overrides(
    db: AsyncSession,
    user_id: str,
    internal_chat_id: str,
    overrides: dict[str, str],
) -> None:
    """Save persistent command overrides to memory table."""
    if not internal_chat_id or not overrides:
        return

    existing_result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == internal_chat_id,
        )
    )
    existing = {row.key: row for row in existing_result.scalars().all()}

    for cmd_name, setting in overrides.items():
        key = f"{PERSIST_PREFIX}{cmd_name.lower()}"
        if key in existing:
            if existing[key].value != setting:
                existing[key].value = setting
        else:
            db.add(Memory(
                user_id=user_id,
                conversation_id=internal_chat_id,
                key=key,
                value=setting,
                memory_type="command_override",
            ))

    await db.commit()


async def delete_persistent_overrides(
    db: AsyncSession, user_id: str, internal_chat_id: str, command: str
) -> None:
    """Delete a persistent override (reset)."""
    if not internal_chat_id:
        return

    key = f"{PERSIST_PREFIX}{command.lower()}"
    result = await db.execute(
        select(Memory).where(
            Memory.user_id == user_id,
            Memory.conversation_id == internal_chat_id,
            Memory.key == key,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()


@dataclass
class ResolvedOverrides:
    verify: bool | None = None
    summary: bool | None = None
    forbidden: bool | None = None
    memory: bool | None = None
    driver_callable: bool | None = None
    map: bool | None = None

    def get(self, command: str) -> bool | None:
        mapping = {
            "VERIFY": self.verify,
            "SUMMARY": self.summary,
            "FORBIDDEN": self.forbidden,
            "MEMORY": self.memory,
            "DRIVER": self.driver_callable,
            "MAP": self.map,
        }
        return mapping.get(command.upper())

    def to_dict(self) -> dict[str, Any]:
        return {
            "verify": self.verify,
            "summary": self.summary,
            "forbidden": self.forbidden,
            "memory": self.memory,
            "driver_callable": self.driver_callable,
            "map": self.map,
        }


async def resolve_command_overrides(
    user_id: str,
    internal_chat_id: str,
    message_commands: list[ParsedCommand],
) -> ResolvedOverrides:
    """Resolve final override state using precedence:
    one-off > persistent > GUI (None = use GUI setting).

    One-off commands (without :persist) create a temporary override for this request only.
    Persistent commands (with :persist) are stored and applied until reset.
    Reset clears the persistent override.
    """
    resolved = ResolvedOverrides()
    cmd_field_map = {
        "VERIFY": "verify",
        "SUMMARY": "summary",
        "FORBIDDEN": "forbidden",
        "MEMORY": "memory",
        "DRIVER": "driver_callable",
        "MAP": "map",
    }

    async with async_session() as db:
        persistent = await load_persistent_overrides(db, user_id, internal_chat_id)

        new_persist: dict[str, str] = {}
        resets: set[str] = set()
        one_offs: dict[str, str] = {}

        for cmd in message_commands:
            if cmd.setting == "reset":
                resets.add(cmd.command)
                continue

            if cmd.persist:
                new_persist[cmd.command] = cmd.setting
            else:
                one_offs[cmd.command] = cmd.setting

        for cmd_name in resets:
            if cmd_name in new_persist:
                del new_persist[cmd_name]
            if cmd_name in persistent:
                await delete_persistent_overrides(db, user_id, internal_chat_id, cmd_name)

        if new_persist:
            await save_persistent_overrides(db, user_id, internal_chat_id, new_persist)
            persistent.update(new_persist)

        for cmd_name in resets:
            persistent.pop(cmd_name, None)

        for cmd_name, setting in persistent.items():
            if cmd_name in resets:
                continue
            field = cmd_field_map.get(cmd_name)
            if field:
                setattr(resolved, field, setting == "on")

        for cmd_name, setting in one_offs.items():
            field = cmd_field_map.get(cmd_name)
            if field:
                setattr(resolved, field, setting == "on")

    return resolved
