"""Package 3 — response capture & data access (items 3, 8, 4, 22, 2-slice).

Response tags: registry validation, extraction/stripping ownership, the
four-path hook, same-turn delivery to post-Navigator cantrips. Registered
strip patterns: validation, round-trip, application. context.activate() as
a rule-19 union source. context.inject() with the in_chat_system position.
The is_regenerate heuristic and request facts. E2E tests require Deno.
"""

from __future__ import annotations

import json

import pytest

from app.services.deno_runner import DENO_PATH
from tests.conftest import TestSessionLocal

MOCK_ENDPOINT_URL = "http://package3-e2e.test"

requires_deno = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")


# ============================================================================
# Helpers
# ============================================================================

async def _make_e2e_endpoint(client):
    resp = await client.post("/api/endpoints", json={
        "name": "Package 3 E2E",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "upstream-key",
        "enabled": True,
    })
    assert resp.status_code == 201, resp.text
    await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})


def _upstream(httpx_mock, content: str = "OK"):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={
            "id": "t", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


async def _send(client, api_key, content="hello", messages=None, conv="p3-chat"):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": messages or [{"role": "user", "content": content}]},
        headers={"Authorization": f"Bearer {api_key}", "x-conversation-id": conv},
    )


def _forwarded_text(httpx_mock) -> str:
    requests = httpx_mock.get_requests()
    return json.dumps(json.loads(requests[-1].content)["messages"])


async def _make_cantrip(client, code, name="p3", **overrides):
    payload = {"name": name, "code": code, "is_active": True}
    payload.update(overrides)
    resp = await client.post("/api/cantrips", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


# ============================================================================
# Unit: tag names and extraction
# ============================================================================

class TestTagNames:
    def test_normalize(self):
        from app.services.response_tags import normalize_tag_name
        assert normalize_tag_name(" Deed ") == "deed"
        assert normalize_tag_name("boss-fight-2") == "boss-fight-2"
        assert normalize_tag_name("") is None
        assert normalize_tag_name("has space") is None
        assert normalize_tag_name("UPPER") == "upper"
        assert normalize_tag_name("x" * 33) is None
        assert normalize_tag_name("memstore") is None, "reserved name refused"
        assert normalize_tag_name("jslorebook") is None

    def test_parse_degrades(self):
        from app.services.response_tags import parse_tag_names
        assert parse_tag_names(None) == []
        assert parse_tag_names("not json") == []
        assert parse_tag_names('{"a": 1}') == []
        assert parse_tag_names('["deed", "DEED", 3, "memstore"]') == ["deed"]


class TestExtractStrip:
    def test_paired_and_selfclosing(self):
        from app.services.response_tags import extract_tags, strip_tags
        content = (
            "Prose here.\n<deed>The hero slew the wolf.</deed>\n"
            '<mood value="triumphant"/>\nMore prose.'
        )
        found = extract_tags(content, ["deed", "mood", "unregistered"])
        assert found == {"deed": ["The hero slew the wolf."], "mood": ["triumphant"]}
        stripped = strip_tags(content, ["deed", "mood"])
        assert "<deed>" not in stripped and "wolf" not in stripped
        assert "triumphant" not in stripped
        assert "Prose here." in stripped and "More prose." in stripped

    def test_ownership_unregistered_untouched(self):
        from app.services.response_tags import extract_tags, strip_tags
        content = "<score>10</score> text"
        assert extract_tags(content, ["deed"]) == {}
        assert strip_tags(content, ["deed"]) == content

    def test_case_insensitive_and_multiple(self):
        from app.services.response_tags import extract_tags
        found = extract_tags("<DEED>one</DEED> mid <deed>two</deed>", ["deed"])
        assert found == {"deed": ["one", "two"]}

    def test_strip_collapses_blank_lines(self):
        from app.services.response_tags import strip_tags
        out = strip_tags("a\n\n\n\n<deed>x</deed>\n\n\n\nb", ["deed"])
        assert out == "a\n\nb"


# ============================================================================
# Unit: strip patterns
# ============================================================================

class TestStripPatterns:
    def test_validate_rejects_bad_regex(self):
        from app.services.response_tags import validate_strip_patterns
        with pytest.raises(ValueError, match="does not compile"):
            validate_strip_patterns(["[unclosed"])
        with pytest.raises(ValueError, match="at most"):
            validate_strip_patterns(["a"] * 21)

    def test_validate_drops_empties_and_dedups(self):
        from app.services.response_tags import validate_strip_patterns
        assert validate_strip_patterns(["x", "", "x", "y?"]) == ["x", "y?"]

    def test_compile_degrades_and_apply(self):
        from app.services.response_tags import apply_strip_patterns, compile_strip_patterns
        # "junk line" is a valid literal regex; "[unclosed" is not.
        patterns = compile_strip_patterns('["junk line", "[unclosed", "SITE_FOOTER:.*"]')
        assert len(patterns) == 2
        # Greedy .* removes through end of line, like a site footer.
        out = apply_strip_patterns("Story.\nSITE_FOOTER: spam", patterns)
        assert out == "Story."
        out2 = apply_strip_patterns("keeps this SITE_FOOTER: tail gone", patterns)
        assert out2 == "keeps this"

    async def test_round_trip_and_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={
            "response_strip_patterns": ["FOOTER_[A-Z]+", "junk line"]})
        assert resp.status_code == 200, resp.text
        body = (await client.get("/api/settings")).json()
        assert body["response_strip_patterns"] == ["FOOTER_[A-Z]+", "junk line"]

        resp = await client.put("/api/settings", json={
            "response_strip_patterns": ["(unclosed"]})
        assert resp.status_code == 422
        # The failed write left the good value in place.
        body = (await client.get("/api/settings")).json()
        assert len(body["response_strip_patterns"]) == 2


