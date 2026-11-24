from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.services.log_manager import TimezoneFormatter, apply_log_timezone
from app.services.migrations import run_migrations
from app.services.timezones import (
    get_zoneinfo,
    resolve_effective_timezone,
    validate_timezone,
)

# ---------------------------------------------------------------------------
# Unit tests: app/services/timezones.py
# ---------------------------------------------------------------------------


class TestTimezoneValidation:
    def test_valid_iana_names(self):
        assert validate_timezone("America/New_York") == "America/New_York"
        assert validate_timezone("UTC") == "UTC"
        assert validate_timezone("Asia/Tokyo") == "Asia/Tokyo"
        assert validate_timezone("  Europe/London  ") == "Europe/London"

    def test_blank_and_none_return_empty_string(self):
        assert validate_timezone(None) == ""
        assert validate_timezone("") == ""
        assert validate_timezone("   ") == ""

    def test_invalid_timezone_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            validate_timezone("Mars/Olympus")
        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            validate_timezone("Not/A_Real_Zone")
        with pytest.raises(ValueError, match="Unknown IANA timezone"):
            validate_timezone("../etc/passwd")

    def test_over_max_length_raises_value_error(self):
        with pytest.raises(ValueError, match="at most 64"):
            validate_timezone("A" * 65)

    def test_get_zoneinfo(self):
        assert get_zoneinfo("UTC") == ZoneInfo("UTC")
        assert get_zoneinfo("America/New_York") == ZoneInfo("America/New_York")
        assert get_zoneinfo("") is None
        assert get_zoneinfo(None) is None
        assert get_zoneinfo("Mars/Olympus") is None


class TestResolveEffectiveTimezone:
    def test_user_blank_inherits_admin_global(self):
        assert resolve_effective_timezone("", "America/New_York") == "America/New_York"
        assert resolve_effective_timezone(None, "Europe/Berlin") == "Europe/Berlin"
        assert resolve_effective_timezone("   ", "Asia/Tokyo") == "Asia/Tokyo"

    def test_user_explicit_overrides_admin_global(self):
        assert resolve_effective_timezone("Asia/Tokyo", "America/New_York") == "Asia/Tokyo"
        assert resolve_effective_timezone("UTC", "") == "UTC"

    def test_both_blank_returns_empty(self):
        assert resolve_effective_timezone("", "") == ""
        assert resolve_effective_timezone(None, None) == ""
        assert resolve_effective_timezone("  ", "  ") == ""


# ---------------------------------------------------------------------------
# Server log formatter tests
# ---------------------------------------------------------------------------


class TestLogTimezoneFormatter:
    def _make_record(self, dt_utc: datetime) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello timezone",
            args=(),
            exc_info=None,
        )
        record.created = dt_utc.timestamp()
        return record

    def test_apply_log_timezone_formats_records_live(self):
        root = logging.getLogger()
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        try:
            # 2026-01-15 17:30:00 UTC -> 2026-01-15 12:30:00 EST in America/New_York
            fixed_utc = datetime(2026, 1, 15, 17, 30, 0, tzinfo=UTC)
            record = self._make_record(fixed_utc)

            apply_log_timezone("America/New_York")
            assert isinstance(handler.formatter, TimezoneFormatter)
            formatted_ny = handler.format(record)
            assert "2026-01-15 12:30:00 EST" in formatted_ny
            assert "hello timezone" in formatted_ny

            apply_log_timezone("UTC")
            assert isinstance(handler.formatter, TimezoneFormatter)
            formatted_utc = handler.format(record)
            assert "2026-01-15 17:30:00 UTC" in formatted_utc

            apply_log_timezone("")
            assert isinstance(handler.formatter, TimezoneFormatter)
            assert handler.formatter.tz_name == ""
            assert handler.formatter._zi is None
            formatted_local = handler.format(record)
            assert "hello timezone" in formatted_local
        finally:
            root.removeHandler(handler)
            apply_log_timezone("")


