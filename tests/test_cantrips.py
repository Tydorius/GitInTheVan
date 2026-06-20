import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.cantrip_context import apply_cantrip_result_to_messages, build_context
from app.services.deno_runner import DENO_PATH, run_cantrip

JANITOR_SCRIPTS_DIR = Path(r"E:\github\JanitorScripts\JanitorAI_Scripts")

SKIP_DENO = not DENO_PATH

pytestmark = pytest.mark.skipif(SKIP_DENO, reason="Deno not available")


# ============================================================================
# Helpers
# ============================================================================

def _load_script(name: str) -> str:
    return (JANITOR_SCRIPTS_DIR / name).read_text(encoding="utf-8")


def _make_context(
    last_message="Hello",
    message_count=1,
    conversation_id="test-conv",
    character_name="TestChar",
    character_description="A test character",
):
    messages = [{"role": "user", "content": last_message}]
    for i in range(message_count - 1):
        messages.insert(0, {"role": "assistant" if i % 2 == 0 else "user", "content": f"Earlier message {i+1}"})
    return build_context(
        messages=messages,
        conversation_id=conversation_id,
        user_name="TestUser",
        character_name=character_name,
        character_description=character_description,
        first_message="Greetings!",
    )


# ============================================================================
# Deno Runner: Basic execution
# ============================================================================

class TestDenoRunnerBasic:
    @pytest.mark.asyncio
    async def test_simple_scenario_append(self):
        code = 'context.character.scenario += " The world is vast.";'
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "The world is vast." in result.scenario

    @pytest.mark.asyncio
    async def test_simple_personality_append(self):
        code = 'context.character.personality += ", brave and bold";'
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "brave and bold" in result.personality

    @pytest.mark.asyncio
    async def test_simple_example_dialogs_append(self):
        code = 'context.character.example_dialogs += "\\n<START>\\n{{user}}: Hi\\n{{char}}: Hello!";'
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "Hello!" in result.example_dialogs

    @pytest.mark.asyncio
    async def test_console_log_captured(self):
        code = 'console.log("debug message"); console.log("second line");'
        result = await run_cantrip(code, _make_context())
        assert "debug message" in result.debug_logs
        assert "second line" in result.debug_logs

    @pytest.mark.asyncio
    async def test_no_modifications(self):
        code = 'const x = 1 + 1;'
        result = await run_cantrip(code, _make_context())
        assert result.personality == ""
        assert result.scenario == ""
        assert result.example_dialogs == ""
        assert not result.has_modifications


# ============================================================================
# Deno Runner: JanitorAI compatibility patterns
# ============================================================================

