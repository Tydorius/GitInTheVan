"""Tests for Phase 44 Item 13 (Macro Expansion) and 0.43.0 dedup / cleanup."""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services import lorebook as lorebook_svc
from app.services import response_pipeline, upstream
from app.services.macros import (
    expand_macros,
    expand_macros_in_messages,
    resolve_macro_names,
)

MOCK_ENDPOINT_URL = "http://mock-backend:9999"


class TestExpandMacros:
    def test_case_insensitive_tokens(self):
        text = "Hi {{user}}, meet {{User}} and {{USER}}. {{char}} / {{Char}} / {{CHAR}}."
        out, count = expand_macros(text, user_name="Alice", char_name="Bob")
        assert out == "Hi Alice, meet Alice and Alice. Bob / Bob / Bob."
        assert count == 6

    def test_unknown_char_left_intact(self):
        text = "Hello {{user}}, I am {{char}}."
        out, count = expand_macros(text, user_name="Alice", char_name="")
        assert out == "Hello Alice, I am {{char}}."
        assert count == 1

    def test_jslorebook_block_untouched(self):
        text = (
            "Before {{user}} {{char}}.\n"
            "<jslorebook>\n"
            "const msg = '{{user}} meets {{char}}';\n"
            "</jslorebook>\n"
            "After {{CHAR}} and {{User}}."
        )
        out, count = expand_macros(text, user_name="Alice", char_name="Bob")
        assert (
            out
            == "Before Alice Bob.\n"
            "<jslorebook>\n"
            "const msg = '{{user}} meets {{char}}';\n"
            "</jslorebook>\n"
            "After Bob and Alice."
        )
        assert count == 4

    def test_no_macros_returns_zero(self):
        text = "Plain text without macros."
        out, count = expand_macros(text, user_name="Alice", char_name="Bob")
        assert out == text
        assert count == 0


class TestResolveMacroNames:
    def test_header_overrides_case_insensitive(self):
        messages = [
            {"role": "system", "content": "<CardChar's Persona>Brave</CardChar's Persona>"}
        ]
        headers = {"x-user-name": "HeaderUser", "X-Char-Name": "HeaderChar"}
        user_name, char_name = resolve_macro_names(messages, request_headers=headers)
        assert user_name == "HeaderUser"
        assert char_name == "HeaderChar"

    def test_x_character_name_header_alias(self):
        headers = {"X-User-Name": "Dana", "X-Character-Name": "Kael"}
        user_name, char_name = resolve_macro_names([], request_headers=headers)
        assert user_name == "Dana"
        assert char_name == "Kael"

    def test_janitor_persona_and_user_persona_fallback(self):
        messages = [
            {
                "role": "system",
                "content": (
                    "<Aria's Persona>A clever mage.</Aria's Persona>\n"
                    "<UserPersona>User: Rowan\nA wandering bard.</UserPersona>"
                ),
            }
        ]
        user_name, char_name = resolve_macro_names(messages)
        assert user_name == "Rowan"
        assert char_name == "Aria"

    def test_unknown_user_and_char_stay_empty(self):
        messages = [{"role": "user", "content": "Hello {{char}} and {{user}}"}]
        user_name, char_name = resolve_macro_names(messages)
        assert user_name == ""
        assert char_name == ""

    def test_persona_trait_line_not_treated_as_char_name(self):
        messages = [
            {"role": "system", "content": "Persona: shy, observant\nSetting: tavern\n"}
        ]
        user_name, char_name = resolve_macro_names(messages)
        assert user_name == ""
        assert char_name == ""

    def test_unknown_user_macro_left_intact(self):
        body_json = {
            "messages": [{"role": "user", "content": "Hello {{user}} and {{char}}."}],
        }
        count = expand_macros_in_messages(body_json)
        assert count == 0
        assert body_json["messages"][0]["content"] == "Hello {{user}} and {{char}}."

    def test_bare_name_line_does_not_resolve_user(self):
        """Card-style prompts use a bare ``Name:`` line for the CHARACTER;
        resolving the user from it would silently swap the two names. A bare
        Name: line resolves neither name (char needs a Persona section or a
        Character:/Persona: line)."""
        messages = [
            {"role": "system", "content": "Name: Dracula\nA gothic scenario.\n"}
        ]
        user_name, char_name = resolve_macro_names(messages)
        assert user_name != "Dracula"
        assert char_name == ""

    def test_name_line_inside_user_persona_resolves_user(self):
        messages = [
            {
                "role": "system",
                "content": (
                    "<Vlad's Persona>The prince.</Vlad's Persona>\n"
                    "<UserPersona>Name: Van Helsing\nA hunter.</UserPersona>"
                ),
            }
        ]
        user_name, char_name = resolve_macro_names(messages)
        assert user_name == "Van Helsing"
        assert char_name == "Vlad"


