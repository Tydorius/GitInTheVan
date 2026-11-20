import json

import pytest

from app.services.lorebook import inject_entries, match_entries


def _make_entry(
    name="entry",
    keys=None,
    content="lore content",
    position="before_last_message",
    insertion_order=10,
    is_constant=False,
    is_selective=False,
    is_disabled=False,
    secondary_keys=None,
):
    return {
        "name": name,
        "keys": json.dumps(keys or []),
        "secondary_keys": json.dumps(secondary_keys or []),
        "content": content,
        "position": position,
        "insertion_order": insertion_order,
        "is_constant": is_constant,
        "is_selective": is_selective,
        "is_disabled": is_disabled,
        "character_limit": 0,
    }


def _make_messages(*roles_and_content):
    return [{"role": r, "content": c} for r, c in roles_and_content]


class TestMatchEntries:
    def test_primary_key_match(self):
        entries = [_make_entry(keys=["dragon"], content="Dragons are fire-breathing.")]
        messages = _make_messages(("user", "Tell me about the dragon"))
        matched = match_entries(messages, entries)
        assert len(matched) == 1
        assert matched[0].content == "Dragons are fire-breathing."

    def test_primary_key_no_match(self):
        entries = [_make_entry(keys=["dragon"], content="Dragons are fire-breathing.")]
        messages = _make_messages(("user", "Tell me about the cat"))
        matched = match_entries(messages, entries)
        assert len(matched) == 0

    def test_case_insensitive_matching(self):
        entries = [_make_entry(keys=["Dragon"], content="lore")]
        messages = _make_messages(("user", "the DRAGON appeared"))
        matched = match_entries(messages, entries)
        assert len(matched) == 1

    def test_multiple_keys_any_match(self):
        entries = [_make_entry(keys=["dragon", "wyrm", "drake"], content="lore")]
        messages = _make_messages(("user", "the drake is here"))
        matched = match_entries(messages, entries)
        assert len(matched) == 1

    def test_constant_entries_always_included(self):
        entries = [_make_entry(keys=["unused"], content="always here", is_constant=True)]
        messages = _make_messages(("user", "nothing relevant"))
        matched = match_entries(messages, entries)
        assert len(matched) == 1
        assert matched[0].content == "always here"

    def test_disabled_entries_skipped(self):
        entries = [_make_entry(keys=["dragon"], content="hidden", is_disabled=True)]
        messages = _make_messages(("user", "the dragon"))
        matched = match_entries(messages, entries)
        assert len(matched) == 0

    def test_selective_mode_requires_both_keys(self):
        entries = [
            _make_entry(
                keys=["dragon"],
                secondary_keys=["castle"],
                content="castle dragon lore",
                is_selective=True,
            )
        ]

        messages_primary_only = _make_messages(("user", "a dragon flies"))
        matched = match_entries(messages_primary_only, entries)
        assert len(matched) == 0

        messages_both = _make_messages(("user", "a dragon in the castle"))
        matched = match_entries(messages_both, entries)
        assert len(matched) == 1

    def test_selective_with_empty_secondary_includes(self):
        entries = [
            _make_entry(
                keys=["dragon"],
                secondary_keys=[],
                content="lore",
                is_selective=True,
            )
        ]
        messages = _make_messages(("user", "a dragon"))
        matched = match_entries(messages, entries)
        assert len(matched) == 1

    def test_multiple_entries_sorted_by_order(self):
        entries = [
            _make_entry(name="second", keys=["a"], content="second", insertion_order=20),
            _make_entry(name="first", keys=["a"], content="first", insertion_order=5),
            _make_entry(name="third", keys=["a"], content="third", insertion_order=30),
        ]
        messages = _make_messages(("user", "match a here"))
        matched = match_entries(messages, entries)
        assert len(matched) == 3
        assert matched[0].name == "first"
        assert matched[1].name == "second"
        assert matched[2].name == "third"

    def test_character_budget(self):
        entries = [
            _make_entry(keys=["a"], content="x" * 50, insertion_order=1),
            _make_entry(keys=["a"], content="y" * 50, insertion_order=2),
            _make_entry(keys=["a"], content="z" * 50, insertion_order=3),
        ]
        messages = _make_messages(("user", "match a"))
        matched = match_entries(messages, entries, total_budget=80)
        assert len(matched) == 2

    def test_no_keys_skips_entry(self):
        entries = [_make_entry(keys=[], content="orphan")]
        messages = _make_messages(("user", "anything"))
        matched = match_entries(messages, entries)
        assert len(matched) == 0

    def test_matches_in_assistant_messages(self):
        entries = [_make_entry(keys=["castle"], content="castle lore")]
        messages = _make_messages(
            ("user", "hello"),
            ("assistant", "you see a grand castle ahead"),
        )
        matched = match_entries(messages, entries)
        assert len(matched) == 1

    def test_matches_in_system_message(self):
        entries = [_make_entry(keys=["forest"], content="forest lore")]
        messages = _make_messages(
            ("system", "You are in a dark forest"),
            ("user", "look around"),
        )
        matched = match_entries(messages, entries)
        assert len(matched) == 1


