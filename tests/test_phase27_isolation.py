"""Phase 27 item 7: sandboxes and replays never write the user-scoped stores.

chat_data and memories are isolated by the forked/synthetic chat id the proxy
resolves, but user_data and cantrip_data are keyed by user alone — before this
phase a cantrip writing either during a sandbox or replay run mutated the same
live rows a real request writes.
"""
import json

import pytest
from sqlalchemy import select

from app.services.cantrip import process_cantrips
from app.services.deno_runner import DENO_PATH
from tests.conftest import TestSessionLocal

pytestmark = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")

# Writes every store the context exposes.
WRITING_CANTRIP = """
context.user_data.set('probe', 'replay-wrote');
context.cantrip_data.set('probe', 'replay-wrote');
context.chat_data.set('probe', 'replay-wrote');
"""


async def _seed_store(model, user_id: str, key: str, value: str, cantrip_id: str = "") -> None:
    async with TestSessionLocal() as db:
        db.add(model(
            user_id=user_id,
            **({"cantrip_id": cantrip_id} if cantrip_id else {}),
            key=key,
            value_json=json.dumps(value),
        ))
        await db.commit()


async def _store_value(model, user_id: str, key: str, cantrip_id: str = ""):
    async with TestSessionLocal() as db:
        query = select(model).where(model.user_id == user_id, model.key == key)
        if cantrip_id:
            query = query.where(model.cantrip_id == cantrip_id)
        row = (await db.execute(query)).scalar_one_or_none()
        return json.loads(row.value_json) if row and row.value_json else None


async def _current_user_id(client) -> str:
    me = await client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["id"]


class TestStoreIsolation:
    @pytest.fixture
    async def writing_cantrip_id(self, admin_client):
        client, _, _ = admin_client
        created = await client.post("/api/cantrips", json={
            "name": "StoreWriter", "code": WRITING_CANTRIP, "is_active": True,
        })
        assert created.status_code == 201, created.text
        return created.json()["id"]

    @pytest.mark.asyncio
    async def test_a_live_run_writes_every_store(self, admin_client, writing_cantrip_id):
        """Vacuity check: without the isolation flags the same cantrip writes
        the user-scoped stores, so the skips below prove the flags, not a dead
        script."""
        from app.models.user_data import UserData

        client, _, _ = admin_client
        user_id = await _current_user_id(client)

        body = {"messages": [{"role": "user", "content": "hello"}]}
        await process_cantrips(
            body, user_id, {"x-conversation-id": "conv-live"}, None,
        )

        assert await _store_value(UserData, user_id, "probe") == "replay-wrote"

    @pytest.mark.asyncio
    async def test_a_replay_writes_no_user_or_cantrip_store(
        self, admin_client, writing_cantrip_id,
    ):
        from app.models.cantrip_data import CantripData
        from app.models.chat_data import ChatData
        from app.models.user_data import UserData

        client, _, _ = admin_client
        user_id = await _current_user_id(client)

        # Sentinel values the script would overwrite if its writes landed.
        await _seed_store(UserData, user_id, "probe", "live-value")
        await _seed_store(CantripData, user_id, "probe", "live-value", writing_cantrip_id)

        replay_body = {
            "messages": [{"role": "user", "content": "replayed"}],
            "_gitv_replay": True,
            "_gitv_replay_of": "run-1",
        }
        await process_cantrips(
            replay_body, user_id, {"x-conversation-id": "conv-replay"}, None,
            internal_chat_id="replay:run-1",
        )

        assert await _store_value(UserData, user_id, "probe") == "live-value"
        assert await _store_value(
            CantripData, user_id, "probe", writing_cantrip_id
        ) == "live-value"
        # The live conversation's chat_data was never written either.
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(ChatData).where(
                    ChatData.user_id == user_id,
                    ChatData.conversation_id == "conv-replay",
                )
            )).scalar_one_or_none()
            assert row is None

    @pytest.mark.asyncio
    async def test_a_sandbox_keeps_the_fork_but_not_the_user_stores(
        self, admin_client, writing_cantrip_id,
    ):
        from app.models.cantrip_data import CantripData
        from app.models.chat_data import ChatData
        from app.models.user_data import UserData

        client, _, _ = admin_client
        user_id = await _current_user_id(client)

        sandbox_body = {
            "messages": [{"role": "user", "content": "sandboxed"}],
            "_gitv_sandbox": True,
            "_gitv_sandbox_chat_id": "sbx-fork-1",
        }
        await process_cantrips(
            sandbox_body, user_id, {"x-conversation-id": "sbx-fork-1"}, None,
            internal_chat_id="sbx-fork-1",
        )

        # user-scoped stores: untouched by the sandbox run.
        assert await _store_value(UserData, user_id, "probe") is None
        assert await _store_value(CantripData, user_id, "probe", writing_cantrip_id) is None
        # chat_data lands in the fork — a sandbox's writes accumulate there.
        async with TestSessionLocal() as db:
            row = (await db.execute(
                select(ChatData).where(
                    ChatData.user_id == user_id,
                    ChatData.conversation_id == "sbx-fork-1",
                )
            )).scalar_one_or_none()
            assert row is not None
