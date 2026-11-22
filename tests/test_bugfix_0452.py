"""Tests for 0.45.2: LiteLLM provider model listing (get_valid_models),
Gemini 3/4 context-window estimation, and truncated tail response-tag stripping."""

from __future__ import annotations

import pytest

from app.routers.endpoints import _list_models_litellm, _strip_provider_prefix
from app.services.budget import estimate_context_window
from app.services.response_tags import strip_tags


class TestLiteLLMModelListing:
    def test_strip_provider_prefix(self):
        assert _strip_provider_prefix("gemini/gemini-2.5-flash", "gemini") == "gemini-2.5-flash"
        assert _strip_provider_prefix("gemini/models/gemini-4-argon", "gemini") == "gemini-4-argon"
        assert _strip_provider_prefix("models/gemini-3-flash-preview", "gemini") == "gemini-3-flash-preview"
        assert _strip_provider_prefix("claude-sonnet-4", "anthropic") == "claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_list_models_uses_get_valid_models_and_strips_prefix(self, monkeypatch):
        import litellm

        captured: dict = {}

        def fake_get_valid_models(**kwargs):
            captured.update(kwargs)
            return ["gemini/gemini-2.5-flash", "gemini/models/gemini-4-argon"]

        monkeypatch.setattr(litellm, "get_valid_models", fake_get_valid_models)
        models = await _list_models_litellm("gemini", "sk-gem", "https://generativelanguage.googleapis.com")
        assert models == ["gemini-2.5-flash", "gemini-4-argon"]
        assert captured["check_provider_endpoint"] is True
        assert captured["custom_llm_provider"] == "gemini"
        assert captured["api_key"] == "sk-gem"
        assert captured["api_base"] == "https://generativelanguage.googleapis.com"

    @pytest.mark.asyncio
    async def test_list_models_falls_back_to_models_by_provider_on_error(self, monkeypatch):
        import litellm

        def boom(**kwargs):
            raise RuntimeError("network blocked")

        monkeypatch.setattr(litellm, "get_valid_models", boom)
        monkeypatch.setattr(litellm, "models_by_provider", {"gemini": ["gemini/gemini-2.5-pro"]})
        models = await _list_models_litellm("gemini", "sk-gem", "")
        assert models == ["gemini-2.5-pro"]

    @pytest.mark.asyncio
    async def test_endpoint_models_route_for_gemini_provider(self, admin_client, monkeypatch):
        import litellm

        client, _, _ = admin_client
        monkeypatch.setattr(
            litellm,
            "get_valid_models",
            lambda **kwargs: ["gemini/gemini-2.5-flash", "gemini/gemini-4-argon"],
        )

        ep = await client.post(
            "/api/endpoints",
            json={
                "name": "GeminiProd",
                "base_url": "",
                "api_key": "sk-gem",
                "provider": "gemini",
                "default_model": "gemini-2.5-flash",
            },
        )
        assert ep.status_code in (200, 201), ep.text
        ep_id = ep.json()["id"]

        resp = await client.get(f"/api/endpoints/{ep_id}/models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == ["gemini-2.5-flash", "gemini-4-argon"]


class TestBudgetGemini3And4:
    @pytest.mark.parametrize(
        "model_name",
        ["gemini-3-flash-preview", "gemini-3.1-pro", "gemini-4-argon", "gemini/gemini-4-neon"],
    )
    def test_gemini_3_and_4_context_window(self, model_name):
        assert estimate_context_window({"model": model_name}, 0) == 1000000


class TestTruncatedTailTagStripping:
    def test_strips_half_closed_tail_tag(self):
        text = "You step east into the mist. <mud_move>E</m"
        assert strip_tags(text, ["mud_move"]) == "You step east into the mist."

    def test_strips_unclosed_payload_and_opening_tag(self):
        assert strip_tags("Done.\n<deed>opened the iron ga", ["deed"]) == "Done."
        assert strip_tags('Done. <mud_delta type="event" note="cut', ["mud_delta"]) == "Done."
        assert strip_tags("Done. <mud_move", ["mud_move"]) == "Done."

    def test_preserves_similar_prefix_non_tag(self):
        text = "The <mud_movement> spell triggers."
        assert strip_tags(text, ["mud_move"]) == text

    def test_complete_tag_and_truncated_second_tag(self):
        text = "Text <mud_delta>{\"event\":\"ok\"}</mud_delta> more <mud_move>E</mu"
        assert strip_tags(text, ["mud_delta", "mud_move"]) == "Text  more"
