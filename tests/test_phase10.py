import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.cantrip import Cantrip
from app.models.forbidden_word import ForbiddenWord
from app.services.forbidden_words import _scan
from tests.conftest import TestSessionLocal

# ============================================================================
# Forbidden Words: Scan Logic
# ============================================================================

class TestForbiddenScan:
    def _make_word(self, phrase, is_regex=False):
        return ForbiddenWord(id="w1", user_id="u1", phrase=phrase, is_regex=is_regex)

    def test_plain_match_case_insensitive(self):
        words = [self._make_word("dragon")]
        result = _scan("The Dragon flew away.", words, case_sensitive=False)
        assert result.has_matches
        assert result.matches[0].phrase == "dragon"
        assert result.matches[0].count == 1

    def test_plain_match_case_sensitive_no_hit(self):
        words = [self._make_word("dragon")]
        result = _scan("The Dragon flew away.", words, case_sensitive=True)
        assert not result.has_matches

    def test_multiple_occurrences(self):
        words = [self._make_word("the")]
        result = _scan("the cat and the dog", words, case_sensitive=False)
        assert result.matches[0].count == 2

    def test_no_words_no_matches(self):
        result = _scan("some content", [], case_sensitive=False)
        assert not result.has_matches

    def test_regex_match(self):
        words = [self._make_word(r"\b\d{3}-\d{4}\b", is_regex=True)]
        result = _scan("Call 555-1234 now", words, case_sensitive=False)
        assert result.has_matches

    def test_invalid_regex_skipped(self):
        words = [self._make_word("[invalid", is_regex=True)]
        result = _scan("some text", words, case_sensitive=False)
        assert not result.has_matches

    def test_empty_phrase_skipped(self):
        words = [self._make_word("")]
        result = _scan("some text", words, case_sensitive=False)
        assert not result.has_matches

    def test_summary_format(self):
        words = [self._make_word("forbidden")]
        result = _scan("This is forbidden content.", words, case_sensitive=False)
        assert "[FORBIDDEN CONTENT DETECTED]" in result.summary
        assert "forbidden" in result.summary


# ============================================================================
# Forbidden Words: API
# ============================================================================

