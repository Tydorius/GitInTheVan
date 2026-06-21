
from app.models.cantrip import Cantrip
from app.services.driver_callable import (
    build_tool_notification,
    format_tool_result,
    inject_tool_notification,
    parse_call_tags,
    strip_call_tags,
)

# ============================================================================
# Call Tag Parsing
# ============================================================================

class TestParseCallTags:
    def test_simple_call(self):
        calls = parse_call_tags("Some text <call:dice_roll> more text")
        assert len(calls) == 1
        assert calls[0].name == "dice_roll"
        assert calls[0].args == {}

    def test_call_with_args(self):
        calls = parse_call_tags('<call:dice_roll count="2" sides="6">')
        assert len(calls) == 1
        assert calls[0].name == "dice_roll"
        assert calls[0].args == {"count": "2", "sides": "6"}

    def test_call_with_single_quotes(self):
        calls = parse_call_tags("<call:lookup query='dragon'>")
        assert calls[0].args == {"query": "dragon"}

    def test_multiple_calls(self):
        calls = parse_call_tags("<call:roll> text <call:lookup>")
        assert len(calls) == 2

    def test_no_calls(self):
        calls = parse_call_tags("Just regular text, no calls here.")
        assert len(calls) == 0

    def test_case_insensitive_name(self):
        calls = parse_call_tags("<call:Dice_Roll>")
        assert calls[0].name == "dice_roll"


# ============================================================================
# Call Tag Stripping
# ============================================================================

class TestStripCallTags:
    def test_strips_simple_call(self):
        result = strip_call_tags("Hello <call:dice_roll> world")
        assert "<call:" not in result
        assert "Hello" in result

    def test_preserves_other_content(self):
        result = strip_call_tags("<call:roll> The dragon attacks!")
        assert "The dragon attacks!" in result
        assert "<call:" not in result

    def test_no_tags_unchanged(self):
        text = "No tags here"
        assert strip_call_tags(text) == text


# ============================================================================
# Tool Notification Building
# ============================================================================

class TestBuildToolNotification:
    def _make_cantrip(self, name, description="A test tool"):
        return Cantrip(id="c1", user_id="u1", name=name, description=description, code="")

    def test_basic_notification(self):
        tools = [self._make_cantrip("dice_roll", "Rolls dice")]
        notif = build_tool_notification(tools, 1)
        assert "[TOOL ACCESS]" in notif
        assert "[/TOOL ACCESS]" in notif
        assert "1 turn(s) remaining" in notif
        assert "dice_roll" in notif
        assert "Rolls dice" in notif

    def test_zero_turns_returns_empty(self):
        tools = [self._make_cantrip("dice_roll")]
        assert build_tool_notification(tools, 0) == ""

    def test_no_tools_returns_empty(self):
        assert build_tool_notification([], 5) == ""

    def test_long_instructions_included(self):
        tools = [self._make_cantrip("tool")]
        tools[0].llm_instructions = "x" * 200
        notif = build_tool_notification(tools, 1)
        assert "x" * 200 in notif

    def test_includes_call_syntax_hint(self):
        tools = [self._make_cantrip("dice_roll", "Rolls dice")]
        notif = build_tool_notification(tools, 1)
        assert "<call:" in notif


# ============================================================================
# Tool Notification Injection
# ============================================================================

class TestInjectToolNotification:
    def test_appends_to_system_message(self):
        messages = [{"role": "system", "content": "You are a character."}]
        result = inject_tool_notification(messages, "[TOOL ACCESS] info [/TOOL ACCESS]")
        assert "[TOOL ACCESS]" in result[0]["content"]
        assert "You are a character." in result[0]["content"]

    def test_inserts_new_system_if_none(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_tool_notification(messages, "[TOOL ACCESS] info [/TOOL ACCESS]")
        assert result[0]["role"] == "system"
        assert "[TOOL ACCESS]" in result[0]["content"]

    def test_empty_notification_returns_original(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_tool_notification(messages, "")
        assert result == messages

    def test_replaces_existing_notification(self):
        messages = [{"role": "system", "content": "[TOOL ACCESS]\nold\n[/TOOL ACCESS]\nBase."}]
        result = inject_tool_notification(messages, "[TOOL ACCESS]\nnew\n[/TOOL ACCESS]")
        assert "new" in result[0]["content"]
        assert "old" not in result[0]["content"]
        assert "Base." in result[0]["content"]


# ============================================================================
# Tool Result Formatting
# ============================================================================

class TestFormatToolResult:
    def test_basic_format(self):
        result = format_tool_result("dice_roll", "2d6 = [3, 5] = 8")
        assert "[TOOL RESULT]" in result
        assert "[/TOOL RESULT]" in result
        assert "dice_roll" in result
        assert "2d6 = [3, 5] = 8" in result
