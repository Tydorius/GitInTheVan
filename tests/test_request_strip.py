"""Request strip rules (0.52.0, N1).

Covers the exact JanitorAI banned-words-note sample taken 2026-11-25 (three
consecutive requests from one chat: the newest user message carries the
note, the same message returns without it on the next request, and even a
byte-identical regenerate gets a fresh chat id). With a hash-scope rule the
whole chain resolves to one conversation; without one, every request gets
its own chat id -- the reported failure, kept as the vacuity check.

Forward scope strips the outbound body while still normalizing identity
(forward implies hash), hash scope leaves the forwarded request untouched.
"""

import json
import re

import pytest
from sqlalchemy import select

from app.models.conversation_hash import ConversationHash
from app.services.conversation import (
    compute_hash,
    note_request_shape,
    record_post_hash,
    resolve_conversation,
)
from app.services.request_strip import (
    compile_request_strip_rules,
    janitor_example_rule,
    parse_request_strip_rules,
    strip_forward_messages,
    validate_request_strip_rules,
)
from tests.conftest import TestSessionLocal

USER = "user-reqstrip"

# The exact note text from the 2026-11-25 sample (gitv-n1-samples), appended
# after one newline to the newest user message, no wrapper.
NOTE = (
    '\nSYSTEM NOTE: Do not include the following words/phrases in your '
    'output under any circumstances: "quokka"'
)
JANITOR_PATTERN = (
    r"\n?SYSTEM NOTE: Do not include the following words/phrases "
    r"in your output under any circumstances:.*"
)
assert len("Kyle: Test One" + NOTE) == 119  # sample: message 25, Post 1

_A1 = "Kyle: Test One"
_A2 = "Kyle: Test Two"
_A3 = "Kyle: Test Three"
# The real Post-2 assistant reply from the sample (message 26).
_OOC = (
    "[OOC: Test One received and acknowledged. All system notes, slow-burn "
    "pacing, and stylistic guidelines are active. Ready whenever you want to "
    "continue with Median's actual response!]"
)


def _msg(role: str, content) -> dict:
    return {"role": role, "content": content}


def _history(sys_text: str) -> list[dict]:
    """Messages 0-24 of the sample chat: identical in all three requests
    except the system message, which JanitorAI's own lorebooks rewrote
    between Post 1 and Post 2 (sample side note). Index 0 sits outside the
    4-message hash window, so the drift must not break matching."""
    msgs = [_msg("system", sys_text), _msg("user", " ")]
    for i in range(1, 25):
        msgs.append(_msg("user" if i % 2 else "assistant", f"history line {i}"))
    return msgs


def _post1() -> list[dict]:
    msgs = _history("system prompt v1 (lorebook set A fired)")
    msgs.append(_msg("user", _A1 + NOTE))
    return msgs


def _post2() -> list[dict]:
    msgs = _history("system prompt v2 (lorebook set B fired)")
    msgs.append(_msg("user", _A1))
    msgs.append(_msg("assistant", _OOC))
    msgs.append(_msg("user", _A2 + NOTE))
    return msgs


def _post4() -> list[dict]:
    msgs = _post2()
    msgs.append(_msg("assistant", "narrator reply"))
    msgs.append(_msg("user", _A3 + NOTE))
    return msgs


async def _distinct_chat_ids() -> set[str]:
    async with TestSessionLocal() as db:
        rows = (
            await db.execute(select(ConversationHash.internal_chat_id))
        ).scalars().all()
    return set(rows)


# ============================================================================
# Rule plumbing: validate, compile, parse
# ============================================================================