class TestJanitorAICompatibility:
    @pytest.mark.asyncio
    async def test_use_worker_directive(self):
        code = '"use worker";\ncontext.character.scenario += " Worker active.";'
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "Worker active." in result.scenario

    @pytest.mark.asyncio
    async def test_return_early(self):
        code = (
            'const lastMessage = context.chat.last_message.toLowerCase();\n'
            'if (!lastMessage.includes("magic")) {\n'
            '    return;\n'
            '}\n'
            'context.character.scenario += " Magic detected!";\n'
            '}'
        )
        result_no_keyword = await run_cantrip(code, _make_context(last_message="Hello world"))
        assert result_no_keyword.scenario == ""

        result_with_keyword = await run_cantrip(code, _make_context(last_message="cast magic spell"))
        assert "Magic detected!" in result_with_keyword.scenario

    @pytest.mark.asyncio
    async def test_context_guards_pattern(self):
        code = (
            'context.character = context.character || {};\n'
            'context.character.personality = context.character.personality || "";\n'
            'context.character.scenario = context.character.scenario || "";\n'
            'context.character.personality += ", guarded";'
        )
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "guarded" in result.personality

    @pytest.mark.asyncio
    async def test_last_messages_access(self):
        code = (
            'const messages = context.chat.last_messages || [];\n'
            'const allText = messages.map(m => m.message).join(" ");\n'
            'console.log("Messages: " + allText);\n'
            'context.character.scenario += " Saw " + messages.length + " messages.";'
        )
        result = await run_cantrip(code, _make_context())
        assert "1 messages" in result.scenario

    @pytest.mark.asyncio
    async def test_message_count_access(self):
        code = (
            'const count = context.chat.message_count;\n'
            'context.character.scenario += " Count is " + count;'
        )
        result = await run_cantrip(code, _make_context(message_count=5))
        assert "Count is 5" in result.scenario

    @pytest.mark.asyncio
    async def test_user_name_access(self):
        code = (
            'const userName = context.chat.user_name;\n'
            'context.character.personality += ", knows " + userName;'
        )
        result = await run_cantrip(code, _make_context())
        assert "TestUser" in result.personality

    @pytest.mark.asyncio
    async def test_character_name_readonly(self):
        code = (
            'const name = context.character.name;\n'
            'context.character.scenario += " Character: " + name;'
        )
        result = await run_cantrip(code, _make_context(character_name="Aria"))
        assert "Character: Aria" in result.scenario

    @pytest.mark.asyncio
    async def test_stat_parsing_pattern(self):
        code = (
            'function getStat(statName, lastResponse) {\n'
            '    const regex = new RegExp(`\\\\*\\\\*${statName}:\\\\*\\\\*\\\\s*(\\\\d+)\\\\s*%?`, "i");\n'
            '    const match = lastResponse.match(regex);\n'
            '    if (match && match[1]) return parseInt(match[1], 10);\n'
            '    return null;\n'
            '}\n'
            'const day = getStat("Day", context.chat.last_message);\n'
            'if (day !== null && day === 7) {\n'
            '    context.character.scenario += " A week has passed.";\n'
            '}'
        )
        ctx = _make_context(last_message="**Day:** 7")
        result = await run_cantrip(code, ctx)
        assert "A week has passed." in result.scenario

    @pytest.mark.asyncio
    async def test_keyword_detection_pattern(self):
        code = (
            'const keywords = ["magic", "arcane", "spell"];\n'
            'const found = keywords.some(kw => context.chat.last_message.toLowerCase().includes(kw));\n'
            'if (found) {\n'
            '    context.character.scenario += " Arcane energies detected.";\n'
            '}'
        )
        result = await run_cantrip(code, _make_context(last_message="I cast a spell"))
        assert "Arcane energies detected." in result.scenario

    @pytest.mark.asyncio
    async def test_zero_width_encoding_pattern(self):
        code = (
            'const ZW_MAP = {"0": "\\u200B", "1": "\\u200C", "2": "\\u200D"};\n'
            'const REVERSE_MAP = {"\\u200B": "0", "\\u200C": "1", "\\u200D": "2"};\n'
            'const encoded = "12".split("").map(c => ZW_MAP[c] || "").join("");\n'
            'const decoded = encoded.split("").map(c => REVERSE_MAP[c] || "").join("");\n'
            'console.log("Encoded length: " + encoded.length);\n'
            'console.log("Decoded: " + decoded);\n'
            'context.character.scenario += " State: " + decoded;'
        )
        result = await run_cantrip(code, _make_context())
        assert result.error is None
        assert "State: 12" in result.scenario
        assert any("Encoded length: 2" in log for log in result.debug_logs)


# ============================================================================
# Deno Runner: chat_data extension
# ============================================================================

class TestChatData:
    @pytest.mark.asyncio
    async def test_chat_data_set_and_get(self):
        code = (
            'const day = context.chat_data.get("day") || 1;\n'
            'context.chat_data.set("day", day + 1);\n'
            'context.character.scenario += " Day " + day + " begins.";'
        )
        result = await run_cantrip(code, _make_context())
        assert "Day 1 begins." in result.scenario
        assert result.chat_data.get("day") == 2

    @pytest.mark.asyncio
    async def test_chat_data_with_existing_values(self):
        code = (
            'const count = context.chat_data.get("count") || 0;\n'
            'context.chat_data.set("count", count + 1);\n'
            'context.character.scenario += " Count: " + (count + 1);'
        )
        result = await run_cantrip(
            code, _make_context(),
            chat_data={"count": 5},
        )
        assert "Count: 6" in result.scenario
        assert result.chat_data["count"] == 6

    @pytest.mark.asyncio
    async def test_chat_data_keys(self):
        code = (
            'const keys = context.chat_data.keys();\n'
            'console.log("Keys: " + keys.join(", "));'
        )
        result = await run_cantrip(
            code, _make_context(),
            chat_data={"a": 1, "b": 2},
        )
        assert any("Keys: a, b" in log for log in result.debug_logs)

    @pytest.mark.asyncio
    async def test_chat_data_delete(self):
        code = (
            'context.chat_data.delete("temp");\n'
            'const keys = context.chat_data.keys();\n'
            'console.log("After delete: " + keys.length + " keys");'
        )
        result = await run_cantrip(
            code, _make_context(),
            chat_data={"temp": "x", "perm": "y"},
        )
        assert "temp" not in result.chat_data
        assert "perm" in result.chat_data

    @pytest.mark.asyncio
    async def test_chat_data_get_missing_returns_null(self):
        code = (
            'const val = context.chat_data.get("nonexistent");\n'
            'console.log("Missing value type: " + typeof val);\n'
            'console.log("Missing value: " + val);'
        )
        result = await run_cantrip(code, _make_context())
        assert any("Missing value: null" in log for log in result.debug_logs)