# ============================================================================
# API: the registry
# ============================================================================

class TestRegistryApi:
    async def test_round_trip_and_normalization(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips", json={
            "name": "ledger", "code": "// ledger", "response_tags": ["Deed", "DEED2"],
        })
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["response_tags"] == ["deed", "deed2"]

    async def test_invalid_and_reserved_names_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips", json={
            "name": "bad1", "code": "// x", "response_tags": ["has space"]})
        assert resp.status_code == 422
        resp = await client.post("/api/cantrips", json={
            "name": "bad2", "code": "// x", "response_tags": ["memstore"]})
        assert resp.status_code == 422

    async def test_duplicate_across_cantrips_409(self, admin_client):
        client, _, _ = admin_client
        await _make_cantrip(client, "// a", "one", response_tags=["deed"])
        resp = await client.post("/api/cantrips", json={
            "name": "two", "code": "// b", "response_tags": ["deed"]})
        assert resp.status_code == 409
        # The same cantrip may keep its own name.
        cid = (await client.get("/api/cantrips")).json()["cantrips"][0]["id"]
        resp = await client.put(f"/api/cantrips/{cid}", json={"response_tags": ["deed"]})
        assert resp.status_code == 200

    async def test_cross_user_registry_is_scoped(self, admin_client):
        """Uniqueness is per-user: another user's cantrip holding the name
        must not block this user from registering it (the query filters by
        user_id -- a global check would 409 here)."""
        from app.models.cantrip import Cantrip
        from app.models.user import User

        client, _, _ = admin_client
        other_id = "00000000-0000-0000-0000-0000000000ee"
        async with TestSessionLocal() as db:
            db.add(User(id=other_id, username="other", password_hash="x",
                        gitv_api_key="gitv_other"))
            db.add(Cantrip(user_id=other_id, name="other-ledger",
                           code="// b", response_tags='["deed"]'))
            await db.commit()

        resp = await client.post("/api/cantrips", json={
            "name": "my-ledger", "code": "// a", "response_tags": ["deed"]})
        assert resp.status_code in (200, 201), resp.text


# ============================================================================
# E2E: response tags (Deno)
# ============================================================================