class TestRulePlumbing:
    def test_example_rule_validates(self):
        cleaned = validate_request_strip_rules([janitor_example_rule()])
        assert cleaned == [janitor_example_rule()]

    def test_rejects_non_compiling_pattern(self):
        with pytest.raises(ValueError, match="does not compile"):
            validate_request_strip_rules([{"pattern": "([", "scope": "hash"}])

    def test_rejects_unknown_scope(self):
        with pytest.raises(ValueError, match="scope"):
            validate_request_strip_rules([{"pattern": "x", "scope": "everywhere"}])

    def test_rejects_malformed_rule(self):
        with pytest.raises(ValueError):
            validate_request_strip_rules(["plain string"])
        with pytest.raises(ValueError):
            validate_request_strip_rules([{"scope": "hash"}])

    def test_rejects_over_caps(self):
        from app.services.response_tags import MAX_STRIP_PATTERN_LEN, MAX_STRIP_PATTERNS
        with pytest.raises(ValueError, match="at most"):
            validate_request_strip_rules(
                [{"pattern": f"p{i}", "scope": "hash"} for i in range(MAX_STRIP_PATTERNS + 1)]
            )
        with pytest.raises(ValueError, match="256"):
            validate_request_strip_rules(
                [{"pattern": "a" * (MAX_STRIP_PATTERN_LEN + 1), "scope": "hash"}]
            )

    def test_compile_forward_implies_hash(self):
        stored = json.dumps([
            {"pattern": JANITOR_PATTERN, "scope": "hash"},
            {"pattern": r"^\[junk\]", "scope": "forward"},
        ])
        compiled = compile_request_strip_rules(stored)
        assert len(compiled["hash"]) == 2
        assert len(compiled["forward"]) == 1

    def test_compile_degrades_on_junk(self):
        assert compile_request_strip_rules(None) == {"hash": [], "forward": []}
        assert compile_request_strip_rules("not json") == {"hash": [], "forward": []}
        assert compile_request_strip_rules(json.dumps(
            [{"pattern": "([", "scope": "hash"}])) == {"hash": [], "forward": []}

    def test_parse_round_trip(self):
        stored = json.dumps([janitor_example_rule()])
        assert parse_request_strip_rules(stored) == [janitor_example_rule()]
        assert parse_request_strip_rules(None) == []


# ============================================================================
# Hash normalization on the sample strings
# ============================================================================

class TestHashNormalization:
    def test_noted_and_plain_hash_equal_with_rule(self):
        rules = [re.compile(JANITOR_PATTERN)]
        noted = [_msg("user", _A1 + NOTE)]
        plain = [_msg("user", _A1)]
        assert compute_hash(noted, strip_patterns=rules) == compute_hash(
            plain, strip_patterns=rules
        )

    def test_without_rule_hashes_differ_vacuity(self):
        """The reported failure mode, reproduced: without rules the noted and
        plain versions of the same message hash differently."""
        noted = [_msg("user", _A1 + NOTE)]
        plain = [_msg("user", _A1)]
        assert compute_hash(noted) != compute_hash(plain)

    def test_empty_patterns_exact_historical_hash(self):
        noted = [_msg("user", _A1 + NOTE)]
        assert compute_hash(noted, strip_patterns=[]) == compute_hash(noted)
        assert compute_hash(noted, strip_patterns=None) == compute_hash(noted)

    def test_multimodal_content_parts(self):
        rules = [re.compile(JANITOR_PATTERN)]
        noted = [_msg("user", [
            {"type": "text", "text": _A1 + NOTE},
            {"type": "image_url", "image_url": {"url": "http://x"}},
        ])]
        plain = [_msg("user", [
            {"type": "text", "text": _A1},
            {"type": "image_url", "image_url": {"url": "http://x"}},
        ])]
        assert compute_hash(noted, strip_patterns=rules) == compute_hash(
            plain, strip_patterns=rules
        )


# ============================================================================
# The sample scenario at the service level: resolve + record chain
# ============================================================================

class TestSampleChain:
    @pytest.mark.asyncio
    async def test_one_chat_id_across_note_movements(self):
        rules = [re.compile(JANITOR_PATTERN)]

        chat1, new1 = await resolve_conversation(_post1(), USER, strip_patterns=rules)
        assert new1
        await record_post_hash(_post1(), "reply one", chat1, USER, strip_patterns=rules)

        # Post 2: message 25 returns WITHOUT the note (sample finding 2).
        chat2, new2 = await resolve_conversation(_post2(), USER, strip_patterns=rules)
        assert not new2
        assert chat2 == chat1
        await record_post_hash(_post2(), "reply two", chat2, USER, strip_patterns=rules)

        # Regenerate: byte-identical to Post 2 (sample finding 5) and still
        # chains -- it matches Post 1's recorded hash over the [22..25] slice.
        chat3, new3 = await resolve_conversation(_post2(), USER, strip_patterns=rules)
        assert not new3
        assert chat3 == chat1

        # The turn after: Post 2's recorded hash covers [24..27] normalized.
        chat4, new4 = await resolve_conversation(_post4(), USER, strip_patterns=rules)
        assert not new4
        assert chat4 == chat1

    @pytest.mark.asyncio
    async def test_without_rules_every_request_is_a_new_chat(self):
        """Vacuity for the chain test: with no rules the sample's three chat
        ids reproduce (the bug the reporter saw)."""
        chat1, _ = await resolve_conversation(_post1(), USER)
        await record_post_hash(_post1(), "reply one", chat1, USER)

        chat2, new2 = await resolve_conversation(_post2(), USER)
        assert new2
        assert chat2 != chat1

        chat3, new3 = await resolve_conversation(_post2(), USER)
        assert new3
        assert chat3 not in (chat1, chat2)