class TestExpandMacrosInMessages:
    def test_multimodal_list_content_and_debug_capture(self):
        body_json = {
            "messages": [
                {
                    "role": "system",
                    "content": "<Lyra's Persona>Knight</Lyra's Persona>",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "{{user}} waves at {{char}}."},
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    ],
                },
            ],
            "_gitv_debug": {"original_messages": "[]", "stages": []},
        }
        count = expand_macros_in_messages(body_json, {"X-User-Name": "Sam"})
        assert count == 2
        parts = body_json["messages"][1]["content"]
        assert parts[0]["text"] == "Sam waves at Lyra."
        assert parts[1]["image_url"]["url"] == "https://example.com/a.png"

        stages = body_json["_gitv_debug"]["stages"]
        assert len(stages) == 1
        assert stages[0]["name"] == "macro_expansion"
        assert stages[0]["metadata"]["count"] == 2
        assert stages[0]["metadata"]["user_name"] == "Sam"
        assert stages[0]["metadata"]["char_name"] == "Lyra"

    def test_no_debug_capture_when_zero_replacements(self):
        body_json = {
            "messages": [{"role": "user", "content": "No macros here"}],
            "_gitv_debug": {"original_messages": "[]", "stages": []},
        }
        count = expand_macros_in_messages(body_json)
        assert count == 0
        assert body_json["_gitv_debug"]["stages"] == []


