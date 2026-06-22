import pytest

from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.command_tags import (
    ParsedCommand,
    extract_command_tags_from_messages,
    parse_command_tags,
    resolve_command_overrides,
    strip_command_tags,
    strip_command_tags_from_messages,
)
from tests.conftest import TestSessionLocal

# ============================================================================
# Parsing
# ============================================================================

class TestParseCommandTags:
    def test_simple_on(self):
        cmds = parse_command_tags("Hello <VERIFY:on>")
        assert len(cmds) == 1
        assert cmds[0].command == "VERIFY"
        assert cmds[0].setting == "on"
        assert cmds[0].persist is False

    def test_simple_off(self):
        cmds = parse_command_tags("<SUMMARY:off>")
        assert cmds[0].command == "SUMMARY"
        assert cmds[0].setting == "off"

    def test_persist(self):
        cmds = parse_command_tags("<VERIFY:off:persist>")
        assert cmds[0].persist is True

    def test_reset(self):
        cmds = parse_command_tags("<VERIFY:reset>")
        assert cmds[0].setting == "reset"

    def test_multiple(self):
        cmds = parse_command_tags("<VERIFY:off> <SUMMARY:on:persist>")
        assert len(cmds) == 2
        assert cmds[0].command == "VERIFY"
        assert cmds[1].command == "SUMMARY"

    def test_case_insensitive(self):
        cmds = parse_command_tags("<verify:OFF:PERSIST>")
        assert cmds[0].command == "VERIFY"
        assert cmds[0].setting == "off"
        assert cmds[0].persist is True

    def test_no_commands(self):
        assert parse_command_tags("Just regular text") == []

    def test_all_valid_commands(self):
        for cmd in ["VERIFY", "SUMMARY", "FORBIDDEN", "MEMORY", "DRIVER"]:
            parsed = parse_command_tags(f"<{cmd}:on>")
            assert len(parsed) == 1
            assert parsed[0].command == cmd

    def test_duplicate_one_off_first_wins(self):
        cmds = parse_command_tags("<VERIFY:off> <VERIFY:on>")
        assert len(cmds) == 1
        assert cmds[0].setting == "off"

    def test_duplicate_persist_overrides_one_off(self):
        cmds = parse_command_tags("<VERIFY:off> <VERIFY:on:persist>")
        assert len(cmds) == 2


# ============================================================================
# Stripping
# ============================================================================

class TestStripCommandTags:
    def test_strips_simple(self):
        result = strip_command_tags("Hello <VERIFY:off> world")
        assert "<VERIFY" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strips_persist(self):
        result = strip_command_tags("Text <SUMMARY:off:persist> end")
        assert "<SUMMARY" not in result
        assert "Text" in result

    def test_strips_reset(self):
        result = strip_command_tags("<VERIFY:reset>")
        assert result.strip() == ""

    def test_preserves_regular_text(self):
        assert strip_command_tags("No commands here") == "No commands here"

    def test_strips_from_messages(self):
        messages = [
            {"role": "user", "content": "Hello <VERIFY:off>"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = strip_command_tags_from_messages(messages)
        assert "<VERIFY" not in result[0]["content"]
        assert "Hello" in result[0]["content"]


# ============================================================================
# Extraction from Messages
# ============================================================================

class TestExtractFromMessages:
    def test_extracts_from_user_message(self):
        messages = [{"role": "user", "content": "Hello <VERIFY:off>"}]
        cmds = extract_command_tags_from_messages(messages)
        assert len(cmds) == 1
        assert cmds[0].command == "VERIFY"

    def test_extracts_from_list_content(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "<SUMMARY:on:persist>"}]}]
        cmds = extract_command_tags_from_messages(messages)
        assert len(cmds) == 1
        assert cmds[0].command == "SUMMARY"
        assert cmds[0].persist is True


# ============================================================================
# Resolve: Precedence (one-off > persistent > GUI)
# ============================================================================

class TestResolveOverrides:
    @pytest.fixture
    async def setup_user(self):
        async with TestSessionLocal() as s:
            s.add(User(id="u-cmd", username="cmdtest", password_hash="x", gitv_api_key="key-cmd", is_admin=False))
            await s.commit()
            s.add(UserSettings(user_id="u-cmd"))
            await s.commit()

    @pytest.mark.asyncio
    async def test_no_commands_returns_defaults(self, setup_user):
        resolved = await resolve_command_overrides("u-cmd", "chat-1", [])
        assert resolved.verify is None
        assert resolved.summary is None

    @pytest.mark.asyncio
    async def test_one_off_override(self, setup_user):
        cmds = [ParsedCommand(command="VERIFY", setting="off", persist=False)]
        resolved = await resolve_command_overrides("u-cmd", "chat-1", cmds)
        assert resolved.verify is False

    @pytest.mark.asyncio
    async def test_persist_saved_and_loaded(self, setup_user):
        cmds = [ParsedCommand(command="VERIFY", setting="off", persist=True)]
        resolved = await resolve_command_overrides("u-cmd", "chat-2", cmds)
        assert resolved.verify is False

        resolved2 = await resolve_command_overrides("u-cmd", "chat-2", [])
        assert resolved2.verify is False

    @pytest.mark.asyncio
    async def test_one_off_overrides_persistent(self, setup_user):
        persist_cmd = [ParsedCommand(command="VERIFY", setting="off", persist=True)]
        await resolve_command_overrides("u-cmd", "chat-3", persist_cmd)

        one_off = [ParsedCommand(command="VERIFY", setting="on", persist=False)]
        resolved = await resolve_command_overrides("u-cmd", "chat-3", one_off)
        assert resolved.verify is True

        resolved2 = await resolve_command_overrides("u-cmd", "chat-3", [])
        assert resolved2.verify is False

    @pytest.mark.asyncio
    async def test_reset_clears_persistent(self, setup_user):
        persist_cmd = [ParsedCommand(command="SUMMARY", setting="off", persist=True)]
        await resolve_command_overrides("u-cmd", "chat-4", persist_cmd)

        reset_cmd = [ParsedCommand(command="SUMMARY", setting="reset", persist=False)]
        resolved = await resolve_command_overrides("u-cmd", "chat-4", reset_cmd)
        assert resolved.summary is None

    @pytest.mark.asyncio
    async def test_multiple_commands(self, setup_user):
        cmds = [
            ParsedCommand(command="VERIFY", setting="off", persist=False),
            ParsedCommand(command="SUMMARY", setting="on", persist=True),
            ParsedCommand(command="MEMORY", setting="off", persist=False),
        ]
        resolved = await resolve_command_overrides("u-cmd", "chat-5", cmds)
        assert resolved.verify is False
        assert resolved.summary is True
        assert resolved.memory is False

    @pytest.mark.asyncio
    async def test_persist_different_chats_isolated(self, setup_user):
        cmds_a = [ParsedCommand(command="VERIFY", setting="off", persist=True)]
        await resolve_command_overrides("u-cmd", "chat-a", cmds_a)

        resolved_b = await resolve_command_overrides("u-cmd", "chat-b", [])
        assert resolved_b.verify is None

    @pytest.mark.asyncio
    async def test_gui_setting_used_when_no_override(self, setup_user):
        resolved = await resolve_command_overrides("u-cmd", "chat-6", [])
        assert resolved.verify is None
        assert resolved.forbidden is None
        assert resolved.driver_callable is None