# ============================================================================
# Forward scope: outbound body stripped, input never mutated
# ============================================================================

class TestForwardStrip:
    def test_string_content_stripped_and_input_untouched(self):
        msgs = [_msg("system", "sys"), _msg("user", _A1 + NOTE)]
        before = json.dumps(msgs)
        new_msgs, changed = strip_forward_messages(
            msgs, [re.compile(JANITOR_PATTERN)]
        )
        assert changed == [1]
        assert new_msgs[1]["content"] == _A1
        assert new_msgs[0]["content"] == "sys"
        assert json.dumps(msgs) == before  # the hash anchor is never touched

    def test_content_parts_list(self):
        msgs = [_msg("user", [
            {"type": "text", "text": _A1 + NOTE},
            {"type": "image_url", "image_url": {"url": "http://x"}},
        ])]
        new_msgs, changed = strip_forward_messages(
            msgs, [re.compile(JANITOR_PATTERN)]
        )
        assert changed == [0]
        assert new_msgs[0]["content"][0]["text"] == _A1
        assert new_msgs[0]["content"][1]["image_url"]["url"] == "http://x"

    def test_no_match_leaves_message_identity(self):
        msgs = [_msg("user", _A1)]
        new_msgs, changed = strip_forward_messages(
            msgs, [re.compile(JANITOR_PATTERN)]
        )
        assert changed == []
        assert new_msgs == msgs

    def test_no_patterns_is_identity(self):
        msgs = [_msg("user", _A1 + NOTE)]
        new_msgs, changed = strip_forward_messages(msgs, [])
        assert changed == []
        assert new_msgs == msgs


# ============================================================================
# Regenerate fingerprint: a changed note must not hide a regenerate
# ============================================================================

class TestRegenerateFingerprint:
    @pytest.mark.asyncio
    async def test_note_change_does_not_hide_regenerate(self):
        from app.services.conversation import bump_turn_counter

        rules = [re.compile(JANITOR_PATTERN)]
        conv = "conv-fp"
        first = [_msg("user", _A2 + NOTE)]
        edited_note = (
            '\nSYSTEM NOTE: Do not include the following words/phrases in '
            'your output under any circumstances: "quokka", "axolotl"'
        )
        second = [_msg("user", _A2 + edited_note)]

        # The proxy always bumps the turn counter before noting the shape
        # (note_request_shape only ever updates; the bump creates the row).
        await bump_turn_counter(USER, conv)
        assert not await note_request_shape(USER, conv, first, strip_patterns=rules)
        await bump_turn_counter(USER, conv)
        assert await note_request_shape(USER, conv, second, strip_patterns=rules)


# ============================================================================
# Settings API: validation and round trip
# ============================================================================

class TestSettingsAPI:
    @pytest.mark.asyncio
    async def test_round_trip(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["request_strip_patterns"] == []

        resp = await client.put("/api/settings", json={"request_strip_patterns": [
            janitor_example_rule(),
            {"pattern": r"^\[junk\]", "scope": "forward"},
        ]})
        assert resp.status_code == 200, resp.text

        resp = await client.get("/api/settings")
        rules = resp.json()["request_strip_patterns"]
        assert janitor_example_rule() in rules
        assert {"pattern": r"^\[junk\]", "scope": "forward"} in rules

    @pytest.mark.asyncio
    async def test_non_compiling_pattern_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={"request_strip_patterns": [
            {"pattern": "([", "scope": "hash"}]})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_scope_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={"request_strip_patterns": [
            {"pattern": "x", "scope": "everywhere"}]})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_malformed_rule_422(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/settings", json={
            "request_strip_patterns": ["plain string"]})
        assert resp.status_code == 422


# ============================================================================
# End to end through the proxy
# ============================================================================