class TestProxyEndToEndMacroExpansion:
    @pytest.mark.asyncio
    async def test_lorebook_injected_macros_expanded_before_forward(
        self, admin_client, httpx_mock, monkeypatch
    ):
        test_settings = Settings(
            default_endpoint_url=MOCK_ENDPOINT_URL,
            default_endpoint_api_key="sk-test-key",
            default_endpoint_model="test-model",
            default_endpoint_api_base_path="",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

        ac, _, api_key = admin_client
        ep_resp = await ac.post(
            "/api/endpoints",
            json={"name": "Raw", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-test-key"},
        )
        assert ep_resp.status_code in (200, 201), ep_resp.text
        await ac.put("/api/settings", json={"default_endpoint_id": ep_resp.json()["id"]})
        # Create an active lorebook with an entry containing {{char}} and {{user}}
        lb_resp = await ac.post(
            "/api/lorebooks",
            json={"name": "MacroTestBook", "is_active": True},
        )
        assert lb_resp.status_code in (200, 201)
        lb_id = lb_resp.json()["id"]

        entry_resp = await ac.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "GreetingRule",
                "keys": ["castle"],
                "content": "Lore: {{char}} guards the castle for {{user}}.",
                "position": "before_last_message",
                "is_constant": False,
                "is_disabled": False,
            },
        )
        assert entry_resp.status_code in (200, 201)

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "chatcmpl-macro",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Response keeps {{char}} untouched.",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
            status_code=200,
        )

        resp = await ac.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-User-Name": "Elena"},
            json={
                "model": "test-model",
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": "<Garrick's Persona>Stalwart guard.</Garrick's Persona>",
                    },
                    {
                        "role": "user",
                        "content": "I approach the castle as {{user}}.",
                    },
                ],
            },
        )
        assert resp.status_code == 200
        # Response side is never mutated
        assert resp.json()["choices"][0]["message"]["content"] == "Response keeps {{char}} untouched."

        forwarded = httpx_mock.get_request()
        assert forwarded is not None
        forwarded_body = json.loads(forwarded.content)
        joined = "\n".join(
            m["content"] for m in forwarded_body["messages"] if isinstance(m.get("content"), str)
        )
        assert "Lore: Garrick guards the castle for Elena." in joined
        assert "I approach the castle as Elena." in joined
        assert "{{user}}" not in joined
        assert "{{char}}" not in joined

    @pytest.mark.asyncio
    async def test_map_path_macros_expand_before_stage_forward(
        self, admin_client, httpx_mock, monkeypatch
    ):
        """The map branch returns before the non-map expansion site; without
        the map-side expansion call, {{user}} ships literal to every stage."""
        test_settings = Settings(
            default_endpoint_url=MOCK_ENDPOINT_URL,
            default_endpoint_api_key="sk-test-key",
            default_endpoint_model="test-model",
            default_endpoint_api_base_path="",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

        ac, _, api_key = admin_client
        ep_resp = await ac.post(
            "/api/endpoints",
            json={"name": "Raw", "base_url": MOCK_ENDPOINT_URL, "api_key": "sk-test-key"},
        )
        assert ep_resp.status_code in (200, 201), ep_resp.text
        ep_id = ep_resp.json()["id"]
        await ac.put("/api/settings", json={"default_endpoint_id": ep_id})

        map_resp = await ac.post("/api/maps/import", json={"data": {
            "name": "MacroMap",
            "stages": [{
                "stage_order": 1,
                "name": "Draft",
                "system_instructions": "Write a reply.",
                "endpoint_id": ep_id,
                "model_override": "",
                "output_mode": "persist",
                "resources": [],
            }],
        }})
        assert map_resp.status_code in (200, 201), map_resp.text
        await ac.put("/api/settings", json={"default_map_id": map_resp.json()["id"]})

        httpx_mock.add_response(
            url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
            json={
                "id": "chatcmpl-map-macro",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "test-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "Mapped reply."},
                    "finish_reason": "stop",
                }],
            },
            status_code=200,
        )

        resp = await ac.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-User-Name": "Elena"},
            json={
                "model": "test-model",
                "stream": False,
                "messages": [
                    {"role": "system", "content": "You are a narrator."},
                    {"role": "user", "content": "I approach the castle as {{user}}."},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["choices"][0]["message"]["content"] == "Mapped reply."

        forwarded = httpx_mock.get_request()
        assert forwarded is not None
        forwarded_body = json.loads(forwarded.content)
        joined = "\n".join(
            m["content"] for m in forwarded_body["messages"] if isinstance(m.get("content"), str)
        )
        assert "I approach the castle as Elena." in joined
        assert "{{user}}" not in joined


class TestDedupCleanup0430:
    def test_book_entries_to_dicts_omits_character_limit(self):
        class DummyEntry:
            id = "e1"
            name = "Entry 1"
            keys = ["k"]
            secondary_keys = []
            content = "body"
            position = "before_last_message"
            insertion_order = 10
            is_constant = False
            is_selective = False
            is_disabled = False
            character_limit = 500

        class DummyBook:
            id = "b1"
            name = "Book 1"

        dicts = lorebook_svc.book_entries_to_dicts([DummyEntry()], book=DummyBook())
        assert len(dicts) == 1
        assert "character_limit" not in dicts[0]

    def test_resolve_proxy_override_shared_from_upstream(self):
        assert response_pipeline._resolve_proxy_override is upstream._resolve_proxy_override
