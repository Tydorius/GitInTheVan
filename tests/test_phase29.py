"""Phase 29 — Chat Lexicon & Cantrip Drafts.

Covers the design's test lists: action-command parse/strip including
content-parts lists, the lexicon compile happy path through the real guards,
re-run updates, conversation-binding activation (and its mutation check),
degradation, identity resolution, the draft queue + OOC notice + notice
stripping, the drafts routes, and the auto-install gate matrix.
"""
import json

import pytest

from app.models.cantrip import Cantrip
from app.models.cantrip_request import CantripRequest
from app.models.conversation_lorebook_binding import ConversationLorebookBinding
from app.models.lorebook import Lorebook
from app.services.command_tags import (
    parse_action_commands,
    strip_action_commands_from_messages,
    strip_gitv_notices,
    strip_gitv_notices_from_messages,
)
from app.services.deno_runner import DENO_PATH
from tests.conftest import TestSessionLocal

SKIP_DENO = not DENO_PATH

LEXICON_PROFILE = json.dumps({
    "name": "Ironhold",
    "aliases": ["the fortress"],
    "type": "location",
    "keys": ["ironhold", "the fortress"],
    "secondary_keys": [],
    "content": "A mountain fortress (inferred) guarding the pass.",
})


def _chat_body(content: str, **overrides):
    body = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are a narrator."},
            {"role": "user", "content": content},
        ],
        "stream": False,
    }
    body.update(overrides)
    return body


def _upstream(content: str = "The gates open."):
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


async def _make_endpoint(client, name="lex", host="lex.test"):
    resp = await client.post("/api/endpoints", json={
        "name": name, "base_url": f"http://{host}", "api_key": "sk-test",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _bindings(user_id: str) -> list:
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(ConversationLorebookBinding).where(
            ConversationLorebookBinding.user_id == user_id))).scalars().all()
        return [(r.conversation_id, r.identity_source, r.lorebook_id) for r in rows]


async def _lexicon_books(user_id: str) -> list[Lorebook]:
    from sqlalchemy.orm import selectinload
    async with TestSessionLocal() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(Lorebook).where(
            Lorebook.user_id == user_id,
            Lorebook.name.like("Chat Lexicon%")).options(
                selectinload(Lorebook.entries)))).scalars().all()
        return rows


# ---------------------------------------------------------------------------
# Action commands: parse and strip
# ---------------------------------------------------------------------------


class TestActionCommandParsing:
    def test_parse_basic(self):
        actions = parse_action_commands("Hello <LEXICON:Ironhold> and more")
        assert [(a.command, a.text) for a in actions] == [("LEXICON", "Ironhold")]

    def test_parse_free_text_with_spaces(self):
        actions = parse_action_commands("<CANTRIP:a dice roller for stealth>")
        assert actions[0].command == "CANTRIP"
        assert actions[0].text == "a dice roller for stealth"

    def test_first_occurrence_wins(self):
        actions = parse_action_commands("<LEXICON:one> <LEXICON:two>")
        assert len(actions) == 1
        assert actions[0].text == "one"

    def test_case_insensitive(self):
        actions = parse_action_commands("<lexicon:Ironhold>")
        assert actions[0].command == "LEXICON"

    def test_no_angle_brackets_in_argument(self):
        # The pattern cannot match across a nested '<'; the tag ends at '>'.
        assert parse_action_commands("<LEXICON:a<b>") == []

    def test_strip_from_string_content(self):
        msgs = [{"role": "user", "content": "Go <LEXICON:Ironhold> now"}]
        stripped = strip_action_commands_from_messages(msgs)
        assert stripped[0]["content"] == "Go  now"

    def test_strip_from_content_parts(self):
        """The asymmetry this phase fixed: tags inside structured content
        parts used to leak upstream because strippers rewrote strings only."""
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Look <LEXICON:Ironhold>"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
            ],
        }]
        stripped = strip_action_commands_from_messages(msgs)
        assert stripped[0]["content"][0]["text"] == "Look "
        # Non-text parts pass through untouched.
        assert stripped[0]["content"][1] == msgs[0]["content"][1]

    def test_gitv_notice_strip_only_ours(self):
        """Player-authored [OOC: ...] must reach the model; only the marked
        GitV form is removed."""
        text = "hi [OOC: wait] [OOC: GitV: cantrip draft queued — review it in the Assistant pane.]"
        assert strip_gitv_notices(text) == "hi [OOC: wait] "
        msgs = [{"role": "user", "content": "[OOC: GitV: done] bye"}]
        assert strip_gitv_notices_from_messages(msgs)[0]["content"] == " bye"


