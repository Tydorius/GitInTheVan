"""Tests for dialect-aware database migrations.

These tests verify:
- Dialect-specific SQL resolution (rowid vs ROW_NUMBER, INSERT OR IGNORE vs ON CONFLICT)
- DDL generation for the _migrations tracking table per dialect
- Error tolerance for re-running migrations after create_all
- Full migration run on a fresh SQLite database
- Migration idempotency (running twice is a no-op)
- Advisory lock no-op for SQLite
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import *  # noqa: F401,F403 -- ensure all models register on Base.metadata
from app.models import Base
from app.services.migrations import (
    MIGRATIONS,
    _acquire_lock,
    _is_tolerable_error,
    _migrations_table_ddl,
    _release_lock,
    _resolve_sql,
    run_migrations,
)

# ---------------------------------------------------------------------------
# SQL resolution
# ---------------------------------------------------------------------------

class TestResolveSql:
    def test_plain_string_returned_for_all_dialects(self):
        sql_def = "ALTER TABLE foo ADD COLUMN bar INTEGER;"
        assert _resolve_sql(sql_def, "sqlite") == sql_def
        assert _resolve_sql(sql_def, "postgresql") == sql_def
        assert _resolve_sql(sql_def, "mysql") == sql_def

    def test_dict_picks_dialect_specific(self):
        sql_def = {
            "sqlite": "INSERT OR IGNORE",
            "postgresql": "INSERT ON CONFLICT",
            "mysql": "INSERT IGNORE",
        }
        assert "OR IGNORE" in _resolve_sql(sql_def, "sqlite")
        assert "ON CONFLICT" in _resolve_sql(sql_def, "postgresql")
        assert "IGNORE" in _resolve_sql(sql_def, "mysql")

    def test_dict_falls_back_to_default(self):
        sql_def = {"default": "fallback", "sqlite": "sqlite-sql"}
        assert _resolve_sql(sql_def, "postgresql") == "fallback"

    def test_dict_missing_dialect_and_default_returns_empty(self):
        sql_def = {"sqlite": "only-sqlite"}
        assert _resolve_sql(sql_def, "postgresql") == ""


class TestMigration006DialectVariants:
    """Migration 006 uses rowid (SQLite-specific). Verify dialect variants."""

    def _get_migration(self, name: str):
        for n, sql in MIGRATIONS:
            if n == name:
                return sql
        raise KeyError(name)

    def test_sqlite_uses_rowid(self):
        sql = _resolve_sql(self._get_migration("006_seed_default_tags"), "sqlite")
        assert "rowid" in sql.lower()

    def test_postgresql_uses_row_number(self):
        sql = _resolve_sql(self._get_migration("006_seed_default_tags"), "postgresql")
        assert "row_number()" in sql.lower()
        assert "rowid" not in sql.lower()

    def test_mysql_uses_row_number_join(self):
        sql = _resolve_sql(self._get_migration("006_seed_default_tags"), "mysql")
        assert "row_number()" in sql.lower()
        assert "inner join" in sql.lower()
        assert "rowid" not in sql.lower()


class TestMigration023DialectVariants:
    """Migration 023 uses INSERT OR IGNORE (SQLite-specific). Verify dialect variants."""

    def _get_migration(self, name: str):
        for n, sql in MIGRATIONS:
            if n == name:
                return sql
        raise KeyError(name)

    def test_sqlite_uses_insert_or_ignore(self):
        sql = _resolve_sql(self._get_migration("023_add_admin_settings_and_caps"), "sqlite")
        assert "INSERT OR IGNORE" in sql

    def test_postgresql_uses_on_conflict(self):
        sql = _resolve_sql(self._get_migration("023_add_admin_settings_and_caps"), "postgresql")
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    def test_mysql_uses_insert_ignore(self):
        sql = _resolve_sql(self._get_migration("023_add_admin_settings_and_caps"), "mysql")
        assert "INSERT IGNORE" in sql


# ---------------------------------------------------------------------------
# DDL generation
# ---------------------------------------------------------------------------

class TestMigrationsTableDDL:
    def test_sqlite_uses_autoincrement(self):
        ddl = _migrations_table_ddl("sqlite")
        assert "AUTOINCREMENT" in ddl
        assert "CREATE TABLE IF NOT EXISTS _migrations" in ddl

    def test_postgresql_uses_serial(self):
        ddl = _migrations_table_ddl("postgresql")
        assert "SERIAL" in ddl
        assert "AUTOINCREMENT" not in ddl

    def test_mysql_uses_auto_increment(self):
        ddl = _migrations_table_ddl("mysql")
        assert "AUTO_INCREMENT" in ddl
        assert "AUTOINCREMENT" not in ddl

    def test_all_have_name_unique_and_applied_at(self):
        for dialect in ("sqlite", "postgresql", "mysql"):
            ddl = _migrations_table_ddl(dialect)
            assert "name VARCHAR(128) UNIQUE NOT NULL" in ddl
            assert "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in ddl


# ---------------------------------------------------------------------------
# Error tolerance
# ---------------------------------------------------------------------------

class TestTolerableErrors:
    def test_sqlite_duplicate_column(self):
        assert _is_tolerable_error(Exception('duplicate column name: "foo"'))

    def test_sqlite_unique_constraint(self):
        assert _is_tolerable_error(Exception("UNIQUE constraint failed: _migrations.name"))

    def test_postgresql_already_exists(self):
        assert _is_tolerable_error(
            Exception('column "foo" of relation "bar" already exists')
        )

    def test_postgresql_unique_violation(self):
        assert _is_tolerable_error(
            Exception('duplicate key value violates unique constraint "_migrations_name_key"')
        )

    def test_mysql_duplicate_entry(self):
        assert _is_tolerable_error(Exception("Duplicate entry '1' for key 'PRIMARY'"))

    def test_mysql_duplicate_column(self):
        assert _is_tolerable_error(Exception("Duplicate column name 'foo'"))

    def test_real_error_not_tolerated(self):
        assert not _is_tolerable_error(Exception("syntax error at or near 'SELEC'"))
        assert not _is_tolerable_error(Exception("permission denied for table users"))


# ---------------------------------------------------------------------------
# Full migration run (SQLite integration)
# ---------------------------------------------------------------------------

@pytest.fixture
async def fresh_sqlite_engine():
    """A fresh in-memory SQLite engine with all ORM tables created."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