class TestForbiddenWordsAPI:
    @pytest.fixture
    async def admin_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            setup = await ac.post("/api/auth/setup", json={"username": "admin", "password": "adminpass123"})
            token = setup.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            yield ac

    @pytest.mark.asyncio
    async def test_settings_default(self, admin_client):
        resp = await admin_client.get("/api/forbidden-words/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["forbidden_words_enabled"] is False
        assert data["forbidden_words_case_sensitive"] is False

    @pytest.mark.asyncio
    async def test_update_settings(self, admin_client):
        resp = await admin_client.put("/api/forbidden-words/settings", json={
            "forbidden_words_enabled": True,
            "forbidden_words_case_sensitive": True,
        })
        assert resp.status_code == 200
        assert resp.json()["forbidden_words_enabled"] is True
        assert resp.json()["forbidden_words_case_sensitive"] is True

    @pytest.mark.asyncio
    async def test_create_list_delete(self, admin_client):
        create = await admin_client.post("/api/forbidden-words", json={"phrase": "bad word"})
        assert create.status_code == 201
        assert create.json()["phrase"] == "bad word"

        lst = await admin_client.get("/api/forbidden-words")
        assert lst.json()["total"] == 1

        wid = lst.json()["words"][0]["id"]
        dele = await admin_client.delete(f"/api/forbidden-words/{wid}")
        assert dele.status_code == 204

        lst = await admin_client.get("/api/forbidden-words")
        assert lst.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_empty_phrase_rejected(self, admin_client):
        resp = await admin_client.post("/api/forbidden-words", json={"phrase": "  "})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_test_endpoint(self, admin_client):
        await admin_client.post("/api/forbidden-words", json={"phrase": "secret"})
        resp = await admin_client.post("/api/forbidden-words/test", json={
            "content": "This is a secret message."
        })
        assert resp.status_code == 200
        assert resp.json()["has_matches"] is True
        assert resp.json()["match_count"] == 1

    @pytest.mark.asyncio
    async def test_test_endpoint_no_match(self, admin_client):
        await admin_client.post("/api/forbidden-words", json={"phrase": "xyzzy"})
        resp = await admin_client.post("/api/forbidden-words/test", json={
            "content": "Nothing to see here."
        })
        assert resp.json()["has_matches"] is False


# ============================================================================
# Multi-Position: DB-Level Defaults (verified via migration test below)
# ============================================================================

# Constructor defaults are applied by SQLAlchemy on insert, not on __init__.
# See TestMigrationPositionFields for end-to-end DB default verification.

# (removed TestCantripPositionDefaults — defaults verified through API tests)


# ============================================================================
# Multi-Position: Cantrip API
# ============================================================================

class TestCantripPositionAPI:
    @pytest.fixture
    async def admin_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            setup = await ac.post("/api/auth/setup", json={"username": "admin", "password": "adminpass123"})
            token = setup.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            yield ac

    @pytest.mark.asyncio
    async def test_create_with_positions(self, admin_client):
        resp = await admin_client.post("/api/cantrips", json={
            "name": "Multi-Position",
            "code": "context.character.scenario += ' test';",
            "run_pre_driver": False,
            "run_pre_navigator": True,
            "run_post_navigator": True,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["run_pre_driver"] is False
        assert data["run_pre_navigator"] is True
        assert data["run_post_navigator"] is True
        assert data["run_driver_callable"] is False

    @pytest.mark.asyncio
    async def test_update_positions(self, admin_client):
        create = await admin_client.post("/api/cantrips", json={"name": "Test", "code": ""})
        cid = create.json()["id"]

        resp = await admin_client.put(f"/api/cantrips/{cid}", json={
            "run_pre_navigator": True,
        })
        assert resp.json()["run_pre_navigator"] is True
        assert resp.json()["run_pre_driver"] is True

    @pytest.mark.asyncio
    async def test_list_includes_positions(self, admin_client):
        await admin_client.post("/api/cantrips", json={
            "name": "Position Test", "code": "",
            "run_driver_callable": True,
        })
        resp = await admin_client.get("/api/cantrips")
        c = resp.json()["cantrips"][0]
        assert "run_pre_driver" in c
        assert "run_driver_callable" in c
        assert "run_pre_navigator" in c
        assert "run_post_navigator" in c
        assert c["run_driver_callable"] is True


# ============================================================================
# Multi-Position: Lorebook API
# ============================================================================

class TestLorebookPositionAPI:
    @pytest.fixture
    async def admin_client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            setup = await ac.post("/api/auth/setup", json={"username": "admin", "password": "adminpass123"})
            token = setup.json()["access_token"]
            ac.headers["Authorization"] = f"Bearer {token}"
            yield ac

    @pytest.mark.asyncio
    async def test_create_with_positions(self, admin_client):
        resp = await admin_client.post("/api/lorebooks", json={
            "name": "Multi-Position LB",
            "run_pre_navigator": True,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["run_pre_driver"] is True
        assert data["run_pre_navigator"] is True

    @pytest.mark.asyncio
    async def test_update_positions(self, admin_client):
        create = await admin_client.post("/api/lorebooks", json={"name": "Test LB"})
        lid = create.json()["id"]

        resp = await admin_client.put(f"/api/lorebooks/{lid}", json={
            "run_pre_driver": False,
            "run_post_navigator": True,
        })
        assert resp.json()["run_pre_driver"] is False
        assert resp.json()["run_post_navigator"] is True

    @pytest.mark.asyncio
    async def test_list_includes_positions(self, admin_client):
        await admin_client.post("/api/lorebooks", json={"name": "Test LB 2"})
        resp = await admin_client.get("/api/lorebooks")
        lb = resp.json()["lorebooks"][0]
        assert "run_pre_driver" in lb
        assert "run_pre_navigator" in lb
        assert "run_post_navigator" in lb


# ============================================================================
# Migration: Position columns exist
# ============================================================================

class TestMigrationPositionFields:
    @pytest.mark.asyncio
    async def test_cantrip_has_position_fields(self):
        from app.models.user import User
        async with TestSessionLocal() as s:
            s.add(User(id="u-mig2", username="mig2", password_hash="x", gitv_api_key="key-mig2", is_admin=False))
            await s.commit()
            c = Cantrip(user_id="u-mig2", name="Migration Test", code="")
            s.add(c)
            await s.commit()
            await s.refresh(c)
            assert hasattr(c, "run_pre_driver")
            assert hasattr(c, "run_driver_callable")
            assert hasattr(c, "run_pre_navigator")
            assert hasattr(c, "run_post_navigator")
            assert c.run_pre_driver is True
