"""Phase 47 tests for Deno warm pool AdminSettings, migration 071, and API wiring."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import Base
from app.models.admin_settings import AdminSettings
from app.services import deno_runner
from app.services.admin import apply_admin_overrides_at_startup
from app.services.migrations import run_migrations


class _FakeDenoPool:
    """Minimal stand-in matching the cross-worker deno_pool contract."""

    def __init__(self, *, idle_count: int = 1, warm_hits: int = 5, cold_spawns: int = 2) -> None:
        self.min_idle = 1
        self.max_idle = 4
        self.idle_ttl_seconds = 60
        self.idle_count = idle_count
        self.total_warm_hits = warm_hits
        self.total_cold_spawns = cold_spawns
        self.configure_calls: list[dict[str, int]] = []

    def configure(self, *, min_idle: int, max_idle: int, idle_ttl_seconds: int) -> None:
        self.min_idle = min_idle
        self.max_idle = max_idle
        self.idle_ttl_seconds = idle_ttl_seconds
        self.configure_calls.append(
            {
                "min_idle": min_idle,
                "max_idle": max_idle,
                "idle_ttl_seconds": idle_ttl_seconds,
            }
        )

    def status(self) -> dict[str, Any]:
        return {
            "idle_count": self.idle_count,
            "min_idle": self.min_idle,
            "max_idle": self.max_idle,
            "idle_ttl_seconds": self.idle_ttl_seconds,
            "total_warm_hits": self.total_warm_hits,
            "total_cold_spawns": self.total_cold_spawns,
        }


class TestAdminDenoPoolSettingsAPI:
    async def test_get_settings_returns_defaults_and_pool_status(self, admin_client, monkeypatch):
        client, _, _ = admin_client
        fake_pool = _FakeDenoPool(idle_count=2, warm_hits=11, cold_spawns=3)
        monkeypatch.setattr(deno_runner, "deno_pool", fake_pool, raising=False)

        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deno_pool_min_idle"] == 1
        assert data["deno_pool_max_idle"] == 4
        assert data["deno_pool_idle_ttl_seconds"] == 60
        assert data["deno_pool_status"] == {
            "idle_count": 2,
            "min_idle": 1,
            "max_idle": 4,
            "idle_ttl_seconds": 60,
            "total_warm_hits": 11,
            "total_cold_spawns": 3,
        }

    async def test_get_settings_handles_missing_pool_gracefully(self, admin_client, monkeypatch):
        client, _, _ = admin_client
        monkeypatch.setattr(deno_runner, "deno_pool", None, raising=False)

        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deno_pool_min_idle"] == 1
        assert data["deno_pool_max_idle"] == 4
        assert data["deno_pool_idle_ttl_seconds"] == 60
        assert data["deno_pool_status"] is None

    async def test_put_settings_persists_and_configures_pool_live(self, admin_client, monkeypatch):
        client, _, _ = admin_client
        fake_pool = _FakeDenoPool()
        monkeypatch.setattr(deno_runner, "deno_pool", fake_pool, raising=False)

        put_resp = await client.put(
            "/api/admin/settings",
            json={
                "deno_pool_min_idle": 2,
                "deno_pool_max_idle": 8,
                "deno_pool_idle_ttl_seconds": 120,
            },
        )
        assert put_resp.status_code == 200
        data = put_resp.json()
        assert data["deno_pool_min_idle"] == 2
        assert data["deno_pool_max_idle"] == 8
        assert data["deno_pool_idle_ttl_seconds"] == 120
        assert fake_pool.configure_calls == [
            {"min_idle": 2, "max_idle": 8, "idle_ttl_seconds": 120}
        ]
        assert data["deno_pool_status"]["min_idle"] == 2
        assert data["deno_pool_status"]["max_idle"] == 8
        assert data["deno_pool_status"]["idle_ttl_seconds"] == 120

        # Confirm persisted across a subsequent GET.
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.status_code == 200
        persisted = get_resp.json()
        assert persisted["deno_pool_min_idle"] == 2
        assert persisted["deno_pool_max_idle"] == 8
        assert persisted["deno_pool_idle_ttl_seconds"] == 120

    async def test_put_settings_clamps_max_idle_to_at_least_min_idle(self, admin_client, monkeypatch):
        client, _, _ = admin_client
        fake_pool = _FakeDenoPool()
        monkeypatch.setattr(deno_runner, "deno_pool", fake_pool, raising=False)

        # Both sent with max < min -> max clamps up to min.
        resp1 = await client.put(
            "/api/admin/settings",
            json={"deno_pool_min_idle": 6, "deno_pool_max_idle": 2},
        )
        assert resp1.status_code == 200
        assert resp1.json()["deno_pool_min_idle"] == 6
        assert resp1.json()["deno_pool_max_idle"] == 6

        # Only min_idle raised above current max_idle (6) -> max clamps up to 10.
        resp2 = await client.put(
            "/api/admin/settings",
            json={"deno_pool_min_idle": 10},
        )
        assert resp2.status_code == 200
        assert resp2.json()["deno_pool_min_idle"] == 10
        assert resp2.json()["deno_pool_max_idle"] == 10

        # Setting both to 0 disables pre-warming cleanly.
        resp3 = await client.put(
            "/api/admin/settings",
            json={"deno_pool_min_idle": 0, "deno_pool_max_idle": 0},
        )
        assert resp3.status_code == 200
        assert resp3.json()["deno_pool_min_idle"] == 0
        assert resp3.json()["deno_pool_max_idle"] == 0
        assert fake_pool.configure_calls[-1] == {
            "min_idle": 0,
            "max_idle": 0,
            "idle_ttl_seconds": 60,
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {"deno_pool_min_idle": -1},
            {"deno_pool_min_idle": 17},
            {"deno_pool_max_idle": -1},
            {"deno_pool_max_idle": 33},
            {"deno_pool_idle_ttl_seconds": 9},
            {"deno_pool_idle_ttl_seconds": 0},
            {"deno_pool_idle_ttl_seconds": -5},
            {"deno_pool_idle_ttl_seconds": 601},
        ],
    )
    async def test_put_settings_rejects_out_of_range_values_with_422(
        self, admin_client, payload, monkeypatch
    ):
        client, _, _ = admin_client
        fake_pool = _FakeDenoPool()
        monkeypatch.setattr(deno_runner, "deno_pool", fake_pool, raising=False)

        resp = await client.put("/api/admin/settings", json=payload)
        assert resp.status_code == 422
        assert fake_pool.configure_calls == []

    async def test_startup_override_configures_pool_from_db(self, admin_client, monkeypatch):
        client, _, _ = admin_client
        monkeypatch.setattr(deno_runner, "deno_pool", None, raising=False)

        resp = await client.put(
            "/api/admin/settings",
            json={
                "deno_pool_min_idle": 3,
                "deno_pool_max_idle": 7,
                "deno_pool_idle_ttl_seconds": 90,
            },
        )
        assert resp.status_code == 200

        # Simulate process restart: a fresh pool instance receives the persisted config.
        restarted_pool = _FakeDenoPool()
        monkeypatch.setattr(deno_runner, "deno_pool", restarted_pool, raising=False)
        await apply_admin_overrides_at_startup()

        assert restarted_pool.configure_calls == [
            {"min_idle": 3, "max_idle": 7, "idle_ttl_seconds": 90}
        ]


class TestMigration071UpgradePath:
    """Verify 071_admin_deno_pool_settings adds the 3 columns on an upgraded DB."""

    ADDED_COLUMNS = (
        "deno_pool_min_idle",
        "deno_pool_max_idle",
        "deno_pool_idle_ttl_seconds",
    )

    async def test_migration_071_adds_columns_with_defaults_on_upgraded_db(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # Seed an existing admin_settings row, then drop the 3 Phase 47 columns
                # to reconstruct a pre-0.47.0 database.
                await conn.execute(text("INSERT INTO admin_settings (id) VALUES (1);"))
                for col in self.ADDED_COLUMNS:
                    await conn.execute(text(f"ALTER TABLE admin_settings DROP COLUMN {col};"))

            # Vacuity check: confirm columns are absent before migrating.
            async with engine.connect() as conn:
                cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(admin_settings)")}
                for col in self.ADDED_COLUMNS:
                    assert col not in cols

            await run_migrations(engine)

            # Columns must now exist and the pre-existing row must have 1 / 4 / 60 defaults.
            async with engine.connect() as conn:
                cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(admin_settings)")}
                for col in self.ADDED_COLUMNS:
                    assert col in cols

            async with AsyncSession(engine) as session:
                row = (
                    await session.execute(select(AdminSettings).where(AdminSettings.id == 1))
                ).scalar_one()
                assert row.deno_pool_min_idle == 1
                assert row.deno_pool_max_idle == 4
                assert row.deno_pool_idle_ttl_seconds == 60
        finally:
            await engine.dispose()