class TestProxyEndToEnd:
    async def _setup_endpoint(self, client):
        resp = await client.post("/api/endpoints", json={
            "name": "reqstrip", "base_url": "http://reqstrip.test",
            "api_key": "k", "enabled": True})
        assert resp.status_code == 201, resp.text
        await client.put("/api/settings", json={
            "default_endpoint_id": resp.json()["id"]})

    @staticmethod
    def _upstream_ok():
        return {"choices": [{"index": 0, "message": {
            "role": "assistant", "content": "ok"}}]}

    @pytest.mark.asyncio
    async def test_hash_rule_chains_and_forwarded_body_untouched(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await self._setup_endpoint(client)
        resp = await client.put("/api/settings", json={
            "request_strip_patterns": [janitor_example_rule()]})
        assert resp.status_code == 200

        upstream = "http://reqstrip.test/v1/chat/completions"
        for _ in range(2):
            httpx_mock.add_response(url=upstream, json=self._upstream_ok())

        r1 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post1()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r1.status_code == 200, r1.text
        r2 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post2()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r2.status_code == 200, r2.text

        # Hash scope: the model still sees the note (JanitorAI wants it seen).
        sent = json.loads(httpx_mock.get_requests()[-1].content)
        assert "SYSTEM NOTE" in sent["messages"][-1]["content"]
        assert len(await _distinct_chat_ids()) == 1

    @pytest.mark.asyncio
    async def test_without_rules_two_chat_ids_vacuity(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await self._setup_endpoint(client)

        upstream = "http://reqstrip.test/v1/chat/completions"
        for _ in range(2):
            httpx_mock.add_response(url=upstream, json=self._upstream_ok())

        r1 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post1()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r1.status_code == 200, r1.text
        r2 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post2()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r2.status_code == 200, r2.text

        assert len(await _distinct_chat_ids()) == 2

    @pytest.mark.asyncio
    async def test_forward_rule_strips_outbound_and_chains(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await self._setup_endpoint(client)
        resp = await client.put("/api/settings", json={
            "request_strip_patterns": [{
                "pattern": JANITOR_PATTERN, "scope": "forward"}]})
        assert resp.status_code == 200

        upstream = "http://reqstrip.test/v1/chat/completions"
        for _ in range(2):
            httpx_mock.add_response(url=upstream, json=self._upstream_ok())

        r1 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post1()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r1.status_code == 200, r1.text
        r2 = await client.post("/v1/chat/completions", json={
            "model": "m", "messages": _post2()},
            headers={"Authorization": f"Bearer {api_key}"})
        assert r2.status_code == 200, r2.text

        # Forward scope: the upstream never sees the note, and identity
        # still chains (forward implies hash).
        sent = json.loads(httpx_mock.get_requests()[-1].content)
        assert "SYSTEM NOTE" not in sent["messages"][-1]["content"]
        assert sent["messages"][-1]["content"] == _A2
        assert len(await _distinct_chat_ids()) == 1


# ============================================================================
# Upgrade path: a 0.51.0-era database gains the column cleanly
# ============================================================================

class TestUpgradePath:
    @pytest.mark.asyncio
    async def test_column_added_to_pre075_database(self, tmp_path):
        """user_settings predates the migration system (exempt from
        TestMigrationCoverage), so the upgrade needs its own proof: build the
        0.51.0 table without the column, migrate, verify the column exists
        and reads as no rules."""
        from sqlalchemy import MetaData, Table, text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.user_settings import UserSettings
        from app.services.migrations import run_migrations

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
        )
        try:
            old_meta = MetaData()
            old_table = Table("user_settings", old_meta)
            for col in UserSettings.__table__.columns:
                if col.name != "request_strip_patterns":
                    old_table.append_column(col.copy())

            def _build(sync_conn):
                old_meta.create_all(sync_conn)

            async with engine.begin() as conn:
                await conn.run_sync(_build)
                # Vacuity: the legacy table really lacks the column.
                cols = {
                    row[1]
                    for row in (
                        await conn.execute(text("PRAGMA table_info(user_settings)"))
                    ).fetchall()
                }
                assert "request_strip_patterns" not in cols

                # A pre-075 row with the model's Python defaults. The Core
                # insert executes Python-side defaults only for plain
                # ColumnDefaults; the uuid primary key needs an explicit value.
                import uuid as _uuid

                legacy = UserSettings(user_id="legacy-user")
                values = {
                    c.name: getattr(legacy, c.name)
                    for c in old_table.columns
                }
                if values.get("id") is None:
                    values["id"] = str(_uuid.uuid4())
                values = {
                    k: v for k, v in values.items() if v is not None
                }
                await conn.execute(old_table.insert().values(**values))

            await run_migrations(engine)

            async with engine.begin() as conn:
                cols = {
                    row[1]
                    for row in (
                        await conn.execute(text("PRAGMA table_info(user_settings)"))
                    ).fetchall()
                }
                assert "request_strip_patterns" in cols
                stored = (
                    await conn.execute(text(
                        "SELECT request_strip_patterns FROM user_settings "
                        "WHERE user_id = 'legacy-user'"
                    ))
                ).scalar_one()
                # Existing rows read as no rules, whether the dialect
                # backfilled the DEFAULT ('[]') or left NULL.
                assert parse_request_strip_rules(stored) == []
        finally:
            await engine.dispose()
