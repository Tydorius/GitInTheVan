from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.services.summarization import (
    DEFAULT_PROMPT,
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    _compress_messages,
    _format_transcript,
    _split_dialogue,
    build_summary_context_block,
    compute_boundary_hash,
    estimate_tokens,
    maybe_summarize,
    summarize_messages,
)

# ============================================================================
# Helpers
# ============================================================================

def _mock_endpoint(base_url="https://sum.test", api_key="sk-sum-key", api_base_path="/v1"):
    return Endpoint(
        id="ep-sum",
        user_id="user-1",
        name="Summarization Endpoint",
        base_url=base_url,
        api_key=api_key,
        api_base_path=api_base_path,
        enabled=True,
    )


def _mock_llm_response(content="A concise summary."):
    return {
        "id": "resp-1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


def _make_messages(count: int, content_len: int = 200) -> list[dict]:
    msgs = [{"role": "system", "content": "You are a character."}]
    text = "x" * content_len
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"{role} message {i}: {text}"})
    return msgs


# ============================================================================
# Token estimation
# ============================================================================

class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens([]) == 0

    def test_simple_string(self):
        msgs = [{"role": "user", "content": "abcd"}]
        # 4 chars / 4 = 1 token + 3 overhead + 1 role = 5
        assert estimate_tokens(msgs) == 5

    def test_list_content(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "abcdefgh"}]}]
        tokens = estimate_tokens(msgs)
        assert tokens > 0

    def test_grows_with_content(self):
        short = estimate_tokens([{"role": "user", "content": "ab"}])
        long = estimate_tokens([{"role": "user", "content": "a" * 1000}])
        assert long > short


# ============================================================================
# Boundary hash
# ============================================================================

class TestBoundaryHash:
    def test_stable_for_same_messages(self):
        msgs = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        assert compute_boundary_hash(msgs) == compute_boundary_hash(msgs)

    def test_changes_with_different_messages(self):
        a = [{"role": "user", "content": "hello"}]
        b = [{"role": "user", "content": "goodbye"}]
        assert compute_boundary_hash(a) != compute_boundary_hash(b)

    def test_empty(self):
        assert compute_boundary_hash([])


# ============================================================================
# Transcript formatting
# ============================================================================

class TestFormatTranscript:
    def test_basic_labels(self):
        msgs = [
            {"role": "user", "content": "Hi there"},
            {"role": "assistant", "content": "Hello!"},
        ]
        text = _format_transcript(msgs)
        assert "User: Hi there" in text
        assert "Character: Hello!" in text

    def test_skips_empty_content(self):
        msgs = [{"role": "user", "content": ""}, {"role": "assistant", "content": "x"}]
        text = _format_transcript(msgs)
        assert "Character: x" in text
        assert "User" not in text

    def test_truncation(self):
        msgs = [{"role": "user", "content": "a" * 30000}]
        text = _format_transcript(msgs)
        assert "[...truncated]" in text


# ============================================================================
# Message splitting and compression
# ============================================================================

class TestSplitDialogue:
    def test_identifies_non_system(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ]
        assert _split_dialogue(msgs) == [1, 2]

    def test_all_system(self):
        msgs = [{"role": "system", "content": "sys"}]
        assert _split_dialogue(msgs) == []


class TestCompressMessages:
    def test_preserves_system_messages(self):
        msgs = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "old1"},
            {"role": "assistant", "content": "old2"},
            {"role": "user", "content": "recent1"},
        ]
        result = _compress_messages(msgs, {1, 2}, "SUMMARY")
        contents = [m["content"] for m in result]
        assert "persona" in contents
        assert "recent1" in contents
        assert "SUMMARY" in contents
        assert "old1" not in contents
        assert "old2" not in contents
        # system + summary + recent
        assert len(result) == 3

    def test_summary_inserted_at_first_compressed_position(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "recent"},
        ]
        result = _compress_messages(msgs, {1}, "SUMMARY")
        assert result[0]["content"] == "sys"
        assert result[1]["content"] == "SUMMARY"
        assert result[2]["content"] == "recent"

    def test_no_compress_indices_returns_original(self):
        msgs = [{"role": "user", "content": "hi"}]
        assert _compress_messages(msgs, set(), "SUMMARY") is msgs


