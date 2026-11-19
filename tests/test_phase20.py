"""Phase 20 — dynamic lorebooks (``context.lorebook``).

Covers the sandbox facade (op collection), host-side op application with the
rule-18 guards, the conversation binding (fifth source) for script-created
books, cross-conversation tagged books, same-turn and next-turn injection,
read-only seeding (``lorebook_reads``), isolation, the op cap, jslorebook
execution, the post-driver body-key regression, and the upgrade path
(migration 059).

E2E tests require Deno (a real cantrip run); unit tests do not.
"""

from __future__ import annotations

import json

import pytest

from app.services.deno_runner import DENO_PATH
from app.services.dynamic_lorebook import parse_reads
from tests.conftest import TestSessionLocal

MOCK_ENDPOINT_URL = "http://phase20-e2e.test"

requires_deno = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")


# ============================================================================
# Helpers
# ============================================================================

async def _make_e2e_endpoint(client):
    resp = await client.post("/api/endpoints", json={
        "name": "Phase 20 E2E",
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


async def _send(client, api_key, *, conversation: str | None = "chat-a",
                content: str = "hello", extra_headers=None):
    headers = {"Authorization": f"Bearer {api_key}"}
    if conversation is not None:
        headers["x-conversation-id"] = conversation
    if extra_headers:
        headers.update(extra_headers)
    return await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": content}]},
        headers=headers,
    )


def _forwarded_text(httpx_mock) -> str:
    """The LATEST forwarded request's messages (multi-request tests ask for
    the most recent turn, not the first)."""
    requests = httpx_mock.get_requests()
    return json.dumps(json.loads(requests[-1].content)["messages"])


async def _books(client) -> dict[str, dict]:
    resp = await client.get("/api/lorebooks")
    assert resp.status_code == 200, resp.text
    return {b["name"]: b for b in resp.json()["lorebooks"]}


# ============================================================================
# Unit: parse_reads
# ============================================================================

class TestParseReads:
    def test_none_and_empty(self):
        assert parse_reads(None) == []
        assert parse_reads("") == []

    def test_malformed_degrades_to_empty(self):
        assert parse_reads("not json") == []
        assert parse_reads('{"a": 1}') == []

    def test_names_and_cap(self):
        assert parse_reads('["a", " b ", "a"]') == ["a", "b"]
        many = json.dumps([f"book-{i}" for i in range(30)])
        assert len(parse_reads(many)) == 20


# ============================================================================
# Unit: op application (no Deno)
# ============================================================================

_CREATE_OPS = [
    {"op": "create", "book": "notes", "description": "", "tag": ""},
    {"op": "add", "book": "notes", "entry": {
        "name": "dragon", "keys": ["dragon"], "content": "Dragons fear silver.",
    }},
]


class TestApplyOpsUnit:
    async def _user(self, client) -> str:
        return (await client.get("/api/auth/me")).json()["id"]

    async def test_ops_apply_and_bind_to_the_conversation(self, admin_client):
        from sqlalchemy import select

        from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        async with TestSessionLocal() as db:
            delta = await apply_ops(
                db, {}, uid, "conv-1", list(_CREATE_OPS), source="unit-test"
            )
            assert len(delta) == 1
            assert delta[0].content == "Dragons fear silver."
            row = (await db.execute(
                select(ConversationDynamicLorebook).where(
                    ConversationDynamicLorebook.user_id == uid,
                    ConversationDynamicLorebook.conversation_id == "conv-1",
                    ConversationDynamicLorebook.name == "notes",
                )
            )).scalar_one()
            assert row.lorebook_id == delta[0].lorebook_id

    async def test_tagged_create_binds_to_the_sentinel_row(self, admin_client):
        from sqlalchemy import select

        from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        ops = [{"op": "create", "book": "shared", "tag": "dyn"}]
        async with TestSessionLocal() as db:
            await apply_ops(db, {}, uid, "conv-1", ops, source="unit-test")
            row = (await db.execute(
                select(ConversationDynamicLorebook).where(
                    ConversationDynamicLorebook.user_id == uid,
                    ConversationDynamicLorebook.name == "shared",
                )
            )).scalar_one()
            # "" is the sentinel for an unbound (cross-conversation) book.
            assert row.conversation_id == ""

    async def test_sandbox_and_replay_drop_all_ops(self, admin_client):
        from sqlalchemy import select

        from app.models.lorebook import Lorebook

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        for flag in ("_gitv_sandbox", "_gitv_replay"):
            async with TestSessionLocal() as db:
                delta = await apply_ops(
                    db, {flag: True}, uid, "conv-iso", list(_CREATE_OPS), source="unit-test"
                )
                assert delta == []
                books = (await db.execute(
                    select(Lorebook).where(Lorebook.user_id == uid, Lorebook.name == "notes")
                )).scalars().all()
                assert books == [], f"{flag} must not write"

    async def test_unknown_book_refused_nothing_written(self, admin_client):
        from sqlalchemy import select

        from app.models.lorebook import Lorebook

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        ops = [{"op": "add", "book": "ghost", "entry": {
            "name": "x", "keys": ["x"], "content": "nope",
        }}]
        async with TestSessionLocal() as db:
            delta = await apply_ops(db, {}, uid, "conv-1", ops, source="unit-test")
            assert delta == []
            assert (await db.execute(
                select(Lorebook).where(Lorebook.user_id == uid)
            )).scalars().all() == []

    async def test_add_upserts_by_name(self, admin_client):
        from sqlalchemy import select

        from app.models.lorebook_entry import LorebookEntry

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        ops = [
            {"op": "create", "book": "notes", "tag": ""},
            {"op": "add", "book": "notes", "entry": {
                "name": "dragon", "keys": ["dragon"], "content": "first",
            }},
            {"op": "add", "book": "notes", "entry": {
                "name": "dragon", "keys": ["dragon"], "content": "second",
            }},
        ]
        async with TestSessionLocal() as db:
            await apply_ops(db, {}, uid, "conv-up", ops, source="unit-test")
        async with TestSessionLocal() as db:
            rows = (await db.execute(
                select(LorebookEntry).order_by(LorebookEntry.created_at)
            )).scalars().all()
            dragons = [r for r in rows if r.name == "dragon"]
            assert len(dragons) == 1, "a re-run replaces its entry instead of duplicating it"
            assert dragons[0].content == "second"

    async def test_update_snapshots_first(self, admin_client):
        from sqlalchemy import select

        from app.models.resource_snapshot import ResourceSnapshot

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import apply_ops

        # The add and the update are separate batches: an update merged into
        # a same-batch add is a last-write-wins merge of a not-yet-written
        # entry (nothing exists to snapshot). Only a real update of a stored
        # entry owes a pre_edit snapshot.
        async with TestSessionLocal() as db:
            await apply_ops(db, {}, uid, "conv-snap", list(_CREATE_OPS), source="t")
        async with TestSessionLocal() as db:
            await apply_ops(db, {}, uid, "conv-snap", [
                {"op": "update", "book": "notes", "entry": "dragon",
                 "fields": {"content": "Dragons fear nothing."}},
            ], source="t")
        async with TestSessionLocal() as db:
            snaps = (await db.execute(
                select(ResourceSnapshot).where(
                    ResourceSnapshot.user_id == uid,
                    ResourceSnapshot.resource_type == "lorebook",
                )
            )).scalars().all()
            assert snaps, "an update must leave a Phase 25 pre_edit snapshot"

    async def test_per_request_op_cap(self, admin_client):
        from sqlalchemy import select

        from app.models.lorebook_entry import LorebookEntry

        client, _, _ = admin_client
        uid = await self._user(client)

        from app.services.dynamic_lorebook import MAX_OPS_PER_REQUEST, apply_ops

        ops = [{"op": "create", "book": "flood", "tag": ""}] + [
            {"op": "add", "book": "flood", "entry": {
                "name": f"e{i}", "keys": [f"e{i}"], "content": "x",
            }}
            for i in range(MAX_OPS_PER_REQUEST + 10)
        ]
        body: dict = {}
        async with TestSessionLocal() as db:
            await apply_ops(db, body, uid, "conv-cap", ops, source="unit-test")
        async with TestSessionLocal() as db:
            count = len((await db.execute(select(LorebookEntry))).scalars().all())
        # The create op consumes one of the 50; the 51st op onward is refused.
        assert count == MAX_OPS_PER_REQUEST - 1, f"expected the cap minus the create, got {count}"


class TestBookSeedCap:
    def _book(self, content: str):
        from app.models.lorebook import Lorebook
        from app.models.lorebook_entry import LorebookEntry

        lb = Lorebook(user_id="u", name="Big", is_active=True)
        for i in range(5):
            lb.entries.append(LorebookEntry(name=f"e{i}", content=content,
                                            is_constant=True))
        return lb

    def test_oversize_book_truncates_from_the_end(self):
        from app.services.dynamic_lorebook import _book_seed

        book = self._book(content="x" * 600)  # 5 entries x 600 = 3000 chars
        seed, truncated = _book_seed(book, writable=True, cap_chars=1300)
        assert truncated is True
        # Newest-kept: the earliest entries survive, the tail is dropped.
        assert len(seed["entries"]) == 2
        assert seed["entries"][0]["name"] == "e0"

    def test_book_under_the_cap_seed_whole(self):
        from app.services.dynamic_lorebook import _book_seed

        book = self._book(content="x" * 10)
        seed, truncated = _book_seed(book, writable=True, cap_chars=10_000)
        assert truncated is False
        assert len(seed["entries"]) == 5


# ============================================================================
# E2E through the real pipeline (Deno required)
# ============================================================================

_WRITER_CANTRIP = (
    'const lb = context.lorebook.create({name: "Chat Notes"});'
    'lb.add({name: "dragon", keys: ["dragon"], content: "DYNAMIC_DRAGON_LORE",'
    ' is_constant: true});'
)

_READER_CANTRIP = (
    'const lb = context.lorebook.get("Chat Notes");'
    'const n = lb ? lb.entries().length : -1;'
    'context.character.scenario += " READ_COUNT_" + n;'
)


@requires_deno
class TestDynamicLorebooksE2E:
    async def test_same_turn_injection(self, admin_client, httpx_mock):
        """The headline claim: an entry a cantrip writes this turn reaches the
        model this turn."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.post("/api/cantrips", json={
            "name": "Dynamic Writer", "code": _WRITER_CANTRIP, "is_active": True,
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key)
        assert resp.status_code == 200, resp.text
        assert "DYNAMIC_DRAGON_LORE" in _forwarded_text(httpx_mock)

    async def test_next_turn_and_conversation_isolation(
        self, admin_client, httpx_mock
    ):
        """The written book is bound to its conversation (fifth source): it
        injects on later requests in the same chat and in no other. The
        writer cantrip is tag-gated so only chat-a's first request runs it --
        later requests prove the injection, not a fresh write."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.post("/api/cantrips", json={
            "name": "Dynamic Writer", "code": _WRITER_CANTRIP,
            "is_active": False, "tag": "writer",
        })

        _upstream(httpx_mock)
        resp = await _send(
            client, api_key, conversation="chat-a",
            content="hello <#cantrip-writer#>",
        )
        assert resp.status_code == 200, resp.text
        assert "DYNAMIC_DRAGON_LORE" in _forwarded_text(httpx_mock)

        # No tag: the cantrip does not run; the book injects via the binding.
        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-a")
        assert resp.status_code == 200, resp.text
        assert "DYNAMIC_DRAGON_LORE" in _forwarded_text(httpx_mock)

        # A different conversation gets nothing (no cantrip run, no binding).
        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-b")
        assert resp.status_code == 200, resp.text
        assert "DYNAMIC_DRAGON_LORE" not in _forwarded_text(httpx_mock)

    async def test_cross_conversation_tagged_book(self, admin_client, httpx_mock):
        """create({tag}) makes an unbound book activated by its own tag in any
        chat (rule 19 source 3), regardless of its Active flag."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.post("/api/cantrips", json={
            "name": "Tagged Writer",
            "code": (
                'const lb = context.lorebook.create({name: "Shared Codex", tag: "codex"});'
                'lb.add({name: "rune", keys: ["rune"], content: "TAGGED_CODEX_ENTRY",'
                ' is_constant: true});'
            ),
            "is_active": False, "tag": "scribe",
        })

        _upstream(httpx_mock)
        resp = await _send(
            client, api_key, conversation="chat-a",
            content="hello <#cantrip-scribe#>",
        )
        assert resp.status_code == 200, resp.text
        assert "TAGGED_CODEX_ENTRY" in _forwarded_text(httpx_mock)

        # Another conversation activates the unbound book through its tag
        # alone -- no cantrip run involved.
        _upstream(httpx_mock)
        resp = await _send(
            client, api_key, conversation="chat-z",
            content="hello <#lore-codex#>",
        )
        assert resp.status_code == 200, resp.text
        assert "TAGGED_CODEX_ENTRY" in _forwarded_text(httpx_mock)

        # ...and a chat without the tag gets nothing.
        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-y")
        assert resp.status_code == 200, resp.text
        assert "TAGGED_CODEX_ENTRY" not in _forwarded_text(httpx_mock)

    async def test_sequential_cantrips_see_applied_ops(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        # Order matters: the writer runs first and the reader counts entries.
        await client.post("/api/cantrips", json={
            "name": "A-Writer", "code": _WRITER_CANTRIP,
            "is_active": True, "execution_order": 1,
        })
        await client.post("/api/cantrips", json={
            "name": "B-Reader", "code": _READER_CANTRIP,
            "is_active": True, "execution_order": 2,
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-seq")
        assert resp.status_code == 200, resp.text
        assert "READ_COUNT_1" in _forwarded_text(httpx_mock)

    async def test_lorebook_reads_seed_static_books_read_only(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)

        resp = await client.post("/api/lorebooks", json={"name": "World Codex"})
        assert resp.status_code in (200, 201), resp.text
        codex_id = resp.json()["id"]
        resp = await client.post(f"/api/lorebooks/{codex_id}/entries", json={
            "name": "capital", "content": "STATIC_CODEX_ENTRY", "is_constant": True,
        })
        assert resp.status_code == 201, resp.text

        await client.post("/api/cantrips", json={
            "name": "Codex Reader",
            "code": (
                'const lb = context.lorebook.get("World Codex");'
                'const hit = lb ? lb.search("capital").length : 0;'
                'context.character.scenario += " CODEX_HITS_" + hit;'
                'if (lb) lb.add({name: "hack", keys: ["hack"], content: "SHOULD_NOT_EXIST"});'
            ),
            "is_active": True,
            "lorebook_reads": ["World Codex"],
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-read")
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "CODEX_HITS_1" in text, "the configured book must be seeded for reads"
        assert "STATIC_CODEX_ENTRY" in text, "the static book still injects normally"

        # The write against a read-only seed was refused.
        books = await _books(client)
        assert books["World Codex"]["entry_count"] == 1, \
            "the write against a read-only seed must be refused"

    async def test_size_guard_refuses_and_degrades(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        # Entry content deliberately larger than the 1 KB cap below.
        big_content = "DYNAMIC_OVERSIZE_" + "x" * 2048
        await client.post("/api/cantrips", json={
            "name": "Big Writer",
            "code": (
                'const lb = context.lorebook.create({name: "Chat Notes"});'
                f'lb.add({{name: "big", keys: ["big"], content: "{big_content}",'
                ' is_constant: true});'
            ),
            "is_active": True,
        })

        caps = await client.get("/api/admin/settings")
        assert caps.status_code == 200, caps.text
        original = caps.json().get("max_lorebook_size_kb")
        put = await client.put("/api/admin/settings", json={"max_lorebook_size_kb": 1})
        assert put.status_code == 200, put.text
        try:
            _upstream(httpx_mock)
            resp = await _send(client, api_key, conversation="chat-size")
            # The refusal must degrade: the reply succeeds, nothing written.
            assert resp.status_code == 200, resp.text
        finally:
            await client.put("/api/admin/settings",
                             json={"max_lorebook_size_kb": original})

        books = await _books(client)
        assert "Chat Notes" in books, "the create op applied (book exists)"
        assert books["Chat Notes"]["entry_count"] == 0, "the oversize add was refused"

    async def test_scanner_refuses_script_tag_content(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.post("/api/cantrips", json={
            "name": "Evil Writer",
            "code": (
                'const lb = context.lorebook.create({name: "Evil Notes"});'
                'lb.add({name: "x", keys: ["x"], content:'
                ' "<script>alert(1)</script> payload"});'
            ),
            "is_active": True,
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-evil")
        assert resp.status_code == 200, resp.text

        books = await _books(client)
        assert books["Evil Notes"]["entry_count"] == 0

    async def test_debug_timeline_carries_the_three_stages(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.put("/api/settings", json={"debug_mode": True})
        uid = (await client.get("/api/auth/me")).json()["id"]
        await client.post("/api/cantrips", json={
            "name": "Dynamic Writer", "code": _WRITER_CANTRIP, "is_active": True,
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-debug")
        assert resp.status_code == 200, resp.text

        # Second request: the bound book now exists, so the seeding capture
        # fires too (on the first request there was nothing to seed).
        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-debug")
        assert resp.status_code == 200, resp.text

        from app.services.debug import get_exchange, latest_exchange_id

        exchange = await get_exchange(uid, await latest_exchange_id(uid))
        assert exchange is not None, "a debug run must exist"
        stages = exchange.get("pipeline_data", {}).get("stages", [])
        stage_names = [s.get("name") for s in stages]
        assert "dynamic_lorebook_ops" in stage_names
        assert "dynamic_lorebook_injection" in stage_names
        assert "lorebook_seeding" in stage_names

    async def test_metrics_carry_entry_provenance(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        await client.post("/api/cantrips", json={
            "name": "Dynamic Writer", "code": _WRITER_CANTRIP, "is_active": True,
        })

        _upstream(httpx_mock)
        resp = await _send(client, api_key, conversation="chat-metrics")
        assert resp.status_code == 200, resp.text

        resp = await client.get("/api/metrics/prompt-objects?limit=50")
        assert resp.status_code == 200, resp.text
        rows = resp.json()["rows"]
        dyn = [r for r in rows if r.get("object_type") == "lorebook_entry"
               and r.get("object_id")]
        assert dyn, "dynamic entries must carry their real entry id for provenance"


# ============================================================================
# jslorebook execution (Deno required)
# ============================================================================

@requires_deno
class TestJslorebookExecution:
    async def test_embedded_script_runs_and_block_is_stripped(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)

        _upstream(httpx_mock)
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": (
                "hello "
                "<jslorebook>const lb = context.lorebook.create({name: 'JSLB Book'});"
                "lb.add({name: 'goblin', keys: ['goblin'],"
                " content: 'JSLB_GOBLIN_ENTRY', is_constant: true});</jslorebook>"
            )}]},
            headers={"Authorization": f"Bearer {api_key}",
                     "x-conversation-id": "chat-jslb"},
        )
        assert resp.status_code == 200, resp.text

        text = _forwarded_text(httpx_mock)
        assert "JSLB_GOBLIN_ENTRY" in text, "the embedded script must run and inject"
        assert "jslorebook" not in text.lower(), "the block must be stripped"


# ============================================================================
# Post-driver body-key regression (the _gitv_-strip fix)
# ============================================================================

@requires_deno
class TestPostDriverBodyKeys:
    async def test_headerless_post_driver_store_write_persists(
        self, admin_client, httpx_mock
    ):
        """0.33.0 claimed header-less clients' post-driver chat_data writes
        were fixed; the stripped body copy at both call sites still dropped
        them. A response-side store write in request 1 must be readable by
        the pre-driver pass of request 2, with no conversation header at all.

        The requests use realistic rolling-hash bodies: after request 1 the
        proxy records hash(request-1 messages) as the chaining anchor, and
        request 2 resolves by hashing everything before its trailing
        assistant+user pair -- so request 2's prior window must be exactly
        request 1's full message list, the way real clients resend history."""
        client, _, api_key = admin_client
        await _make_e2e_endpoint(client)
        # One cantrip, two positions: the response side writes the store
        # (context.response only exists there), the prompt side reads it.
        await client.post("/api/cantrips", json={
            "name": "Store Keeper",
            "code": (
                'if (context.response) { context.chat_data.set("phase20_key", "saved"); }'
                'else { const v = context.chat_data.get("phase20_key");'
                ' context.character.scenario += v ? " STORE_PRESENT" : " STORE_MISSING"; }'
            ),
            "is_active": True, "run_pre_driver": True, "run_pre_navigator": True,
        })

        headers = {"Authorization": f"Bearer {api_key}"}

        _upstream(httpx_mock)
        resp = await client.post("/v1/chat/completions", json={"model": "m", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "hello"},
        ]}, headers=headers)
        assert resp.status_code == 200, resp.text

        _upstream(httpx_mock)
        resp = await client.post("/v1/chat/completions", json={"model": "m", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "hello again"},
        ]}, headers=headers)
        assert resp.status_code == 200, resp.text
        text = _forwarded_text(httpx_mock)
        assert "STORE_MISSING" not in text
        assert "STORE_PRESENT" in text