class TestInjectEntries:
    def test_inject_before_last_message(self):
        messages = _make_messages(
            ("system", "you are helpful"),
            ("user", "hello"),
            ("assistant", "hi"),
            ("user", "tell me about dragons"),
        )
        matched = match_entries(
            messages, [_make_entry(keys=["dragon"], content="dragon lore", position="before_last_message")]
        )
        result = inject_entries(messages, matched)
        assert len(result) == 5
        assert result[-2]["role"] == "system"
        assert result[-2]["content"] == "dragon lore"
        assert result[-1]["role"] == "user"

    def test_inject_system_start_with_existing_system(self):
        messages = _make_messages(
            ("system", "base instructions"),
            ("user", "hello"),
        )
        matched = match_entries(
            messages, [_make_entry(keys=["hello"], content="extra system", position="system_start")]
        )
        result = inject_entries(messages, matched)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert "base instructions" in result[0]["content"]
        assert "extra system" in result[0]["content"]

    def test_inject_system_start_without_existing_system(self):
        messages = _make_messages(("user", "hello"))
        matched = match_entries(
            messages, [_make_entry(keys=["hello"], content="new system", position="system_start")]
        )
        result = inject_entries(messages, matched)
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "new system"

    def test_no_match_returns_original(self):
        messages = _make_messages(("user", "hello"))
        result = inject_entries(messages, [])
        assert result == messages

    def test_multiple_entries_same_position_combined(self):
        messages = _make_messages(
            ("system", "base"),
            ("user", "dragon and castle"),
        )
        matched = match_entries(
            messages,
            [
                _make_entry(keys=["dragon"], content="dragon lore", position="system_start", insertion_order=1),
                _make_entry(keys=["castle"], content="castle lore", position="system_start", insertion_order=2),
            ],
        )
        result = inject_entries(messages, matched)
        assert len(result) == 2
        assert "dragon lore" in result[0]["content"]
        assert "castle lore" in result[0]["content"]

    def test_original_messages_not_mutated(self):
        messages = _make_messages(("user", "hello"))
        matched = match_entries(
            messages, [_make_entry(keys=["hello"], content="lore")]
        )
        original_len = len(messages)
        inject_entries(messages, matched)
        assert len(messages) == original_len