# ============================================================================
# Deno Runner: Error handling and sandboxing
# ============================================================================

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_syntax_error_caught(self):
        code = 'const x = ;'
        result = await run_cantrip(code, _make_context())
        assert result.has_error
        assert "SyntaxError" in result.error or "Unexpected" in result.error

    @pytest.mark.asyncio
    async def test_runtime_error_caught(self):
        code = 'const x = undefinedVar.property;'
        result = await run_cantrip(code, _make_context())
        assert result.has_error

    @pytest.mark.asyncio
    async def test_infinite_loop_timeout(self):
        code = 'while(true) {}'
        from app.services.deno_runner import CantripTimeoutError
        with pytest.raises(CantripTimeoutError):
            await run_cantrip(code, _make_context(), timeout_ms=2000)

    @pytest.mark.asyncio
    async def test_sandbox_no_network(self):
        code = (
            'try {\n'
            '    Deno.listen({port: 0});\n'
            '    context.character.scenario += " NETWORK_ALLOWED";\n'
            '} catch(e) {\n'
            '    context.character.scenario += " NETWORK_BLOCKED";\n'
            '}\n'
        )
        result = await run_cantrip(code, _make_context())
        assert "NETWORK_BLOCKED" in result.scenario

    @pytest.mark.asyncio
    async def test_sandbox_no_filesystem(self):
        code = (
            'try {\n'
            '    Deno.readTextFileSync("/etc/passwd");\n'
            '    context.character.scenario += " FILE_ALLOWED";\n'
            '} catch(e) {\n'
            '    context.character.scenario += " FILE_BLOCKED";\n'
            '}'
        )
        result = await run_cantrip(code, _make_context())
        assert "FILE_BLOCKED" in result.scenario


# ============================================================================
# Deno Runner: Multiple scripts chaining
# ============================================================================

class TestCantripChaining:
    @pytest.mark.asyncio
    async def test_two_scripts_accumulate(self):
        code1 = 'context.character.scenario += " First script.";'
        code2 = 'context.character.scenario += " Second script.";'

        result1 = await run_cantrip(code1, _make_context())
        context = _make_context()
        context["character"]["scenario"] = result1.scenario

        result2 = await run_cantrip(code2, context)
        assert "First script." in result2.scenario
        assert "Second script." in result2.scenario

    @pytest.mark.asyncio
    async def test_second_script_reads_first_output(self):
        code1 = 'context.character.scenario += " FLAG=ACTIVE";'
        code2 = (
            'if (context.character.scenario.includes("FLAG=ACTIVE")) {\n'
            '    context.character.personality += ", flag-aware";\n'
            '}'
        )

        result1 = await run_cantrip(code1, _make_context())
        context = _make_context()
        context["character"]["scenario"] = result1.scenario

        result2 = await run_cantrip(code2, context)
        assert "flag-aware" in result2.personality


# ============================================================================
# Context Builder
# ============================================================================