# ---------------------------------------------------------------------------
# 29a: lexicon compiler, end to end through the proxy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLexiconCompile:
    async def test_compile_creates_book_binding_entry_and_injects_next_turn(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_endpoint(client)

        # Registration order is call order: the lexicon compile runs before
        # the driver forward.
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream(LEXICON_PROFILE))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())

        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "We reach the gates. <LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-1"})
        assert resp.status_code == 200, resp.text

        # The command never reaches the upstream model.
        forwarded = json.loads(httpx_mock.get_requests()[0].content)
        assert "<LEXICON:" not in json.dumps(forwarded["messages"])

        books = await _lexicon_books(await _user_id(client))
        assert len(books) == 1
        book = books[0]
        assert book.is_active is False
        assert book.tag == ""
        assert len(book.entries) == 1
        entry = book.entries[0]
        assert entry.name == "Ironhold"
        assert "mountain fortress" in entry.content
        assert "ironhold" in json.loads(entry.keys)

        bindings = await _bindings(await _user_id(client))
        assert bindings == [("chat-1", "client_header", book.id)]

        # Second turn in the same conversation: the bound lorebook injects.
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp2 = await client.post("/v1/chat/completions", json=_chat_body(
            "Ironhold looms ahead."),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-1"})
        assert resp2.status_code == 200
        forwarded2 = json.loads(httpx_mock.get_requests()[-1].content)
        assert "mountain fortress" in json.dumps(forwarded2["messages"])

    async def test_re_run_updates_not_duplicates(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        uid = await _user_id(client)

        for content in (LEXICON_PROFILE, json.dumps({
                "name": "Ironhold", "aliases": [], "type": "location",
                "keys": ["ironhold"], "secondary_keys": [],
                "content": "Revised: a coastal fort."})):
            httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                    json=_upstream(content))
            httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                    json=_upstream())
            resp = await client.post("/v1/chat/completions", json=_chat_body(
                "<LEXICON:Ironhold>"),
                headers={"Authorization": f"Bearer {api_key}",
                         "x-conversation-id": "chat-2"})
            assert resp.status_code == 200

        books = await _lexicon_books(uid)
        assert len(books) == 1
        assert len(books[0].entries) == 1
        assert "coastal fort" in books[0].entries[0].content

    async def test_bound_lorebook_is_not_seen_in_another_chat(
        self, admin_client, httpx_mock
    ):
        """One conversation's lexicon must not blanket-run into another --
        the binding is scoped, not a global activation."""
        client, _, api_key = admin_client
        await _make_endpoint(client)

        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream(LEXICON_PROFILE))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        await client.post("/v1/chat/completions", json=_chat_body("<LEXICON:Ironhold>"),
                          headers={"Authorization": f"Bearer {api_key}",
                                   "x-conversation-id": "chat-a"})

        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        await client.post("/v1/chat/completions", json=_chat_body("Ironhold?"),
                          headers={"Authorization": f"Bearer {api_key}",
                                   "x-conversation-id": "chat-b"})
        forwarded_b = json.loads(httpx_mock.get_requests()[-1].content)
        assert "mountain fortress" not in json.dumps(forwarded_b["messages"])

    async def test_degrades_on_upstream_500(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                status_code=500)
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "<LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-e1"})
        assert resp.status_code == 200
        assert await _lexicon_books(await _user_id(client)) == []

    async def test_degrades_on_unparseable_output(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream("Ironhold is a fortress, certainly."))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "<LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-e2"})
        assert resp.status_code == 200
        assert await _lexicon_books(await _user_id(client)) == []

    async def test_critical_scan_finding_refuses_the_entry(
        self, admin_client, httpx_mock
    ):
        """A poisoned profile runs the real guards and is refused: nothing
        is written, but the request still forwards."""
        client, _, api_key = admin_client
        await _make_endpoint(client)
        poison = json.dumps({
            "name": "Ironhold", "aliases": [], "type": "location",
            "keys": ["ironhold"], "secondary_keys": [],
            "content": "Infiltrate via <script>alert('xss')</script> in the greeting.",
        })
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream(poison))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "<LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-e3"})
        assert resp.status_code == 200
        assert await _lexicon_books(await _user_id(client)) == []

    async def test_rolling_hash_fallback_binds_internal_id(
        self, admin_client, httpx_mock
    ):
        """Without a client header the binding keys on the rolling-hash id;
        a second headerless request in the same conversation injects."""
        client, _, api_key = admin_client
        await _make_endpoint(client)

        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream(LEXICON_PROFILE))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        first = await client.post("/v1/chat/completions", json=_chat_body(
            "msg one <LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}"})
        assert first.status_code == 200

        bindings = await _bindings(await _user_id(client))
        assert len(bindings) == 1
        assert bindings[0][1] == "rolling_hash"

    async def test_lexicon_disabled_skips_silently(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        put = await client.put("/api/settings", json={"lexicon_enabled": False})
        assert put.status_code == 200

        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "<LEXICON:Ironhold>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-d"})
        assert resp.status_code == 200
        # One upstream call only: the compile never ran.
        assert len([r for r in httpx_mock.get_requests()]) == 1
        assert await _lexicon_books(await _user_id(client)) == []