class TestLorebookCRUD:
    @pytest.mark.asyncio
    async def test_create_lorebook(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/lorebooks",
            json={"name": "Test Lorebook", "description": "A test lorebook"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Lorebook"
        assert data["description"] == "A test lorebook"
        assert data["entries"] == []

    @pytest.mark.asyncio
    async def test_list_lorebooks(self, admin_client):
        client, _, _ = admin_client
        await client.post("/api/lorebooks", json={"name": "LB1"})
        await client.post("/api/lorebooks", json={"name": "LB2"})
        resp = await client.get("/api/lorebooks")
        assert resp.status_code == 200
        lbs = resp.json()["lorebooks"]
        assert len(lbs) == 2
        names = [lb["name"] for lb in lbs]
        assert "LB1" in names
        assert "LB2" in names

    @pytest.mark.asyncio
    async def test_get_lorebook(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/lorebooks", json={"name": "Detailed"})
        lb_id = create.json()["id"]
        resp = await client.get(f"/api/lorebooks/{lb_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Detailed"

    @pytest.mark.asyncio
    async def test_update_lorebook(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/lorebooks", json={"name": "Original"})
        lb_id = create.json()["id"]
        resp = await client.put(f"/api/lorebooks/{lb_id}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    @pytest.mark.asyncio
    async def test_delete_lorebook(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/lorebooks", json={"name": "ToDelete"})
        lb_id = create.json()["id"]
        resp = await client.delete(f"/api/lorebooks/{lb_id}")
        assert resp.status_code == 204
        list_resp = await client.get("/api/lorebooks")
        assert len(list_resp.json()["lorebooks"]) == 0

    @pytest.mark.asyncio
    async def test_lorebook_not_found(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/lorebooks/nonexistent")
        assert resp.status_code == 404


class TestEntryCRUD:
    @pytest.mark.asyncio
    async def test_add_entry(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        resp = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "Dragon",
                "keys": ["dragon", "wyrm"],
                "content": "Dragons breathe fire.",
                "position": "before_last_message",
                "insertion_order": 10,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Dragon"
        assert data["keys"] == ["dragon", "wyrm"]
        assert data["content"] == "Dragons breathe fire."

    @pytest.mark.asyncio
    async def test_update_entry(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        entry = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "Old", "keys": ["test"], "content": "old content"},
        )
        entry_id = entry.json()["id"]
        resp = await client.put(
            f"/api/lorebooks/{lb_id}/entries/{entry_id}",
            json={"name": "New", "keys": ["updated"], "content": "new content"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"
        assert resp.json()["keys"] == ["updated"]

    @pytest.mark.asyncio
    async def test_delete_entry(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        entry = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "ToDelete", "keys": ["k"], "content": "c"},
        )
        entry_id = entry.json()["id"]
        resp = await client.delete(f"/api/lorebooks/{lb_id}/entries/{entry_id}")
        assert resp.status_code == 204

        get_resp = await client.get(f"/api/lorebooks/{lb_id}")
        assert len(get_resp.json()["entries"]) == 0

    @pytest.mark.asyncio
    async def test_add_entry_exceeds_lorebook_size_limit(self, admin_client):
        from app.services.admin import update_admin_settings
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        try:
            await update_admin_settings({"max_lorebook_size_kb": 1})
            resp = await client.post(
                f"/api/lorebooks/{lb_id}/entries",
                json={"name": "Big", "content": "x" * 2000},
            )
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_lorebook_size_kb": 500})

    @pytest.mark.asyncio
    async def test_add_entry_size_limit_is_aggregate_across_entries(self, admin_client):
        from app.services.admin import update_admin_settings
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        try:
            await update_admin_settings({"max_lorebook_size_kb": 1})
            first = await client.post(
                f"/api/lorebooks/{lb_id}/entries",
                json={"name": "First", "content": "x" * 600},
            )
            assert first.status_code == 201
            second = await client.post(
                f"/api/lorebooks/{lb_id}/entries",
                json={"name": "Second", "content": "x" * 600},
            )
            assert second.status_code == 413
        finally:
            await update_admin_settings({"max_lorebook_size_kb": 500})

    @pytest.mark.asyncio
    async def test_add_entry_strips_control_chars(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        resp = await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "Clean", "content": "hello\x00world"},
        )
        assert resp.status_code == 201
        assert resp.json()["content"] == "helloworld"

    @pytest.mark.asyncio
    async def test_entries_appear_in_lorebook(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        lb_id = lb.json()["id"]
        await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "E1", "keys": ["a"], "content": "first"},
        )
        await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "E2", "keys": ["b"], "content": "second"},
        )
        resp = await client.get(f"/api/lorebooks/{lb_id}")
        assert len(resp.json()["entries"]) == 2


class TestImportExport:
    @pytest.mark.asyncio
    async def test_export_lorebook(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "ExportTest", "description": "desc"})
        lb_id = lb.json()["id"]
        await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={"name": "E1", "keys": ["key1"], "content": "content1"},
        )
        resp = await client.get(f"/api/lorebooks/{lb_id}/export")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ExportTest"
        assert data["description"] == "desc"
        assert len(data["entries"]) == 1
        assert data["entries"][0]["keys"] == ["key1"]
        assert data["entries"][0]["content"] == "content1"

    @pytest.mark.asyncio
    async def test_import_lorebook(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/lorebooks/import",
            json={
                "name": "Imported",
                "description": "imported desc",
                "entries": [
                    {"name": "E1", "keys": ["k1"], "content": "c1"},
                    {"name": "E2", "keys": ["k2"], "content": "c2", "is_constant": True},
                ],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Imported"
        assert len(data["entries"]) == 2

    @pytest.mark.asyncio
    async def test_round_trip_export_import(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post(
            "/api/lorebooks",
            json={"name": "RoundTrip", "description": "test round trip"},
        )
        lb_id = lb.json()["id"]
        await client.post(
            f"/api/lorebooks/{lb_id}/entries",
            json={
                "name": "Full Entry",
                "keys": ["alpha", "beta"],
                "secondary_keys": ["gamma"],
                "content": "full content",
                "position": "system_start",
                "insertion_order": 5,
                "is_constant": True,
                "is_selective": True,
                "character_limit": 500,
            },
        )

        export = await client.get(f"/api/lorebooks/{lb_id}/export")
        export_data = export.json()

        import_resp = await client.post("/api/lorebooks/import", json=export_data)
        assert import_resp.status_code == 201
        imported = import_resp.json()
        assert imported["name"] == "RoundTrip"
        assert len(imported["entries"]) == 1
        entry = imported["entries"][0]
        assert entry["keys"] == ["alpha", "beta"]
        assert entry["secondary_keys"] == ["gamma"]
        assert entry["position"] == "system_start"
        assert entry["is_constant"] is True
        assert entry["is_selective"] is True
        assert entry["character_limit"] == 500


class TestPublicLorebooks:
    @pytest.mark.asyncio
    async def test_public_lorebooks_listed(self, client, admin_client):
        _, _, _ = admin_client
        admin_client_client, admin_token, _ = admin_client
        await admin_client_client.post(
            "/api/lorebooks",
            json={"name": "Public LB", "is_public": True},
        )
        await admin_client_client.post(
            "/api/lorebooks",
            json={"name": "Private LB", "is_public": False},
        )

        resp = await client.get("/api/lorebooks/public")
        assert resp.status_code == 200
        lbs = resp.json()["lorebooks"]
        names = [lb["name"] for lb in lbs]
        assert "Public LB" in names
        assert "Private LB" not in names

    @pytest.mark.asyncio
    async def test_cannot_access_others_private_lorebook(self, client, admin_client):
        admin_client_http, admin_token, _ = admin_client
        lb = await admin_client_http.post(
            "/api/lorebooks",
            json={"name": "Private", "is_public": False},
        )
        lb_id = lb.json()["id"]

        await client.post(
            "/api/users",
            json={"username": "other", "password": "pass1234"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        login = await client.post("/api/auth/login", json={"username": "other", "password": "pass1234"})
        other_token = login.json()["access_token"]

        resp = await client.get(
            f"/api/lorebooks/{lb_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_can_access_others_public_lorebook(self, client, admin_client):
        admin_client_http, admin_token, _ = admin_client
        lb = await admin_client_http.post(
            "/api/lorebooks",
            json={"name": "Shared", "is_public": True},
        )
        lb_id = lb.json()["id"]

        await client.post(
            "/api/users",
            json={"username": "other2", "password": "pass1234"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        login = await client.post("/api/auth/login", json={"username": "other2", "password": "pass1234"})
        other_token = login.json()["access_token"]

        resp = await client.get(
            f"/api/lorebooks/{lb_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Shared"


# ============================================================================
# Upgrade path: migration 063 on the old schema
# ============================================================================

class TestMigration063UpgradePath:
    @pytest.mark.asyncio
    async def test_column_added_on_upgrade(self):
        """lorebooks is migration-created, so TestMigrationCoverage checks
        the model/migration pair statically -- this proves the ALTER runs on
        a pre-063 database (rule 10): create_all, drop the column,
        vacuity-check, migrate, round-trip through the ORM."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.base import Base
        from app.services.migrations import run_migrations

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text("ALTER TABLE lorebooks DROP COLUMN word_boundary_keys")
                )

            async with engine.connect() as conn:
                cols = await conn.execute(text("PRAGMA table_info(lorebooks)"))
                col_names = {r[1] for r in cols.all()}
            assert "word_boundary_keys" not in col_names

            await run_migrations(engine)

            from sqlalchemy import insert, select

            from app.models.lorebook import Lorebook
            async with engine.begin() as conn:
                await conn.execute(insert(Lorebook).values(
                    user_id="u-upg063", name="LB", word_boundary_keys=True))
                raw = (await conn.execute(
                    select(Lorebook.word_boundary_keys).where(Lorebook.name == "LB")
                )).scalar_one()
            assert raw is True
        finally:
            await engine.dispose()


# ============================================================================
# 0.42.0 (B7.1): opt-in word-boundary keyword matching
# ============================================================================

class TestWordBoundaryMatching:
    """Substring matching is the original behavior and stays the default.
    A book that opts in via word_boundary_keys matches whole words only:
    "Rook" stops firing on "rookie", "Crow" on "crowd" -- which retires the
    punctuation-suffix workaround keys ("Crow,", "Crow.") real users have
    been maintaining by hand."""

    @pytest.mark.asyncio
    async def test_substring_stays_the_default(self, admin_client):
        from app.services.lorebook import match_entries

        msgs = [{"role": "user", "content": "the rookie moves"}]
        entries = [{"keys": '["Rook"]', "content": "X", "insertion_order": 1}]
        assert match_entries(msgs, entries), "default must keep matching inside words"

    @pytest.mark.asyncio
    async def test_word_boundary_does_not_match_inside_words(self, admin_client):
        from app.services.lorebook import match_entries

        msgs = [{"role": "user", "content": "the rookie moves"}]
        entries = [{"keys": '["Rook"]', "content": "X", "insertion_order": 1}]
        assert not match_entries(msgs, entries, word_boundary=True)
        # ...but still matches the actual word, with punctuation around it
        msgs2 = [{"role": "user", "content": "the Rook, it said"}]
        assert match_entries(msgs2, entries, word_boundary=True)

    @pytest.mark.asyncio
    async def test_flag_flows_through_the_pipeline(self, admin_client, httpx_mock):
        """The book's flag reaches matching through the real injection
        path: with the flag on, an inside-word mention does not inject."""
        from app.models.lorebook import Lorebook
        from tests.conftest import TestSessionLocal

        client, _, gitv_key = admin_client

        await client.post("/api/endpoints", json={
            "name": "WB", "base_url": "http://mock-wb:9999", "api_key": "sk-test",
        })
        lb_resp = await client.post("/api/lorebooks", json={
            "name": "WB Book", "tag": "", "is_active": True,
            "word_boundary_keys": True,
        })
        lb_id = lb_resp.json()["id"]
        await client.post(f"/api/lorebooks/{lb_id}/entries", json={
            "name": "Crow", "keys": ["Crow"], "content": "CROW_ENTRY",
            "position": "before_last_message", "insertion_order": 5,
            "is_constant": False, "is_selective": False,
        })

        httpx_mock.add_response(
            url="http://mock-wb:9999/v1/chat/completions",
            json={"id": "1", "object": "chat.completion", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"}]},
            status_code=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "stream": False,
                  "messages": [{"role": "user", "content": "a crowded room"}]},
            headers={"Authorization": f"Bearer {gitv_key}"},
        )
        assert resp.status_code == 200
        forwarded = httpx_mock.get_requests()[-1]
        assert "CROW_ENTRY" not in json.dumps(json.loads(forwarded.content)["messages"])

        # Sanity: the same book with the flag off WOULD have injected.
        async with TestSessionLocal() as db:
            from sqlalchemy import select

            book = (await db.execute(
                select(Lorebook).where(Lorebook.id == lb_id)
            )).scalar_one()
            book.word_boundary_keys = False
            await db.commit()
        httpx_mock.add_response(
            url="http://mock-wb:9999/v1/chat/completions",
            json={"id": "2", "object": "chat.completion", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"}]},
            status_code=200,
        )
        resp2 = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "stream": False,
                  "messages": [{"role": "user", "content": "a crowded room"}]},
            headers={"Authorization": f"Bearer {gitv_key}"},
        )
        assert resp2.status_code == 200
        forwarded2 = httpx_mock.get_requests()[-1]
        assert "CROW_ENTRY" in json.dumps(json.loads(forwarded2.content)["messages"])