class TestContextBuilder:
    def test_build_context_basic(self):
        ctx = build_context([{"role": "user", "content": "Hello"}])
        assert ctx["chat"]["last_message"] == "Hello"
        assert ctx["chat"]["message_count"] == 1
        assert len(ctx["chat"]["last_messages"]) == 1

    def test_build_context_multiple_messages(self):
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Message 1"},
            {"role": "assistant", "content": "Response 1"},
            {"role": "user", "content": "Message 2"},
        ]
        ctx = build_context(messages)
        assert ctx["chat"]["last_message"] == "Message 2"
        assert ctx["chat"]["message_count"] == 4

    def test_build_context_multipart_content(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ]},
        ]
        ctx = build_context(messages)
        assert "Part 1" in ctx["chat"]["last_message"]
        assert "Part 2" in ctx["chat"]["last_message"]

    def test_apply_result_no_modifications(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = apply_cantrip_result_to_messages(messages, "", "", "")
        assert result == messages

    def test_apply_result_scenario_injected(self):
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
        ]
        result = apply_cantrip_result_to_messages(
            messages, "", "Extra lore text", ""
        )
        assert len(result) == 3
        scenario_msg = result[-2]
        assert scenario_msg["role"] == "system"
        assert "Extra lore text" in scenario_msg["content"]

    def test_apply_result_personality_injected(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = apply_cantrip_result_to_messages(
            messages, ", brave and strong", "", ""
        )
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "brave and strong" in result[0]["content"]


# ============================================================================
# API: Script CRUD
# ============================================================================

class TestCantripCRUD:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_create_script(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips", json={
            "name": "Test Cantrip",
            "description": "A test cantrip",
            "code": 'context.character.scenario += " Test.";'
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Cantrip"
        assert data["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_scripts(self, admin_client):
        client, _, _ = admin_client
        await client.post("/api/cantrips", json={"name": "Script 1", "code": ""})
        await client.post("/api/cantrips", json={"name": "Script 2", "code": ""})
        resp = await client.get("/api/cantrips")
        assert resp.status_code == 200
        assert len(resp.json()["cantrips"]) == 2

    @pytest.mark.asyncio
    async def test_get_script(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/cantrips", json={
            "name": "GetTest", "code": "const x = 1;"
        })
        cantrip_id = create.json()["id"]
        resp = await client.get(f"/api/cantrips/{cantrip_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "GetTest"

    @pytest.mark.asyncio
    async def test_update_script(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/cantrips", json={
            "name": "Original", "code": ""
        })
        cantrip_id = create.json()["id"]
        resp = await client.put(f"/api/cantrips/{cantrip_id}", json={
            "name": "Updated",
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["is_active"] is False

    @pytest.mark.asyncio
    async def test_delete_script(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/cantrips", json={
            "name": "Delete Me", "code": ""
        })
        cantrip_id = create.json()["id"]
        resp = await client.delete(f"/api/cantrips/{cantrip_id}")
        assert resp.status_code == 204
        resp = await client.get(f"/api/cantrips/{cantrip_id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_cantrip_not_found(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/cantrips/nonexistent-id")
        assert resp.status_code == 404


# ============================================================================
# API: Script Test Endpoint
# ============================================================================

class TestCantripTestEndpoint:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_test_endpoint_basic(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": 'context.character.scenario += " Test output.";'
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "Test output." in data["scenario"]
        assert data["error"] is None

    @pytest.mark.asyncio
    async def test_test_endpoint_with_messages(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": (
                'const lastMsg = context.chat.last_message.toLowerCase();\n'
                'if (lastMsg.includes("castle")) {\n'
                '    context.character.scenario += " Castle found!";\n'
                '}'
            ),
            "messages": [
                {"role": "user", "content": "Let us visit the castle"}
            ],
        })
        assert resp.status_code == 200
        assert "Castle found!" in resp.json()["scenario"]

    @pytest.mark.asyncio
    async def test_test_endpoint_chat_data(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": (
                'const day = context.chat_data.get("day") || 1;\n'
                'context.chat_data.set("day", day + 1);'
            ),
            "chat_data": {"day": 10},
        })
        assert resp.status_code == 200
        assert resp.json()["chat_data"]["day"] == 11

    @pytest.mark.asyncio
    async def test_test_endpoint_debug_logs(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": 'console.log("hello from cantrip"); console.log({key: "value"});',
        })
        assert resp.status_code == 200
        logs = resp.json()["debug_logs"]
        assert any("hello from cantrip" in log for log in logs)

    @pytest.mark.asyncio
    async def test_test_endpoint_error(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": 'const x = ;',
        })
        assert resp.status_code == 200
        assert resp.json()["error"] is not None

    @pytest.mark.asyncio
    async def test_test_stored_cantrip(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/cantrips", json={
            "name": "Stored",
            "code": 'context.character.personality += ", from stored cantrip";',
        })
        cantrip_id = create.json()["id"]
        resp = await client.post(f"/api/cantrips/{cantrip_id}/test", json={})
        assert resp.status_code == 200
        assert "from stored cantrip" in resp.json()["personality"]

    @pytest.mark.asyncio
    async def test_test_endpoint_timeout(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/test", json={
            "code": "while(true) {}",
            "timeout_ms": 2000,
        })
        assert resp.status_code == 408


# ============================================================================
# Real JanitorAI Script Templates
# ============================================================================