# ---------------------------------------------------------------------------
# Router integration tests: /api/admin/settings & /api/settings
# ---------------------------------------------------------------------------


class TestAdminTimezoneSettingsAPI:
    async def test_admin_get_put_log_timezone(self, admin_client):
        client, _token, _api_key = admin_client

        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["log_timezone"] == ""

        put_resp = await client.put("/api/admin/settings", json={"log_timezone": "America/New_York"})
        assert put_resp.status_code == 200
        assert put_resp.json()["log_timezone"] == "America/New_York"

        get_after = await client.get("/api/admin/settings")
        assert get_after.status_code == 200
        assert get_after.json()["log_timezone"] == "America/New_York"

        # Clear back to server local time
        clear_resp = await client.put("/api/admin/settings", json={"log_timezone": ""})
        assert clear_resp.status_code == 200
        assert clear_resp.json()["log_timezone"] == ""

    async def test_admin_put_invalid_log_timezone_returns_422(self, admin_client):
        client, _token, _api_key = admin_client

        resp = await client.put("/api/admin/settings", json={"log_timezone": "Mars/Olympus"})
        assert resp.status_code == 422


class TestUserTimezoneSettingsAPI:
    async def test_user_timezone_inheritance_and_override(self, admin_client):
        client, _token, _api_key = admin_client

        # Initially both user and global are blank
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == ""
        assert data["global_timezone"] == ""
        assert data["effective_timezone"] == ""

        # Admin sets global timezone -> user with blank timezone inherits it
        admin_put = await client.put("/api/admin/settings", json={"log_timezone": "America/New_York"})
        assert admin_put.status_code == 200

        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == ""
        assert data["global_timezone"] == "America/New_York"
        assert data["effective_timezone"] == "America/New_York"

        # User sets explicit override -> effective becomes user's while global stays admin's
        user_put = await client.put("/api/settings", json={"timezone": "Asia/Tokyo"})
        assert user_put.status_code == 200
        data = user_put.json()
        assert data["timezone"] == "Asia/Tokyo"
        assert data["global_timezone"] == "America/New_York"
        assert data["effective_timezone"] == "Asia/Tokyo"

        # User clears override -> inherits admin's global timezone again
        user_clear = await client.put("/api/settings", json={"timezone": ""})
        assert user_clear.status_code == 200
        data = user_clear.json()
        assert data["timezone"] == ""
        assert data["global_timezone"] == "America/New_York"
        assert data["effective_timezone"] == "America/New_York"

        # Reset global log_timezone to keep test state clean
        await client.put("/api/admin/settings", json={"log_timezone": ""})

    async def test_user_put_invalid_timezone_returns_422(self, admin_client):
        client, _token, _api_key = admin_client

        resp = await client.put("/api/settings", json={"timezone": "Mars/Olympus"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Migration 073 upgrade path test
# ---------------------------------------------------------------------------


class TestMigration073UpgradePath:
    async def test_migration_073_adds_timezone_columns(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("ALTER TABLE admin_settings DROP COLUMN log_timezone;"))
                await conn.execute(text("ALTER TABLE user_settings DROP COLUMN timezone;"))

            # Vacuity check: both columns must be absent before migrating
            async with engine.connect() as conn:
                admin_cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(admin_settings)")}
                user_cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(user_settings)")}
                assert "log_timezone" not in admin_cols
                assert "timezone" not in user_cols

            await run_migrations(engine)

            async with engine.connect() as conn:
                admin_cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(admin_settings)")}
                user_cols = {r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(user_settings)")}
                assert "log_timezone" in admin_cols
                assert "timezone" in user_cols

                tz_val = (await conn.execute(text("SELECT log_timezone FROM admin_settings WHERE id = 1;"))).scalar()
                assert tz_val == ""
        finally:
            await engine.dispose()