# ============================================================================
# Creator template
# ============================================================================

class TestCreatorTemplate:
    def test_template_ships_with_position_flags(self):
        from app.templates.cantrip_templates import get_templates

        t = next(x for x in get_templates() if x["name"] == "Dynamic Lorebook Creator")
        assert t["run_pre_navigator"] is True
        assert t.get("run_pre_driver", True) is False
        assert "context.lorebook" in t["code"]

    async def test_install_honors_position_flags(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips/templates/install", json={
            "template_name": "Dynamic Lorebook Creator",
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["run_pre_navigator"] is True
        assert body["run_pre_driver"] is False


# ============================================================================
# API surface
# ============================================================================

class TestLorebookReadsApi:
    async def test_round_trip_and_normalization(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/cantrips", json={
            "name": "Reader", "code": "",
            "lorebook_reads": ["  Alpha ", "Beta", "Alpha", ""],
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["lorebook_reads"] == ["Alpha", "Beta"]

        cid = resp.json()["id"]
        resp = await client.put(f"/api/cantrips/{cid}", json={"lorebook_reads": ["Gamma"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["lorebook_reads"] == ["Gamma"]

        resp = await client.get(f"/api/cantrips/{cid}")
        assert resp.json()["lorebook_reads"] == ["Gamma"]

    async def test_list_carries_reads(self, admin_client):
        client, _, _ = admin_client
        await client.post("/api/cantrips", json={
            "name": "Listed", "code": "", "lorebook_reads": ["Alpha"],
        })
        resp = await client.get("/api/cantrips")
        listed = {c["name"]: c for c in resp.json()["cantrips"]}
        assert listed["Listed"]["lorebook_reads"] == ["Alpha"]


# ============================================================================
# Upgrade path (migration 059)
# ============================================================================

class TestUpgradePath:
    @staticmethod
    async def _pre059_engine():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models import Base

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Roll back to the 0.36.x shape: no lorebook_reads column, no
            # companion table.
            await conn.execute(text("ALTER TABLE cantrips DROP COLUMN lorebook_reads;"))
            await conn.execute(text("DROP TABLE conversation_dynamic_lorebooks;"))
        return engine

    @staticmethod
    async def _columns(engine, table: str) -> list[str]:
        from sqlalchemy import inspect

        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sync: [c["name"] for c in inspect(sync).get_columns(table)]
            )

    async def test_fixture_really_reproduces_the_pre059_state(self):
        """Vacuity check: the column and table must be genuinely absent before
        migrating, or this class proves nothing."""
        from sqlalchemy import inspect

        engine = await self._pre059_engine()
        try:
            cols = await self._columns(engine, "cantrips")
            assert "lorebook_reads" not in cols
            async with engine.connect() as conn:
                tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "conversation_dynamic_lorebooks" not in tables
        finally:
            await engine.dispose()

    async def test_migrating_059_restores_column_and_table(self):

        from app.services.migrations import run_migrations

        pre = await self._pre059_engine()
        try:
            await run_migrations(pre)
            cols = await self._columns(pre, "cantrips")
            assert "lorebook_reads" in cols
            from sqlalchemy import inspect

            async with pre.connect() as conn:
                tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert "conversation_dynamic_lorebooks" in tables
        finally:
            await pre.dispose()

    async def test_orm_exercises_the_upgraded_schema(self):
        """The hot-path query shape: select Cantrip (lorebook_reads included)
        and insert/query a companion row, against the migrated database."""
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.models.cantrip import Cantrip
        from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook
        from app.models.user import User
        from app.services.migrations import run_migrations

        # The upgrade fixture: pre-059 shape, then the migration set.
        pre = await self._pre059_engine()
        try:
            await run_migrations(pre)
            session_maker = async_sessionmaker(pre, expire_on_commit=False)
            async with session_maker() as db:
                db.add(User(username="upgrader", password_hash="x", gitv_api_key="k"))
                await db.flush()
                uid = (await db.execute(select(User))).scalars().first().id
                db.add(Cantrip(user_id=uid, name="Upgraded",
                               code="", lorebook_reads='["Codex"]'))
                await db.commit()

            async with session_maker() as db:
                c = (await db.execute(
                    select(Cantrip).where(Cantrip.name == "Upgraded")
                )).scalar_one()
                assert c.lorebook_reads == '["Codex"]'
                assert parse_reads(c.lorebook_reads) == ["Codex"]

                book = Cantrip(user_id=uid, name="B", code="")
                db.add(book)
                await db.flush()
                db.add(ConversationDynamicLorebook(
                    user_id=uid, conversation_id="conv-x",
                    name="notes", lorebook_id=book.id))
                await db.commit()
                row = (await db.execute(
                    select(ConversationDynamicLorebook).where(
                        ConversationDynamicLorebook.conversation_id == "conv-x")
                )).scalar_one()
                assert row.name == "notes"
        finally:
            await pre.dispose()