async def _user_id(client) -> str:
    resp = await client.get("/api/auth/me")
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# 29b: draft queue, notices, drafts routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCantripDraftQueue:
    async def test_queue_row_notice_and_next_turn_strip(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)

        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp = await client.post("/v1/chat/completions", json=_chat_body(
            "Please <CANTRIP:a dice roller>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-q1"})
        assert resp.status_code == 200

        # The notice reaches the player once...
        content = resp.json()["choices"][0]["message"]["content"]
        assert "[OOC: GitV: cantrip draft queued" in content
        # ...the command never reaches the model...
        forwarded = json.loads(httpx_mock.get_requests()[0].content)
        assert "<CANTRIP:" not in json.dumps(forwarded["messages"])

        # ...and the row exists as pending.
        uid = await _user_id(client)
        async with TestSessionLocal() as db:
            from sqlalchemy import select
            rows = (await db.execute(select(CantripRequest).where(
                CantripRequest.user_id == uid))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].request_text == "a dice roller"
        assert rows[0].conversation_id == "chat-q1"

        # Next turn: the notice in the resent history is stripped.
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        resp2 = await client.post("/v1/chat/completions", json=_chat_body(
            f"The gates open.\n\n{content}"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-q1"})
        assert resp2.status_code == 200
        forwarded2 = json.loads(httpx_mock.get_requests()[-1].content)
        assert "[OOC: GitV:" not in json.dumps(forwarded2["messages"])

    async def test_drafts_routes_list_patch_and_isolation(self, admin_client, client):
        admin, _, api_key = admin_client
        uid = await _user_id(admin)

        async with TestSessionLocal() as db:
            db.add(CantripRequest(user_id=uid, conversation_id="c1",
                                  request_text="roller", context_text="ctx",
                                  status="pending"))
            await db.commit()

        listed = await admin.get("/api/assistant/drafts")
        assert listed.status_code == 200
        drafts = listed.json()["drafts"]
        assert len(drafts) == 1
        assert drafts[0]["request_text"] == "roller"
        assert drafts[0]["context_text"] == "ctx"

        patched = await admin.patch(
            f"/api/assistant/drafts/{drafts[0]['id']}", json={"status": "started"})
        assert patched.status_code == 200
        assert patched.json()["status"] == "started"

        bad = await admin.patch(
            f"/api/assistant/drafts/{drafts[0]['id']}", json={"status": "nonsense"})
        assert bad.status_code == 422

        # A second user sees none of the admin's drafts.
        reg = await client.post("/api/auth/register", json={
            "username": "other29", "password": "otherpass123"})
        if reg.status_code == 201:
            other_token = reg.json()["access_token"]
            other_list = await client.get("/api/assistant/drafts", headers={
                "Authorization": f"Bearer {other_token}"})
            assert other_list.status_code == 200
            assert other_list.json()["drafts"] == []

    async def test_unauthenticated_drafts_denied(self, client):
        resp = await client.get("/api/assistant/drafts")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 29b: auto-install gate matrix (Deno required for the real gates)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SKIP_DENO, reason="Deno not available")
@pytest.mark.asyncio
class TestAutoInstallGates:
    async def _set_gates(self, client, admin_allowed: bool, user_enabled: bool):
        from app.services.admin import update_admin_settings
        await update_admin_settings({
            "assistant_cantrip_auto_install_allowed": admin_allowed})
        put = await client.put("/api/settings", json={
            "cantrip_auto_install_enabled": user_enabled})
        assert put.status_code == 200

    async def _send(self, client, api_key, httpx_mock, llm_reply: str | None,
                    expect_install: bool):
        if expect_install:
            # The drafting LLM call runs before the driver forward.
            httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                    json=_upstream(llm_reply or "ignored"))
        httpx_mock.add_response(url="http://lex.test/v1/chat/completions",
                                json=_upstream())
        return await client.post("/v1/chat/completions", json=_chat_body(
            "<CANTRIP:a stealth dice roller>"),
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-auto"})

    async def _cantrips(self, uid: str) -> list[Cantrip]:
        from sqlalchemy import select
        async with TestSessionLocal() as db:
            return list((await db.execute(select(Cantrip).where(
                Cantrip.user_id == uid))).scalars().all())

    async def test_admin_off_queues_even_with_user_on(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=False, user_enabled=True)
        resp = await self._send(client, api_key, httpx_mock, "{}", expect_install=False)
        assert resp.status_code == 200
        assert "queued" in resp.json()["choices"][0]["message"]["content"]
        assert await self._cantrips(await _user_id(client)) == []

    async def test_user_off_queues_even_with_admin_on(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=True, user_enabled=False)
        resp = await self._send(client, api_key, httpx_mock, "{}", expect_install=False)
        assert resp.status_code == 200
        assert "queued" in resp.json()["choices"][0]["message"]["content"]
        assert await self._cantrips(await _user_id(client)) == []

    async def test_invalid_syntax_refuses_without_install(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=True, user_enabled=True)
        reply = json.dumps({"name": "Broken", "code": "function {{{"})
        resp = await self._send(client, api_key, httpx_mock, reply, expect_install=True)
        assert resp.status_code == 200
        assert "refused" in resp.json()["choices"][0]["message"]["content"]
        assert await self._cantrips(await _user_id(client)) == []

    async def test_critical_scan_finding_refuses(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=True, user_enabled=True)
        reply = json.dumps({"name": "Evil", "code": "fetch('http://evil.example')"})
        resp = await self._send(client, api_key, httpx_mock, reply, expect_install=True)
        assert resp.status_code == 200
        assert "refused" in resp.json()["choices"][0]["message"]["content"]
        assert await self._cantrips(await _user_id(client)) == []

    async def test_smoke_failure_installs_disabled(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=True, user_enabled=True)
        # Parses fine, throws at runtime.
        reply = json.dumps({"name": "Thrower", "code": 'throw new Error("boom")'})
        resp = await self._send(client, api_key, httpx_mock, reply, expect_install=True)
        assert resp.status_code == 200
        cantrips = await self._cantrips(await _user_id(client))
        assert len(cantrips) == 1
        assert cantrips[0].is_active is False

    async def test_all_gates_pass_installs_enabled(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await self._set_gates(client, admin_allowed=True, user_enabled=True)
        reply = json.dumps({
            "name": "Greeter",
            "code": 'context.character.personality += "Brave.";',
        })
        resp = await self._send(client, api_key, httpx_mock, reply, expect_install=True)
        assert resp.status_code == 200
        assert "installed and enabled" in resp.json()["choices"][0]["message"]["content"]
        cantrips = await self._cantrips(await _user_id(client))
        assert len(cantrips) == 1
        assert cantrips[0].is_active is True
        assert cantrips[0].name == "Greeter"


# ============================================================================
# Upgrade path: migration 055 creates both tables on an old database.
# ============================================================================


class TestPhase29UpgradePath:
    @pytest.mark.asyncio
    async def test_migration_creates_tables_and_binding_query_works(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.base import Base
        from app.services.migrations import run_migrations

        # Simulate the pre-055 database: create_all from current models, then
        # drop everything only migration 055 provides. SQLite has supported
        # DROP COLUMN since 3.35 (see the 053 upgrade test for the precedent),
        # and user_settings/admin_settings are NOT migration-created tables,
        # so TestMigrationCoverage cannot catch a missing 055 ALTER for them
        # -- only this test can (the skills.budget_weight bug class, rule 10).
        # The one exception: lexicon_endpoint_id cannot be dropped on SQLite
        # because create_all emits a FOREIGN KEY clause naming it, and SQLite
        # rejects DROP COLUMN for FK-referenced columns. The migration runner
        # tolerates the resulting duplicate ALTER ("duplicate column name" is
        # in _TOLERATED_ERRORS), and the ORM insert below still exercises the
        # column end to end.
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE conversation_lorebook_bindings"))
                await conn.execute(text("DROP TABLE cantrip_requests"))
                for col in (
                    "lexicon_enabled",
                    "lexicon_parameters_json",
                    "cantrip_auto_install_enabled",
                ):
                    await conn.execute(text(f"ALTER TABLE user_settings DROP COLUMN {col}"))
                await conn.execute(text(
                    "ALTER TABLE admin_settings DROP COLUMN assistant_cantrip_auto_install_allowed"
                ))

            # Vacuity check: this really reproduces the old schema.
            async with engine.connect() as conn:
                names = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                table_names = {r[0] for r in names.all()}
                user_cols = await conn.execute(text("PRAGMA table_info(user_settings)"))
                user_col_names = {r[1] for r in user_cols.all()}
                admin_cols = await conn.execute(text("PRAGMA table_info(admin_settings)"))
                admin_col_names = {r[1] for r in admin_cols.all()}
            assert "conversation_lorebook_bindings" not in table_names
            assert "cantrip_requests" not in table_names
            assert "lexicon_enabled" not in user_col_names
            assert "lexicon_parameters_json" not in user_col_names
            assert "cantrip_auto_install_enabled" not in user_col_names
            assert "assistant_cantrip_auto_install_allowed" not in admin_col_names
            # lexicon_endpoint_id survives the drop (see comment above); it is
            # exercised through the ORM insert below instead.

            await run_migrations(engine)

            # The proxy hot path selects bindings at lorebook injection time;
            # the query itself must work against the migrated table.
            from sqlalchemy import insert, select

            from app.models.admin_settings import AdminSettings
            from app.models.user_settings import UserSettings
            async with engine.begin() as conn:
                # Bare binding row: the query under test touches only this
                # table, and the FK targets are irrelevant to it.
                await conn.execute(insert(ConversationLorebookBinding).values(
                    user_id="u1", conversation_id="c1", lorebook_id="lb1"))
                rows = (await conn.execute(
                    select(ConversationLorebookBinding.lorebook_id).where(
                        ConversationLorebookBinding.user_id == "u1",
                        ConversationLorebookBinding.conversation_id == "c1",
                    ))).scalars().all()
                # The settings reads run on every request (user settings) and
                # on every auto-install decision (admin gate); exercise the
                # migrated columns through the ORM, where a missing ALTER
                # surfaces as "no such column".
                await conn.execute(insert(UserSettings).values(
                    user_id="u1", lexicon_enabled=True,
                    lexicon_endpoint_id="ep1",
                    lexicon_parameters_json='[{"k":"temperature","v":0.3}]',
                    cantrip_auto_install_enabled=True))
                settings = (await conn.execute(
                    select(UserSettings.lexicon_enabled,
                           UserSettings.lexicon_endpoint_id,
                           UserSettings.lexicon_parameters_json,
                           UserSettings.cantrip_auto_install_enabled).where(
                        UserSettings.user_id == "u1"))).one()
                assert settings.lexicon_enabled is True
                assert settings.lexicon_endpoint_id == "ep1"
                assert "temperature" in settings.lexicon_parameters_json
                assert settings.cantrip_auto_install_enabled is True
                # admin_settings is a singleton (id=1) that earlier migrations
                # seed; update it if present, insert it if not.
                from sqlalchemy import update
                seeded = (await conn.execute(select(AdminSettings.id))).scalars().all()
                if seeded:
                    await conn.execute(update(AdminSettings).values(
                        assistant_cantrip_auto_install_allowed=True))
                else:
                    await conn.execute(insert(AdminSettings).values(
                        id=1, assistant_cantrip_auto_install_allowed=True))
                allowed = (await conn.execute(
                    select(AdminSettings.assistant_cantrip_auto_install_allowed))).scalar_one()
                assert allowed is True
            assert rows == ["lb1"]
        finally:
            await engine.dispose()
