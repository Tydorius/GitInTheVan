"""Storage browser (feature-request item 11): CRUD over the three cantrip
persistent stores — chat_data, cantrip_data, user_data.

Covers per-scope CRUD round-trips, cross-user isolation (a second user's
rows are 404s and never listed — the filtering standard), the rule-18 write
guards (JSON validation, size cap with restore-in-finally), key-clash 409s,
copy row / copy conversation / clear conversation, export filename safety
(the Content-Disposition 500 family) and the import upsert with per-row
refusal reporting. No Deno needed: rows are created directly.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.models.cantrip import Cantrip
from app.models.cantrip_data import CantripData
from app.models.chat_data import ChatData
from app.models.user import User
from app.models.user_data import UserData
from app.services.admin import update_admin_settings
from app.services.auth import create_access_token
from tests.conftest import TestSessionLocal


async def _admin_id() -> str:
    async with TestSessionLocal() as s:
        row = (await s.execute(
            User.__table__.select().where(User.__table__.c.username == "admin")
        )).fetchone()
    return dict(row._mapping)["id"]


async def _seed_row(model, user_id: str, *, key: str, value, extra: dict | None = None):
    async with TestSessionLocal() as s:
        row = model(
            user_id=user_id,
            key=key,
            value_json=json.dumps(value),
            **(extra or {}),
        )
        s.add(row)
        await s.commit()
        return row.id


async def _fetch_rows(model, user_id: str) -> list:
    async with TestSessionLocal() as s:
        rows = (await s.execute(
            model.__table__.select().where(model.__table__.c.user_id == user_id)
        )).fetchall()
    return [dict(r._mapping) for r in rows]


@pytest.fixture
async def second_user(client):
    """A second, non-admin user with a real token (cross-user filters need
    a row that must be excluded)."""
    uid = f"u2-{uuid.uuid4().hex[:8]}"
    username = f"user_{uid[:6]}"
    async with TestSessionLocal() as s:
        s.add(User(
            id=uid, username=username, password_hash="x",
            gitv_api_key=f"key-{uid}", is_admin=False,
        ))
        await s.commit()
    token = create_access_token(uid, username, False)
    return uid, token


class TestChatDataCrud:
    @pytest.mark.asyncio
    async def test_round_trip(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        row_id = await _seed_row(
            ChatData, uid, key="day", value=3, extra={"conversation_id": "conv-1"}
        )

        resp = await client.get("/api/storage/chats")
        assert resp.status_code == 200
        convs = resp.json()["conversations"]
        assert [c["conversation_id"] for c in convs] == ["conv-1"]
        assert convs[0]["key_count"] == 1

        resp = await client.get("/api/storage/chat-data", params={"conversation_id": "conv-1"})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["key"] == "day"
        assert "3" in rows[0]["preview"]

        resp = await client.get(f"/api/storage/chat-data/{row_id}")
        assert resp.status_code == 200
        assert resp.json()["value"] == 3

        resp = await client.put(f"/api/storage/chat-data/{row_id}", json={"value": "42"})
        assert resp.status_code == 200
        resp = await client.get(f"/api/storage/chat-data/{row_id}")
        assert resp.json()["value"] == 42

        resp = await client.delete(f"/api/storage/chat-data/{row_id}")
        assert resp.status_code == 204
        resp = await client.get(f"/api/storage/chat-data/{row_id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_put_requires_valid_json(self, admin_client):
        client, _, _ = admin_client
        row_id = await _seed_row(
            ChatData, await _admin_id(), key="k", value=1,
            extra={"conversation_id": "conv-x"},
        )
        resp = await client.put(f"/api/storage/chat-data/{row_id}", json={"value": "{not json"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_size_cap_refused_and_restored(self, admin_client):
        client, _, _ = admin_client
        row_id = await _seed_row(
            ChatData, await _admin_id(), key="k", value=1,
            extra={"conversation_id": "conv-x"},
        )
        await update_admin_settings({"max_rule_size_kb": 1})
        try:
            resp = await client.put(
                f"/api/storage/chat-data/{row_id}",
                json={"value": json.dumps(["x" * 2048])},
            )
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_rule_size_kb": 25})

    @pytest.mark.asyncio
    async def test_key_clash_is_409(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        a = await _seed_row(
            ChatData, uid, key="a", value=1, extra={"conversation_id": "conv-x"}
        )
        await _seed_row(
            ChatData, uid, key="b", value=2, extra={"conversation_id": "conv-x"}
        )
        resp = await client.put(f"/api/storage/chat-data/{a}", json={"key": "b", "value": "9"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_search_and_pagination(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        for i in range(5):
            await _seed_row(
                ChatData, uid, key=f"key-{i}", value=i,
                extra={"conversation_id": "conv-p"},
            )
        resp = await client.get(
            "/api/storage/chat-data",
            params={"conversation_id": "conv-p", "search": "key-4"},
        )
        assert resp.status_code == 200
        assert [r["key"] for r in resp.json()["rows"]] == ["key-4"]

        resp = await client.get(
            "/api/storage/chat-data",
            params={"conversation_id": "conv-p", "limit": 2, "offset": 0},
        )
        assert resp.json()["total"] == 5
        assert [r["key"] for r in resp.json()["rows"]] == ["key-0", "key-1"]
        resp = await client.get(
            "/api/storage/chat-data",
            params={"conversation_id": "conv-p", "limit": 2, "offset": 4},
        )
        assert [r["key"] for r in resp.json()["rows"]] == ["key-4"]


class TestCrossUserIsolation:
    @pytest.mark.asyncio
    async def test_other_users_rows_invisible_and_404(self, admin_client, second_user):
        client, _, _ = admin_client
        other_id, other_token = second_user
        row_id = await _seed_row(
            ChatData, other_id, key="secret", value=1,
            extra={"conversation_id": "other-conv"},
        )
        user_row_id = await _seed_row(UserData, other_id, key="ou", value=1)

        # Not listed for the admin.
        resp = await client.get("/api/storage/chats")
        assert all(c["conversation_id"] != "other-conv" for c in resp.json()["conversations"])
        resp = await client.get(
            "/api/storage/chat-data", params={"conversation_id": "other-conv"}
        )
        assert resp.json()["total"] == 0
        resp = await client.get("/api/storage/user-data")
        assert all(r["key"] != "ou" for r in resp.json()["rows"])

        # Direct access by id is a 404, not a 403.
        resp = await client.get(f"/api/storage/chat-data/{row_id}")
        assert resp.status_code == 404
        resp = await client.get(f"/api/storage/user-data/{user_row_id}")
        assert resp.status_code == 404
        resp = await client.put(f"/api/storage/chat-data/{row_id}", json={"value": "0"})
        assert resp.status_code == 404
        resp = await client.delete(f"/api/storage/chat-data/{row_id}")
        assert resp.status_code == 404

        # The other user still sees their own row (genuinely authenticated).
        resp = await client.get(
            "/api/storage/chats", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert [c["conversation_id"] for c in resp.json()["conversations"]] == ["other-conv"]

    @pytest.mark.asyncio
    async def test_unauthenticated_401(self, client):
        resp = await client.get("/api/storage/user-data")
        assert resp.status_code == 401


class TestConversationActions:
    @pytest.mark.asyncio
    async def test_copy_row_and_conflict(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        row_id = await _seed_row(
            ChatData, uid, key="move", value="v", extra={"conversation_id": "c1"}
        )
        resp = await client.post(
            f"/api/storage/chat-data/{row_id}/copy",
            json={"target_conversation_id": "c2"},
        )
        assert resp.status_code == 200
        rows = await _fetch_rows(ChatData, uid)
        assert {(r["conversation_id"], r["key"]) for r in rows} == {("c1", "move"), ("c2", "move")}

        resp = await client.post(
            f"/api/storage/chat-data/{row_id}/copy",
            json={"target_conversation_id": "c2"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_clear_conversation(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        for i in range(3):
            await _seed_row(
                ChatData, uid, key=f"k{i}", value=i,
                extra={"conversation_id": "clear-me"},
            )
        await _seed_row(
            ChatData, uid, key="keep", value=1, extra={"conversation_id": "keep-me"}
        )
        resp = await client.delete("/api/storage/chats/clear-me")
        assert resp.status_code == 204
        rows = await _fetch_rows(ChatData, uid)
        assert {(r["conversation_id"], r["key"]) for r in rows} == {("keep-me", "keep")}

    @pytest.mark.asyncio
    async def test_clear_only_own_conversation(self, admin_client, second_user):
        client, _, _ = admin_client
        other_id, _ = second_user
        await _seed_row(
            ChatData, other_id, key="k", value=1, extra={"conversation_id": "shared-name"}
        )
        resp = await client.delete("/api/storage/chats/shared-name")
        assert resp.status_code == 204
        # The other user's rows with the same conversation id survive.
        rows = await _fetch_rows(ChatData, other_id)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_copy_conversation_skips_existing_keys(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        await _seed_row(ChatData, uid, key="a", value=1, extra={"conversation_id": "src"})
        await _seed_row(ChatData, uid, key="b", value=2, extra={"conversation_id": "src"})
        await _seed_row(ChatData, uid, key="a", value=99, extra={"conversation_id": "dst"})
        resp = await client.post(
            "/api/storage/chats/src/copy", json={"target_conversation_id": "dst"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["copied"] == 1
        assert body["skipped"] == ["a"]
        rows = {r["key"]: r for r in await _fetch_rows(ChatData, uid)}
        assert json.loads(rows["a"]["value_json"]) == 99  # existing value untouched


class TestUserAndCantripData:
    @pytest.mark.asyncio
    async def test_user_data_crud(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        row_id = await _seed_row(UserData, uid, key="global", value={"n": 1})
        resp = await client.get("/api/storage/user-data")
        assert resp.status_code == 200
        assert [r["key"] for r in resp.json()["rows"]] == ["global"]
        resp = await client.put(f"/api/storage/user-data/{row_id}", json={"value": '{"n": 2}'})
        assert resp.status_code == 200
        resp = await client.get(f"/api/storage/user-data/{row_id}")
        assert resp.json()["value"] == {"n": 2}
        resp = await client.delete(f"/api/storage/user-data/{row_id}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_cantrip_data_crud(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        cantrip_id = "c-" + uuid.uuid4().hex[:8]
        async with TestSessionLocal() as s:
            s.add(Cantrip(id=cantrip_id, user_id=uid, name="StoreTest", code=""))
            await s.commit()
        row_id = await _seed_row(
            CantripData, uid, key="state", value=[1, 2],
            extra={"cantrip_id": cantrip_id},
        )
        resp = await client.get(
            "/api/storage/cantrip-data", params={"cantrip_id": cantrip_id}
        )
        assert resp.status_code == 200
        assert [r["key"] for r in resp.json()["rows"]] == ["state"]
        resp = await client.put(f"/api/storage/cantrip-data/{row_id}", json={"value": "[3]"})
        assert resp.status_code == 200
        resp = await client.get(f"/api/storage/cantrip-data/{row_id}")
        assert resp.json()["value"] == [3]
        # A key clash inside the same cantrip is a 409.
        other = await _seed_row(
            CantripData, uid, key="other", value=0, extra={"cantrip_id": cantrip_id}
        )
        resp = await client.put(
            f"/api/storage/cantrip-data/{other}", json={"key": "state", "value": "0"}
        )
        assert resp.status_code == 409


class TestExportImport:
    @pytest.mark.asyncio
    async def test_export_filename_safe_and_import_round_trip(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        nasty_conv = 'conv "quotes" \U0001f389'
        await _seed_row(
            ChatData, uid, key="a", value=1, extra={"conversation_id": nasty_conv}
        )
        resp = await client.get(
            "/api/storage/export",
            params={"scope": "chat", "conversation_id": nasty_conv},
        )
        assert resp.status_code == 200
        # A quote or non-ASCII in the conversation id must not break the
        # header (the known Content-Disposition 500 family).
        disposition = resp.headers["content-disposition"]
        assert '"' == disposition.split("filename=")[1][0]
        assert disposition.rstrip().endswith('.json"')
        payload = resp.json()
        assert payload["rows"] == [{"key": "a", "value": 1}]

        resp = await client.post(
            "/api/storage/import",
            json={"payload": payload, "target_conversation_id": "imported-conv"},
        )
        assert resp.status_code == 200
        assert resp.json()["imported"] == 1
        rows = await _fetch_rows(ChatData, uid)
        targets = {r["conversation_id"] for r in rows}
        assert "imported-conv" in targets

    @pytest.mark.asyncio
    async def test_import_upserts_and_reports_refusals(self, admin_client):
        client, _, _ = admin_client
        uid = await _admin_id()
        await _seed_row(UserData, uid, key="existing", value="old")
        payload = {
            "format": "gitv-storage-export",
            "version": 1,
            "scope": "user",
            "rows": [
                {"key": "existing", "value": "new"},
                {"key": "x" * 300, "value": 1},  # invalid key -> skipped
                {"key": "fresh", "value": 2},
            ],
        }
        resp = await client.post("/api/storage/import", json={"payload": payload})
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"] == 2
        assert len(body["skipped"]) == 1
        rows = {r["key"]: r for r in await _fetch_rows(UserData, uid)}
        assert json.loads(rows["existing"]["value_json"]) == "new"
        assert json.loads(rows["fresh"]["value_json"]) == 2

    @pytest.mark.asyncio
    async def test_import_rejects_foreign_payload(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/storage/import", json={"payload": {"foo": "bar"}})
        assert resp.status_code == 422