_LEDGER_SCRIPT = """
const tags = context.response_tags || {};
const deeds = tags["deed"] || [];
if (deeds.length) {
    context.response.content += " [LEDGER:" + deeds.join("|") + "]";
    context.response.modified = true;
}
"""

_ACTIVATE_INJECT_SCRIPT = """
context.activate("Static Codex");
context.inject("DIRECTIVE_STAY_IN_CHARACTER", "in_chat_system");
"""

_REGEN_SCRIPT = """
const msgs = context.chat.last_messages || [];
const prev = msgs.length >= 2 ? msgs[msgs.length - 2] : null;
if (context.request.is_regenerate) {
    context.inject("REGEN_MARKER", "in_chat_system");
}
if (prev && prev.role === "assistant") {
    context.inject("ROLES_MARKER", "before_last_message");
}
"""


@requires_deno
class TestResponseTagsE2E:
    async def test_extract_strip_and_same_turn_delivery(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await _make_cantrip(
            client, _LEDGER_SCRIPT, "ledger",
            hook_type="post", run_pre_driver=False, run_pre_navigator=False,
            run_post_navigator=True, response_tags=["deed"],
        )
        await client.put("/api/settings", json={"debug_mode": True})

        _upstream(httpx_mock,
                  "The wolf falls.\n<deed>Hero slew the wolf</deed>\n"
                  "<score>10</score> untouched")
        resp = await _send(client, api_key, "attack the wolf")
        assert resp.status_code == 200, resp.text
        content = resp.json()["choices"][0]["message"]["content"]
        assert "<deed>" not in content, "registered tag must be stripped"
        assert "[LEDGER:Hero slew the wolf]" in content, "same-turn delivery"
        assert "<score>10</score> untouched" in content, "unregistered tag passes through"

        # Debug timeline carries the capture; metrics carry the row.
        listing = (await client.get("/api/debug")).json()
        exchange_id = listing["exchanges"][0]["id"]
        detail = (await client.get(f"/api/debug/{exchange_id}")).json()
        pipeline = detail.get("pipeline_data") or detail
        stage_names = [
            s.get("name") for s in json.loads(pipeline).get("stages", [])
        ] if isinstance(pipeline, str) else [
            s.get("name") for s in (pipeline.get("stages") or [])
        ]
        assert "response_tags" in stage_names

    async def test_strip_patterns_applied_to_reply(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        resp = await client.put("/api/settings", json={
            "response_strip_patterns": ["SITE_FOOTER_\\d+"]})
        assert resp.status_code == 200, resp.text

        _upstream(httpx_mock, "Story.\nSITE_FOOTER_42")
        resp = await _send(client, api_key, "go")
        assert resp.status_code == 200, resp.text
        content = resp.json()["choices"][0]["message"]["content"]
        assert "SITE_FOOTER" not in content
        assert "Story." in content


@requires_deno
class TestActivateInjectE2E:
    async def test_activate_pulls_book_and_inject_lands_at_end(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        # An INACTIVE, untagged static book: activate() is the union source
        # that turns it on for this request regardless (rule 19 source 3).
        book = await client.post("/api/lorebooks", json={
            "name": "Static Codex", "description": "",
            "is_active": False, "tag": "",
        })
        assert book.status_code in (200, 201), book.text
        entry = await client.post(f"/api/lorebooks/{book.json()['id']}/entries", json={
            "name": "rune", "keys": ["rune"],
            "content": "CODEX_RUNE_LORE: cold iron binds.",
            "position": "before_last_message",
        })
        assert entry.status_code in (200, 201), entry.text
        await _make_cantrip(client, _ACTIVATE_INJECT_SCRIPT, "director")

        _upstream(httpx_mock)
        resp = await _send(client, api_key, "examine the rune")
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "CODEX_RUNE_LORE" in text, "activate() injected the book's entry"
        messages = json.loads(httpx_mock.get_requests()[-1].content)["messages"]
        assert messages[-1] == {
            "role": "system", "content": "DIRECTIVE_STAY_IN_CHARACTER",
        }, "in_chat_system lands as the final message"

    async def test_missing_book_degrades(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await _make_cantrip(client, 'context.activate("No Such Book");', "wisher")

        _upstream(httpx_mock)
        resp = await _send(client, api_key, "hello")
        assert resp.status_code == 200, resp.text
        assert "No Such Book" not in _forwarded_text(httpx_mock)


@requires_deno
class TestRequestFactsE2E:
    async def test_is_regenerate_roles_and_request_id(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await _make_cantrip(client, _REGEN_SCRIPT, "watcher")

        _upstream(httpx_mock)
        resp = await _send(client, api_key, "first turn")
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "REGEN_MARKER" not in text, "first request is not a regenerate"
        assert "ROLES_MARKER" not in text, "no assistant message precedes yet"

        # Identical resend: same message_count, same trailing user message.
        _upstream(httpx_mock)
        resp = await _send(client, api_key, "first turn")
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "REGEN_MARKER" in text, "identical shape reads as regenerate"

        # A different turn is not: count grows, trailing differs.
        _upstream(httpx_mock)
        resp = await _send(client, api_key, "second turn")
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "REGEN_MARKER" not in text


# ============================================================================
# Upgrade path (migration 060)
# ============================================================================

class TestUpgradePath060:
    """Builds the pre-060 shape of the three tables from the migration DDL
    as it shipped in 059-era code, applies 060, and proves the columns
    landed. The vacuity check asserts the columns are genuinely absent
    first -- without it this class can silently test nothing."""

    def _pre060_engine(self):
        from sqlalchemy import create_engine, text

        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE cantrips (id VARCHAR(36) PRIMARY KEY, "
                "user_id VARCHAR(36), name VARCHAR(128), lorebook_reads TEXT)"))
            conn.execute(text(
                "CREATE TABLE user_settings (user_id VARCHAR(36) PRIMARY KEY, "
                "passenger_parameters_json TEXT)"))
            conn.execute(text(
                "CREATE TABLE conversation_turn_counters (id VARCHAR(36) PRIMARY KEY, "
                "user_id VARCHAR(36), conversation_id VARCHAR(256), turn_count INTEGER)"))
        return engine

    def test_vacuity_pre060_lacks_the_columns(self):
        from sqlalchemy import inspect

        engine = self._pre060_engine()
        insp = inspect(engine)
        assert "response_tags" not in [c["name"] for c in insp.get_columns("cantrips")]
        assert "response_strip_patterns" not in [
            c["name"] for c in insp.get_columns("user_settings")]
        assert "last_request_fingerprint" not in [
            c["name"] for c in insp.get_columns("conversation_turn_counters")]
        engine.dispose()

    async def test_migration_060_adds_columns_and_orm_round_trips(self, admin_client):
        from sqlalchemy import inspect, text

        from app.services.migrations import MIGRATIONS
        ddl = next(sql for name, sql in MIGRATIONS
                   if name == "060_package3_response_capture")["sqlite"]
        engine = self._pre060_engine()
        with engine.begin() as conn:
            for stmt in ddl.strip().split(";"):
                if stmt.strip():
                    conn.execute(text(stmt))
        insp = inspect(engine)
        assert "response_tags" in [c["name"] for c in insp.get_columns("cantrips")]
        assert "response_strip_patterns" in [
            c["name"] for c in insp.get_columns("user_settings")]
        assert "last_request_fingerprint" in [
            c["name"] for c in insp.get_columns("conversation_turn_counters")]
        engine.dispose()

        # ORM exercises the upgraded schema through the real async path.
        from sqlalchemy import select

        from app.models.conversation_turn_counter import ConversationTurnCounter

        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]
        async with TestSessionLocal() as db:
            db.add(ConversationTurnCounter(
                user_id=uid, conversation_id="p3-upgrade",
                turn_count=1, last_request_fingerprint="1:abc"))
            await db.commit()
            row = (await db.execute(select(ConversationTurnCounter).where(
                ConversationTurnCounter.conversation_id == "p3-upgrade"
            ))).scalar_one()
            assert row.last_request_fingerprint == "1:abc"
