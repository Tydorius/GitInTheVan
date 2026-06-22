
from app.services.bypass import (
    BYPASS_PLUGINS,
    TOS_WARNING,
    apply_bypass_decode,
    apply_bypass_encode,
    decode_incoming,
    encode_outgoing,
)
from app.services.jslorebook import (
    extract_from_messages,
    extract_jslorebook_scripts,
    strip_jslorebook_tags,
)
from app.services.prefill import (
    detect_provider,
    has_trailing_assistant,
    normalize_prefill,
)

# ============================================================================
# jslorebook Extraction
# ============================================================================

class TestJSLorebookExtraction:
    def test_simple_extraction(self):
        text = "Normal scenario <jslorebook>context.character.scenario += ' Hello';</jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert len(scripts) == 1
        assert "context.character.scenario" in scripts[0].code

    def test_html_escaped_content(self):
        text = "<jslorebook>context.character.scenario += &quot;Test&quot;;</jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert '"Test"' in scripts[0].code

    def test_multiple_blocks(self):
        text = "<jslorebook>code1</jslorebook> text <jslorebook>code2</jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert len(scripts) == 2

    def test_empty_block_skipped(self):
        text = "<jslorebook>   </jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert len(scripts) == 0

    def test_no_blocks(self):
        assert extract_jslorebook_scripts("Just normal text") == []

    def test_strip_removes_blocks(self):
        text = "Before <jslorebook>code</jslorebook> After"
        result = strip_jslorebook_tags(text)
        assert "<jslorebook>" not in result
        assert "Before" in result
        assert "After" in result

    def test_extract_from_messages(self):
        messages = [
            {"role": "system", "content": "Scenario <jslorebook>code here</jslorebook>"},
            {"role": "user", "content": "Hello"},
        ]
        scripts, cleaned = extract_from_messages(messages)
        assert len(scripts) == 1
        assert "<jslorebook>" not in cleaned[0]["content"]

    def test_position_detection_pre_driver(self):
        text = "<jslorebook>// pre-user\ncode</jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert scripts[0].position == "pre_driver"

    def test_newline_desanitize(self):
        text = "<jslorebook>line1\\nline2</jslorebook>"
        scripts = extract_jslorebook_scripts(text)
        assert "\n" in scripts[0].code
        assert "\\n" not in scripts[0].code


# ============================================================================
# Prefill Normalization
# ============================================================================

class TestPrefill:
    def test_detect_provider_openai(self):
        assert detect_provider("https://api.openai.com/v1") == "openai"

    def test_detect_provider_openrouter(self):
        assert detect_provider("https://openrouter.ai/api/v1") == "openai"

    def test_detect_provider_anthropic(self):
        assert detect_provider("https://api.anthropic.com") == "anthropic"

    def test_detect_provider_google(self):
        assert detect_provider("https://generativelanguage.googleapis.com", "gemini-pro") == "google"

    def test_detect_provider_generic(self):
        assert detect_provider("https://custom-llm.example.com") == "openai"

    def test_has_trailing_assistant_true(self):
        assert has_trailing_assistant([
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ])

    def test_has_trailing_assistant_false(self):
        assert not has_trailing_assistant([
            {"role": "user", "content": "Hi"},
        ])

    def test_no_prefill_when_disabled(self):
        body = {"messages": [{"role": "assistant", "content": "prefill"}]}
        result = normalize_prefill(body, "https://api.openai.com", enabled=False)
        assert result == body

    def test_openai_prefill_converts_to_system(self):
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]
        }
        result = normalize_prefill(body, "https://api.openai.com", enabled=True)
        msgs = result["messages"]
        assert msgs[-1]["role"] != "assistant"
        assert "[ASSISTANT PREFILL]" in msgs[0]["content"]

    def test_anthropic_prefill_passthrough(self):
        body = {
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ]
        }
        result = normalize_prefill(body, "https://api.anthropic.com", enabled=True)
        assert result == body

    def test_no_trailing_assistant_noop(self):
        body = {"messages": [{"role": "user", "content": "Hi"}]}
        result = normalize_prefill(body, "https://api.openai.com", enabled=True)
        assert result == body


# ============================================================================
# Content Bypass Plugins
# ============================================================================

class TestBypassPlugins:
    def test_tos_warning_exists(self):
        assert "Terms of Service" in TOS_WARNING
        assert "own risk" in TOS_WARNING

    def test_plugins_available(self):
        assert "none" in BYPASS_PLUGINS
        assert "space_separation" in BYPASS_PLUGINS
        assert "dot_separation" in BYPASS_PLUGINS
        assert "character_replacement" in BYPASS_PLUGINS

    def test_none_method_passthrough(self):
        text = "Hello world"
        assert encode_outgoing(text, "none") == text
        assert decode_incoming(text, "none") == text

    def test_space_separation_encodes(self):
        text = "This is explicit content"
        encoded = encode_outgoing(text, "space_separation")
        assert encoded != text
        assert "\u200b" in encoded

    def test_space_separation_decodes(self):
        text = "This is explicit content"
        encoded = encode_outgoing(text, "space_separation")
        decoded = decode_incoming(encoded, "space_separation")
        assert decoded == text

    def test_dot_separation_encodes(self):
        text = "explicit content here"
        encoded = encode_outgoing(text, "dot_separation")
        assert "." in encoded
        assert encoded != text

    def test_dot_separation_decodes(self):
        text = "explicit content"
        encoded = encode_outgoing(text, "dot_separation")
        decoded = decode_incoming(encoded, "dot_separation")
        assert decoded == text

    def test_character_replacement_encodes(self):
        text = "Hello apple"
        encoded = encode_outgoing(text, "character_replacement")
        assert encoded != text

    def test_apply_encode_to_messages(self):
        body = {
            "messages": [
                {"role": "system", "content": "System prompt"},
                {"role": "user", "content": "This is explicit content"},
            ]
        }
        result = apply_bypass_encode(body, "space_separation")
        assert "\u200b" in result["messages"][1]["content"]
        assert "\u200b" not in result["messages"][0]["content"]

    def test_apply_decode_to_response(self):
        response = {
            "choices": [{"message": {"content": "exp\u200blicit response"}}]
        }
        result = apply_bypass_decode(response, "space_separation")
        assert "\u200b" not in result["choices"][0]["message"]["content"]

    def test_unknown_method_passthrough(self):
        assert encode_outgoing("text", "unknown") == "text"
