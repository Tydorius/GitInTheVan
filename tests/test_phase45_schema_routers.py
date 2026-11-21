"""Tests for 0.45.0 schema, migrations, and router additions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.models.cantrip import Cantrip
from app.models.lorebook import Lorebook
from app.models.map import Map, MapStage, MapStageResource
from app.models.skill import Skill
from app.models.user import User
from app.services.deno_runner import CantripResult
from app.services.map_transfer import build_map_from_export, serialize_map_to_export
from app.services.migrations import run_migrations


class TestPhase45UpgradePath:
    """Verify 066_budget_reserved_tokens and 067_cantrip_user_data_prefixes on an upgraded DB."""

    DROPPED_COLUMNS = [
        ("cantrips", "reserved_tokens"),
        ("cantrips", "user_data_prefixes"),
        ("lorebooks", "reserved_tokens"),
        ("skills", "reserved_tokens"),
    ]

    async def _columns(self, conn, table: str) -> set[str]:
        result = await conn.execute(text(f"PRAGMA table_info({table});"))
        return {row[1] for row in result.fetchall()}

    async def test_upgrade_adds_reserved_tokens_and_user_data_prefixes(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table, column in self.DROPPED_COLUMNS:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column};"))
            for table, column in self.DROPPED_COLUMNS:
                assert column not in await self._columns(conn, table)

        await run_migrations(engine)

        async with engine.begin() as conn:
            for table, column in self.DROPPED_COLUMNS:
                assert column in await self._columns(conn, table)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            user = User(username="u-phase45", password_hash="x", gitv_api_key="k")
            db.add(user)
            await db.flush()

            c = Cantrip(user_id=user.id, name="C1")
            lb = Lorebook(user_id=user.id, name="LB1")
            sk = Skill(user_id=user.id, name="SK1")
            db.add_all([c, lb, sk])
            await db.commit()

            assert c.reserved_tokens == 0
            assert c.user_data_prefixes == "[]"
            assert lb.reserved_tokens == 0
            assert sk.reserved_tokens == 0

        await engine.dispose()


class TestCantripSchemaRouters:
    @pytest.mark.asyncio
    async def test_cantrip_reserved_tokens_and_prefixes_crud_and_export(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/cantrips",
            headers=headers,
            json={
                "name": "Scoped Cantrip",
                "code": "console.log('ok');",
                "reserved_tokens": 250,
                "user_data_prefixes": ["  hp_ ", "inventory:", "hp_", "", "  "],
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["reserved_tokens"] == 250
        assert data["user_data_prefixes"] == ["hp_", "inventory:"]
        cid = data["id"]

        # Update
        upd = await client.put(
            f"/api/cantrips/{cid}",
            headers=headers,
            json={"reserved_tokens": 400, "user_data_prefixes": ["quest_", "quest_"]},
        )
        assert upd.status_code == 200
        assert upd.json()["reserved_tokens"] == 400
        assert upd.json()["user_data_prefixes"] == ["quest_"]

        # Export preserves fields
        exp = await client.get(f"/api/cantrips/{cid}/export", headers=headers)
        assert exp.status_code == 200
        exported = json.loads(exp.text)
        assert exported["reserved_tokens"] == 400
        assert exported["user_data_prefixes"] == ["quest_"]

    @pytest.mark.asyncio
    async def test_cantrip_negative_reserved_tokens_rejected(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/cantrips",
            headers=headers,
            json={"name": "Bad", "code": "", "reserved_tokens": -1},
        )
        assert resp.status_code == 422

        created = await client.post(
            "/api/cantrips", headers=headers, json={"name": "Good", "code": ""}
        )
        cid = created.json()["id"]
        upd = await client.put(
            f"/api/cantrips/{cid}", headers=headers, json={"reserved_tokens": -5}
        )
        assert upd.status_code == 422

    @pytest.mark.asyncio
    async def test_cantrip_tester_token_metrics(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        mock_result = CantripResult(
            personality="A brave knight with a glowing sword and a long backstory.",
            scenario="The castle gates stand open at midnight.",
            example_dialogs="",
            injections=[{"text": "Remember the oath of the silver order.", "position": "system"}],
        )
        with patch(
            "app.routers.cantrips._test_with_passenger", new=AsyncMock(return_value=mock_result)
        ):
            resp = await client.post(
                "/api/cantrips/test",
                headers=headers,
                json={
                    "code": "context.character.personality += 'A brave knight';",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tokens_before"] >= 1
        assert body["tokens_after"] > body["tokens_before"]
        assert body["token_delta"] == body["tokens_after"] - body["tokens_before"]

        # Stored cantrip test endpoint
        created = await client.post(
            "/api/cantrips", headers=headers, json={"name": "Stored", "code": ""}
        )
        cid = created.json()["id"]
        with patch(
            "app.routers.cantrips._test_with_passenger", new=AsyncMock(return_value=mock_result)
        ):
            resp2 = await client.post(
                f"/api/cantrips/{cid}/test",
                headers=headers,
                json={"messages": [{"role": "user", "content": "Hello"}]},
            )
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["token_delta"] > 0
        assert body2["token_delta"] == body2["tokens_after"] - body2["tokens_before"]


class TestLorebookAndSkillReservedTokens:
    @pytest.mark.asyncio
    async def test_lorebook_reserved_tokens_crud_export_import(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/lorebooks",
            headers=headers,
            json={"name": "LB Reserved", "reserved_tokens": 300},
        )
        assert resp.status_code == 201
        lb_id = resp.json()["id"]
        assert resp.json()["reserved_tokens"] == 300

        upd = await client.put(
            f"/api/lorebooks/{lb_id}", headers=headers, json={"reserved_tokens": 500}
        )
        assert upd.status_code == 200
        assert upd.json()["reserved_tokens"] == 500

        exp = await client.get(f"/api/lorebooks/{lb_id}/export", headers=headers)
        assert exp.status_code == 200
        assert exp.json()["reserved_tokens"] == 500

        imp = await client.post("/api/lorebooks/import", headers=headers, json=exp.json())
        assert imp.status_code == 201
        assert imp.json()["reserved_tokens"] == 500

        bad = await client.post(
            "/api/lorebooks",
            headers=headers,
            json={"name": "Bad LB", "reserved_tokens": -10},
        )
        assert bad.status_code == 422

    @pytest.mark.asyncio
    async def test_skill_reserved_tokens_crud_and_export(self, admin_client):
        client, token, _ = admin_client
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/skills",
            headers=headers,
            json={"name": "SK Reserved", "content": "Always be concise.", "reserved_tokens": 120},
        )
        assert resp.status_code == 201
        sk_id = resp.json()["id"]
        assert resp.json()["reserved_tokens"] == 120

        upd = await client.put(
            f"/api/skills/{sk_id}", headers=headers, json={"reserved_tokens": 240}
        )
        assert upd.status_code == 200
        assert upd.json()["reserved_tokens"] == 240

        exp = await client.get(f"/api/skills/{sk_id}/export", headers=headers)
        assert exp.status_code == 200
        assert json.loads(exp.text)["reserved_tokens"] == 240

        bad = await client.post(
            "/api/skills",
            headers=headers,
            json={"name": "Bad SK", "reserved_tokens": -1},
        )
        assert bad.status_code == 422


class TestMapTransferPhase45Fields:
    @pytest.mark.asyncio
    async def test_map_export_import_preserves_reserved_tokens_and_prefixes(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            user = User(username="u-map", password_hash="x", gitv_api_key="k")
            db.add(user)
            await db.flush()

            c = Cantrip(
                user_id=user.id,
                name="Map Cantrip",
                code="console.log(1);",
                reserved_tokens=150,
                user_data_prefixes='["stats_", "inv_"]',
            )
            lb = Lorebook(user_id=user.id, name="Map LB", reserved_tokens=200)
            sk = Skill(user_id=user.id, name="Map Skill", content="rule", reserved_tokens=75)
            db.add_all([c, lb, sk])
            await db.flush()

            m = Map(user_id=user.id, name="Phase45 Map")
            db.add(m)
            await db.flush()
            st = MapStage(map_id=m.id, stage_order=1, name="Stage 1")
            db.add(st)
            await db.flush()
            db.add_all([
                MapStageResource(map_stage_id=st.id, resource_type="cantrip", resource_id=c.id),
                MapStageResource(map_stage_id=st.id, resource_type="lorebook", resource_id=lb.id),
                MapStageResource(map_stage_id=st.id, resource_type="skill", resource_id=sk.id),
            ])
            await db.commit()

            from sqlalchemy.orm import selectinload

            loaded = (
                await db.execute(
                    select(Map)
                    .where(Map.id == m.id)
                    .options(selectinload(Map.stages).selectinload(MapStage.resources))
                )
            ).scalar_one()
            exported = await serialize_map_to_export(db, loaded)

            # Import into a second user
            user2 = User(username="u-map2", password_hash="x", gitv_api_key="k2")
            db.add(user2)
            await db.flush()
            res = await build_map_from_export(db, user2.id, exported)
            await db.commit()
            assert res.map_obj.id

            c2 = (
                await db.execute(select(Cantrip).where(Cantrip.user_id == user2.id))
            ).scalar_one()
            lb2 = (
                await db.execute(select(Lorebook).where(Lorebook.user_id == user2.id))
            ).scalar_one()
            sk2 = (
                await db.execute(select(Skill).where(Skill.user_id == user2.id))
            ).scalar_one()
            assert c2.reserved_tokens == 150
            assert json.loads(c2.user_data_prefixes) == ["stats_", "inv_"]
            assert lb2.reserved_tokens == 200
            assert sk2.reserved_tokens == 75

        await engine.dispose()