class TestFullMigrationRun:
    async def test_all_migrations_apply_on_fresh_sqlite(self, fresh_sqlite_engine):
        """Running migrations after create_all should complete without errors."""
        await run_migrations(fresh_sqlite_engine)

        async with fresh_sqlite_engine.begin() as conn:
            result = await conn.execute(text("SELECT name FROM _migrations ORDER BY name;"))
            applied = {row[0] for row in result.fetchall()}

        expected = {name for name, _ in MIGRATIONS}
        assert applied == expected, f"Missing migrations: {expected - applied}"

    async def test_running_migrations_twice_is_idempotent(self, fresh_sqlite_engine):
        await run_migrations(fresh_sqlite_engine)
        await run_migrations(fresh_sqlite_engine)

        async with fresh_sqlite_engine.begin() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM _migrations;"))
            count = result.scalar()

        assert count == len(MIGRATIONS)

    async def test_admin_settings_seeded_after_migration(self, fresh_sqlite_engine):
        await run_migrations(fresh_sqlite_engine)

        async with fresh_sqlite_engine.begin() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM admin_settings WHERE id = 1;"))
            assert result.scalar() == 1


class TestAdvisoryLock:
    async def test_sqlite_lock_is_noop(self, fresh_sqlite_engine):
        """SQLite should not acquire or release any advisory lock."""
        await _acquire_lock(fresh_sqlite_engine, "sqlite")
        await _release_lock(fresh_sqlite_engine, "sqlite")

    async def test_migration_count(self):
        """Sanity check: verify we have the expected number of migrations."""
        assert len(MIGRATIONS) == 40

    async def test_all_migrations_have_unique_names(self):
        names = [name for name, _ in MIGRATIONS]
        assert len(names) == len(set(names)), "Duplicate migration names found"

    async def test_no_plain_string_uses_sqlite_only_syntax(self):
        """Migrations that are plain strings (not dicts) should not contain SQLite-only keywords."""
        sqlite_keywords = ["rowid", "INSERT OR IGNORE", "AUTOINCREMENT"]
        for name, sql_def in MIGRATIONS:
            if isinstance(sql_def, str):
                for kw in sqlite_keywords:
                    assert kw.lower() not in sql_def.lower(), (
                        f"Migration {name} is a plain string but contains '{kw}' "
                        f"(should be a dialect-specific dict)"
                    )