# ============================================================================
# Summary context block
# ============================================================================

class TestSummaryContextBlock:
    def test_wraps_summary(self):
        block = build_summary_context_block("My summary")
        assert block.startswith(SUMMARY_OPEN_TAG)
        assert block.endswith(SUMMARY_CLOSE_TAG)
        assert "My summary" in block

    def test_empty_returns_empty(self):
        assert build_summary_context_block("") == ""
        assert build_summary_context_block("   ") == ""


# ============================================================================
# summarize_messages (LLM call, mocked)
# ============================================================================

class TestSummarizeMessages:
    @pytest.mark.asyncio
    async def test_returns_content(self):
        endpoint = _mock_endpoint()
        with patch("app.services.summarization.httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _mock_llm_response("The summary text.")
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await summarize_messages(
                [{"role": "user", "content": "hello"}], endpoint, "model-x", DEFAULT_PROMPT
            )
        assert result == "The summary text."

    @pytest.mark.asyncio
    async def test_returns_existing_on_http_error(self):
        endpoint = _mock_endpoint()
        with patch("app.services.summarization.httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "boom"
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await summarize_messages(
                [{"role": "user", "content": "hello"}], endpoint, "model-x", DEFAULT_PROMPT,
                existing_summary="prior",
            )
        assert result == "prior"

    @pytest.mark.asyncio
    async def test_empty_transcript_returns_existing(self):
        endpoint = _mock_endpoint()
        result = await summarize_messages([], endpoint, "model-x", DEFAULT_PROMPT, "prior")
        assert result == "prior"


# ============================================================================
# maybe_summarize (integration, mocked LLM)
# ============================================================================

class TestMaybeSummarize:
    @pytest.fixture
    async def db_session(self):
        from tests.conftest import TestSessionLocal
        async with TestSessionLocal() as session:
            yield session

    @pytest.mark.asyncio
    async def test_disabled_returns_original(self, db_session):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-disabled", username="disabled", password_hash="x", gitv_api_key="key-disabled", is_admin=False))
            await s.commit()

        body = {"messages": _make_messages(20)}
        result = await maybe_summarize(body, "u-disabled", "chat-1")
        assert result["messages"] == body["messages"]

    @pytest.mark.asyncio
    async def test_below_threshold_returns_original(self, db_session):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-thresh", username="thresh", password_hash="x", gitv_api_key="key-thresh", is_admin=False))
            await s.commit()
            ep = Endpoint(
                id="ep-thresh", user_id="u-thresh", name="EP", base_url="https://x.test",
                api_key="k", api_base_path="/v1", enabled=True,
            )
            s.add(ep)
            s.add(UserSettings(
                user_id="u-thresh", summarization_enabled=True,
                summarization_endpoint_id="ep-thresh",
                summarization_token_threshold=100000,
            ))
            await s.commit()

        with patch("app.services.summarization.summarize_messages", new=AsyncMock(return_value="SUM")):
            body = {"messages": _make_messages(10)}
            result = await maybe_summarize(body, "u-thresh", "chat-1")
        assert result["messages"] == body["messages"]

    @pytest.mark.asyncio
    async def test_compresses_when_over_threshold(self, db_session):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-comp", username="comp", password_hash="x", gitv_api_key="key-comp", is_admin=False))
            await s.commit()
            ep = Endpoint(
                id="ep-comp", user_id="u-comp", name="EP", base_url="https://x.test",
                api_key="k", api_base_path="/v1", enabled=True,
            )
            s.add(ep)
            s.add(UserSettings(
                user_id="u-comp", summarization_enabled=True,
                summarization_endpoint_id="ep-comp",
                summarization_token_threshold=1,
                summarization_keep_recent=4,
            ))
            await s.commit()

        body = {"messages": _make_messages(10)}
        original_count = len(body["messages"])

        with patch(
            "app.services.summarization.summarize_messages",
            new=AsyncMock(return_value="Compressed summary text."),
        ):
            result = await maybe_summarize(body, "u-comp", "chat-comp")

        new_msgs = result["messages"]
        assert len(new_msgs) < original_count
        summary_found = any(
            SUMMARY_OPEN_TAG in m.get("content", "") for m in new_msgs
        )
        assert summary_found
        # recent dialogue preserved
        assert new_msgs[-1]["role"] != "system" or new_msgs[-1]["content"].startswith("user message")

    @pytest.mark.asyncio
    async def test_reuses_cached_summary(self, db_session):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-cache", username="cache", password_hash="x", gitv_api_key="key-cache", is_admin=False))
            await s.commit()
            ep = Endpoint(
                id="ep-cache", user_id="u-cache", name="EP", base_url="https://x.test",
                api_key="k", api_base_path="/v1", enabled=True,
            )
            s.add(ep)
            s.add(UserSettings(
                user_id="u-cache", summarization_enabled=True,
                summarization_endpoint_id="ep-cache",
                summarization_token_threshold=1,
                summarization_keep_recent=2,
            ))
            await s.commit()

        body = {"messages": _make_messages(6)}

        msgs = body["messages"]
        dialogue_idx = _split_dialogue(msgs)
        to_compress = [msgs[i] for i in dialogue_idx[:-2]]
        boundary = compute_boundary_hash(to_compress)

        async with TestSessionLocal() as s:
            s.add(ConversationSummary(
                user_id="u-cache", internal_chat_id="chat-cache",
                summary="Cached summary.", boundary_hash=boundary,
                message_count=len(to_compress), token_estimate=999,
            ))
            await s.commit()

        mock_fn = AsyncMock(return_value="SHOULD NOT BE USED")
        with patch("app.services.summarization.summarize_messages", new=mock_fn):
            result = await maybe_summarize(body, "u-cache", "chat-cache")

        mock_fn.assert_not_called()
        assert any("Cached summary." in m.get("content", "") for m in result["messages"])

    @pytest.mark.asyncio
    async def test_no_endpoint_returns_original(self, db_session):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-noep", username="noep", password_hash="x", gitv_api_key="key-noep", is_admin=False))
            await s.commit()
            s.add(UserSettings(
                user_id="u-noep", summarization_enabled=True,
                summarization_endpoint_id=None,
            ))
            await s.commit()

        body = {"messages": _make_messages(20)}
        result = await maybe_summarize(body, "u-noep", "chat-1")
        assert result["messages"] == body["messages"]


# ============================================================================
# API: Settings CRUD
# ============================================================================

class TestSummarizationSettingsAPI:
    @pytest.fixture
    async def admin_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            setup = await ac.post(
                "/api/auth/setup",
                json={"username": "admin", "password": "adminpass123"},
            )
            assert setup.status_code == 201
            token = setup.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            yield ac

    @pytest.mark.asyncio
    async def test_get_default_settings(self, admin_client):
        resp = await admin_client.get("/api/summarization/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summarization_enabled"] is False
        assert data["summarization_token_threshold"] == 8000
        assert data["summarization_keep_recent"] == 6
        assert data["summarization_prompt"]

    @pytest.mark.asyncio
    async def test_update_settings(self, admin_client):
        resp = await admin_client.put("/api/summarization/settings", json={
            "summarization_enabled": True,
            "summarization_token_threshold": 12000,
            "summarization_keep_recent": 8,
            "summarization_model": "gpt-4o",
            "summarization_prompt": "Custom prompt.",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["summarization_enabled"] is True
        assert data["summarization_token_threshold"] == 12000
        assert data["summarization_keep_recent"] == 8
        assert data["summarization_model"] == "gpt-4o"
        assert data["summarization_prompt"] == "Custom prompt."

    @pytest.mark.asyncio
    async def test_update_settings_persists(self, admin_client):
        await admin_client.put("/api/summarization/settings", json={
            "summarization_enabled": True, "summarization_keep_recent": 3,
        })
        resp = await admin_client.get("/api/summarization/settings")
        assert resp.json()["summarization_keep_recent"] == 3

    @pytest.mark.asyncio
    async def test_threshold_clamped(self, admin_client):
        resp = await admin_client.put("/api/summarization/settings", json={
            "summarization_token_threshold": 0,
        })
        assert resp.json()["summarization_token_threshold"] == 1

    @pytest.mark.asyncio
    async def test_keep_recent_minimum_floor(self, admin_client):
        resp = await admin_client.put("/api/summarization/settings", json={
            "summarization_keep_recent": 0,
        })
        assert resp.json()["summarization_keep_recent"] == 3

        resp = await admin_client.put("/api/summarization/settings", json={
            "summarization_keep_recent": 2,
        })
        assert resp.json()["summarization_keep_recent"] == 3

        resp = await admin_client.put("/api/summarization/settings", json={
            "summarization_keep_recent": 8,
        })
        assert resp.json()["summarization_keep_recent"] == 8

    @pytest.mark.asyncio
    async def test_endpoint_cleared_with_empty(self, admin_client):
        await admin_client.put("/api/summarization/settings", json={
            "summarization_endpoint_id": "some-id",
        })
        await admin_client.put("/api/summarization/settings", json={
            "summarization_endpoint_id": "",
        })
        resp = await admin_client.get("/api/summarization/settings")
        assert resp.json()["summarization_endpoint_id"] is None


# ============================================================================
# API: Summaries list/delete
# ============================================================================

class TestSummariesAPI:
    @pytest.fixture
    async def admin_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            setup = await ac.post(
                "/api/auth/setup",
                json={"username": "admin", "password": "adminpass123"},
            )
            token = setup.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            users_resp = await ac.get("/api/users")
            admin_user_id = next(
                u["id"] for u in users_resp.json().get("users", []) if u["username"] == "admin"
            )
            yield ac, admin_user_id

    @pytest.mark.asyncio
    async def test_list_empty(self, admin_client):
        client, _ = admin_client
        resp = await client.get("/api/summarization/summaries")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_list_and_delete(self, admin_client):
        client, admin_id = admin_client
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(ConversationSummary(
                user_id=admin_id, internal_chat_id="chat-a",
                summary="Summary A", boundary_hash="h1",
                message_count=5, token_estimate=100,
            ))
            await s.commit()

        resp = await client.get("/api/summarization/summaries")
        data = resp.json()
        assert data["total"] == 1
        assert data["summaries"][0]["summary"] == "Summary A"
        sid = data["summaries"][0]["id"]

        del_resp = await client.delete(f"/api/summarization/summaries/{sid}")
        assert del_resp.status_code == 204

        resp = await client.get("/api/summarization/summaries")
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_delete_not_found(self, admin_client):
        client, _ = admin_client
        resp = await client.delete("/api/summarization/summaries/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_filter_by_chat(self, admin_client):
        client, admin_id = admin_client
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(ConversationSummary(
                user_id=admin_id, internal_chat_id="chat-1",
                summary="S1", boundary_hash="h1", message_count=1, token_estimate=1,
            ))
            s.add(ConversationSummary(
                user_id=admin_id, internal_chat_id="chat-2",
                summary="S2", boundary_hash="h2", message_count=1, token_estimate=1,
            ))
            await s.commit()

        resp = await client.get("/api/summarization/summaries?internal_chat_id=chat-1")
        data = resp.json()
        assert data["total"] == 1
        assert data["summaries"][0]["internal_chat_id"] == "chat-1"


# ============================================================================
# Migration: summarization fields exist
# ============================================================================

class TestMigrationFields:
    @pytest.mark.asyncio
    async def test_settings_row_has_summarization_defaults(self):
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as s:
            s.add(User(id="u-mig", username="mig", password_hash="x", gitv_api_key="key-mig", is_admin=False))
            await s.commit()
            us = UserSettings(user_id="u-mig")
            s.add(us)
            await s.commit()
            await s.refresh(us)
            assert us.summarization_enabled is False
            assert us.summarization_token_threshold == 8000
            assert us.summarization_keep_recent == 6
            assert us.summarization_model == ""
            assert us.summarization_endpoint_id is None
            assert "Summarize" in us.summarization_prompt