class TestRealJanitorAICantrips:
    @pytest.mark.asyncio
    async def test_complex_lorebook_template(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Complex_Lorebook_Template.js")
        ctx = build_context(
            messages=[
                {"role": "system", "content": "You are a fantasy character."},
                {"role": "user", "content": "Tell me about the kingdom of example and its politics"},
            ],
            conversation_id="fantasy-1",
            character_name="Aria",
            character_description="A wise sage",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None
        assert "Example Kingdom" in result.scenario or "Example" in result.scenario

    @pytest.mark.asyncio
    async def test_complex_lorebook_keyword_match(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Complex_Lorebook_Template.js")
        ctx = build_context(
            messages=[
                {"role": "system", "content": "You are a fantasy character."},
                {"role": "user", "content": "I want to explore the crystal tower"},
                {"role": "assistant", "content": "**Day:** 3"},
                {"role": "user", "content": "Tell me about the crystal tower"},
            ],
            conversation_id="fantasy-2",
            character_name="Aria",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None
        assert "Crystal Tower" in result.scenario

    @pytest.mark.asyncio
    async def test_complex_lorebook_no_match(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Complex_Lorebook_Template.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "Hello, nice weather today"},
            ],
            conversation_id="fantasy-3",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None
        assert "Example Kingdom" not in result.scenario

    @pytest.mark.asyncio
    async def test_multiple_character_template(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Multiple_Character_Template.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "I see Character A approaching"},
            ],
            conversation_id="multi-1",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_dice_controller_no_roll(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Dice_Controller.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "Hello there"},
            ],
            conversation_id="dice-1",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_dice_controller_user_roll(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Dice_Controller.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "/roll 2d6+3"},
            ],
            conversation_id="dice-2",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None
        assert "DICE SYSTEM" in result.scenario

    @pytest.mark.asyncio
    async def test_context_control_template(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Context_Control_Template.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "Hello"},
            ],
            conversation_id="ctx-1",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_adaptive_lorebook_template(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Adaptive_Lorebook_Template.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "Tell me about the dragons"},
            ],
            conversation_id="adapt-1",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None

    @pytest.mark.asyncio
    async def test_property_exploration_debug(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("PropertyExploration.js")
        ctx = build_context(
            messages=[{"role": "user", "content": "Hello"}],
            character_name="TestChar",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None
        assert len(result.debug_logs) > 0

    @pytest.mark.asyncio
    async def test_hidden_persistent_memory_template(self):
        if not JANITOR_SCRIPTS_DIR.exists():
            pytest.skip("JanitorAI scripts directory not found")
        code = _load_script("Hidden_Persistent_Memory_Template.js")
        ctx = build_context(
            messages=[
                {"role": "user", "content": "I walk outside into the rain"},
            ],
            conversation_id="mem-1",
        )
        result = await run_cantrip(code, ctx, timeout_ms=10000)
        assert result.error is None


# ============================================================================
# Proxy Integration: Cantrips modify requests
# ============================================================================

class TestProxyIntegration:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    MOCK_ENDPOINT_URL = "http://mock-backend:9999"

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        test_settings = Settings(
            default_endpoint_url=self.MOCK_ENDPOINT_URL,
            default_endpoint_api_key="sk-test-key",
            default_endpoint_model="test-model",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

    @pytest.mark.asyncio
    async def test_cantrip_modifies_forwarded_request(self, admin_client, httpx_mock):
        client, token, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "TestEndpoint",
            "base_url": self.MOCK_ENDPOINT_URL,
            "api_key": "upstream-key",
            "enabled": True,
        })
        assert ep_resp.status_code == 201
        await client.put("/api/settings", json={
            "default_endpoint_id": ep_resp.json()["id"],
        })

        await client.post("/api/cantrips", json={
            "name": "Cantrip Injector",
            "code": (
                'const lastMsg = context.chat.last_message.toLowerCase();\n'
                'if (lastMsg.includes("forest")) {\n'
                '    context.character.scenario += " The forest is dark and ancient.";\n'
                '}'
            ),
            "is_active": True,
        })

        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            status_code=200,
        )

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "I enter the forest"}],
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200

        forwarded = httpx_mock.get_request()
        forwarded_body = json.loads(forwarded.content)
        forwarded_text = json.dumps(forwarded_body["messages"])
        assert "dark and ancient" in forwarded_text

    @pytest.mark.asyncio
    async def test_inactive_script_not_executed(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "TestEndpoint",
            "base_url": self.MOCK_ENDPOINT_URL,
            "api_key": "upstream-key",
            "enabled": True,
        })
        assert ep_resp.status_code == 201
        await client.put("/api/settings", json={
            "default_endpoint_id": ep_resp.json()["id"],
        })

        await client.post("/api/cantrips", json={
            "name": "Inactive",
            "code": 'context.character.scenario += " SHOULD_NOT_APPEAR";',
            "is_active": False,
        })

        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            status_code=200,
        )

        await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"Authorization": f"Bearer {api_key}"},
        )

        forwarded = httpx_mock.get_request()
        forwarded_body = json.loads(forwarded.content)
        assert "SHOULD_NOT_APPEAR" not in json.dumps(forwarded_body["messages"])
