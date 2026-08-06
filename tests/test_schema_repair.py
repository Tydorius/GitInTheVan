"""Tests for the one-time 0.15.x -> 0.18.0 schema drift repair.

These build the schema the way an *upgrade* produces it -- migration DDL as it
shipped, with nothing else touching it -- rather than via `create_all`, which
would hide exactly the class of bug the repair exists to fix.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.services.migrations import MIGRATIONS, _resolve_sql, run_migrations
from app.services.schema_repair import (
    REPAIR_MARKER,
    _add_column_ddl,
    _backfill_sql,
    _diff_schema,
    _widen_column_ddl,
    run_schema_repair,
)


async def _engine_missing_budget_weight(*, with_migration_042: bool = False):
    """A database in the exact state a 0.15.x -> 0.18.0 upgrade produced.

    `skills` is rebuilt from migration 032's DDL (as it shipped in 0.15.x) and
    042 is recorded as already applied, so nothing will re-add the column. This
    is what the reported `no such column: skills.budget_weight` install looked
    like.
    """
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE endpoint_skills;"))
        await conn.execute(text("DROP TABLE skills;"))
        sql = _resolve_sql(dict(MIGRATIONS)["032_create_skills_tables"], "sqlite")
        for statement in (s.strip() for s in sql.split(";")):
            if statement:
                await conn.execute(text(statement))

    await run_migrations(engine)

    if not with_migration_042:
        # Undo 042 and leave it marked applied: the state of a database upgraded
        # before 042 existed, which the migration runner will never revisit.
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE skills;"))
            sql = _resolve_sql(dict(MIGRATIONS)["032_create_skills_tables"], "sqlite")
            for statement in (s.strip() for s in sql.split(";")):
                if statement and "endpoint_skills" not in statement:
                    await conn.execute(text(statement))

    return engine


async def _seed_user(engine) -> str:
    from app.models.user import User

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        user = User(username="u", password_hash="x", gitv_api_key="k")
        db.add(user)
        await db.commit()
        return user.id


async def _columns(engine, table: str) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.execute(text(f"PRAGMA table_info({table});"))
        return {row[1] for row in result.fetchall()}


@pytest.fixture(autouse=True)
def isolate_repair_log(monkeypatch, tmp_path):
    """Keep the repair log out of the real data/ directory.

    _REPAIR_LOG_PATH is derived from the module location, so without this every
    test run appends to the developer's live data/schema-repair.log.
    """
    import app.services.schema_repair as repair_mod

    monkeypatch.setattr(repair_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(repair_mod, "_REPAIR_LOG_PATH", tmp_path / "schema-repair.log")


@pytest.fixture
def stub_backup(monkeypatch):
    """Stand in for create_backup, which needs a file-backed database."""
    calls = []

    class _Run:
        def __init__(self, status="success", error_message=""):
            self.status = status
            self.error_message = error_message
            self.file_path = "data/backups/stub.db"

    async def _create_backup(triggered_by: str):
        calls.append(triggered_by)
        return _Run()

    import app.services.backup as backup_mod

    monkeypatch.setattr(backup_mod, "create_backup", _create_backup)
    return calls


class TestDriftDetection:
    async def test_fixture_reproduces_the_broken_state(self):
        """Vacuity guard: if this fails the rest of the file tests nothing."""
        engine = await _engine_missing_budget_weight()
        try:
            assert "budget_weight" not in await _columns(engine, "skills")
        finally:
            await engine.dispose()

    async def test_diff_finds_missing_budget_weight(self):
        engine = await _engine_missing_budget_weight()
        try:
            drifts = await _diff_schema(engine)
            missing = {(d.table, d.column) for d in drifts if d.kind == "missing_column"}
            assert ("skills", "budget_weight") in missing
        finally:
            await engine.dispose()

    async def test_healthy_database_reports_no_missing_columns(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await run_migrations(engine)

            drifts = await _diff_schema(engine)
            assert [d for d in drifts if d.kind == "missing_column"] == []
        finally:
            await engine.dispose()


class TestRepair:
    async def test_repair_adds_missing_column(self, stub_backup):
        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            summary = await run_schema_repair(engine)

            assert summary["ran"] is True
            assert summary["marker_set"] is True
            assert summary["remaining"] == []
            assert any("skills.budget_weight" in a for a in summary["applied"])
            assert "budget_weight" in await _columns(engine, "skills")
            assert stub_backup == ["repair"]
        finally:
            await engine.dispose()

    async def test_repaired_column_backfills_default(self, stub_backup):
        """A row inserted before the repair must not come back as NULL."""
        engine = await _engine_missing_budget_weight()
        try:
            user_id = await _seed_user(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO skills (id, user_id, name, description, content, type, "
                        "created_at, updated_at) VALUES ('s1', :uid, 'n', '', 'c', 'skill', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);"
                    ),
                    {"uid": user_id},
                )

            await run_schema_repair(engine)

            async with engine.begin() as conn:
                result = await conn.execute(text("SELECT budget_weight FROM skills;"))
                assert result.scalar() == 1.0
        finally:
            await engine.dispose()

    async def test_real_query_works_after_repair(self, stub_backup):
        """The exact query from the reported OperationalError."""
        from app.models.endpoint import Endpoint
        from app.models.skill import EndpointSkill, Skill
        from app.services.skills import load_skills_for_endpoint

        engine = await _engine_missing_budget_weight()
        try:
            user_id = await _seed_user(engine)
            await run_schema_repair(engine)

            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                endpoint = Endpoint(user_id=user_id, name="e", base_url="http://localhost")
                db.add(endpoint)
                await db.flush()
                skill = Skill(
                    user_id=user_id, name="s", description="", content="BODY", type="skill"
                )
                db.add(skill)
                await db.flush()
                db.add(EndpointSkill(endpoint_id=endpoint.id, skill_id=skill.id))
                await db.commit()

                skills, samples = await load_skills_for_endpoint(endpoint.id, user_id, db)

            assert skills == ["BODY"]
            assert samples == []
        finally:
            await engine.dispose()

    async def test_second_run_is_a_noop(self, stub_backup):
        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            await run_schema_repair(engine)
            second = await run_schema_repair(engine)

            assert second["ran"] is False
            assert second["applied"] == []
            assert stub_backup == ["repair"], "repair backed up twice"
        finally:
            await engine.dispose()

    async def test_marker_recorded_in_migrations_table(self, stub_backup):
        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            await run_schema_repair(engine)

            async with engine.begin() as conn:
                result = await conn.execute(
                    text("SELECT 1 FROM _migrations WHERE name = :n;"), {"n": REPAIR_MARKER}
                )
                assert result.first() is not None
        finally:
            await engine.dispose()

    async def test_marker_does_not_disturb_migration_runner(self, stub_backup):
        """run_migrations must ignore the repair marker, not choke on it."""
        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            await run_schema_repair(engine)
            await run_migrations(engine)

            async with engine.begin() as conn:
                result = await conn.execute(text("SELECT COUNT(*) FROM _migrations;"))
                assert result.scalar() == len(MIGRATIONS) + 1
        finally:
            await engine.dispose()

    async def test_failed_backup_aborts_without_setting_marker(self, monkeypatch):
        """Never mutate a schema that could not be snapshotted."""
        class _Run:
            status = "failed"
            error_message = "disk full"
            file_path = ""

        async def _create_backup(triggered_by: str):
            return _Run()

        import app.services.backup as backup_mod

        monkeypatch.setattr(backup_mod, "create_backup", _create_backup)

        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            summary = await run_schema_repair(engine)

            assert summary["marker_set"] is False
            assert "disk full" in summary["error"]
            assert "budget_weight" not in await _columns(engine, "skills")
        finally:
            await engine.dispose()

    async def test_marker_withheld_when_repair_does_not_stick(self, stub_backup, monkeypatch):
        """If drift survives the repair, the marker must NOT be set.

        The marker suppresses every future attempt, so recording it over
        unrepaired drift would permanently strand the database. Simulated by
        making the ALTER a no-op, which is what a silently-refused DDL looks
        like on a locked or read-only table.
        """
        import app.services.schema_repair as repair_mod

        async def _noop_execute(engine, statement):
            return ""

        monkeypatch.setattr(repair_mod, "_execute", _noop_execute)

        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)
            summary = await run_schema_repair(engine)

            assert summary["ran"] is True
            assert summary["remaining"] == ["skills.budget_weight"]
            assert summary["marker_set"] is False, (
                "marker set despite unrepaired drift; the repair would never retry"
            )
            assert "will retry" in summary["error"]

            async with engine.begin() as conn:
                result = await conn.execute(
                    text("SELECT 1 FROM _migrations WHERE name = :n;"), {"n": REPAIR_MARKER}
                )
                assert result.first() is None
        finally:
            await engine.dispose()

    async def test_repair_retries_on_next_start_after_a_failure(self, stub_backup, monkeypatch):
        """A withheld marker means the next boot tries again and can succeed."""
        import app.services.schema_repair as repair_mod

        real_execute = repair_mod._execute

        async def _noop_execute(engine, statement):
            return ""

        engine = await _engine_missing_budget_weight()
        try:
            await _seed_user(engine)

            monkeypatch.setattr(repair_mod, "_execute", _noop_execute)
            first = await run_schema_repair(engine)
            assert first["marker_set"] is False

            monkeypatch.setattr(repair_mod, "_execute", real_execute)
            second = await run_schema_repair(engine)
            assert second["marker_set"] is True
            assert second["remaining"] == []
            assert "budget_weight" in await _columns(engine, "skills")
        finally:
            await engine.dispose()

    async def test_sqlite_only_width_drift_does_not_trigger_a_backup(self, stub_backup):
        """map_stage_resources.resource_type is VARCHAR(16) on every upgraded
        database and String(32) in the model. SQLite does not enforce VARCHAR
        length, so backing up and rewriting the schema over it would be pure
        cost for zero benefit.
        """
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE map_stage_resources;"))
                await conn.execute(
                    text(
                        "CREATE TABLE map_stage_resources ("
                        "id VARCHAR(36) PRIMARY KEY, map_stage_id VARCHAR(36) NOT NULL, "
                        "resource_type VARCHAR(16) NOT NULL, resource_id VARCHAR(36) NOT NULL, "
                        "position VARCHAR(32) DEFAULT 'pre_driver' NOT NULL, "
                        "sticky BOOLEAN DEFAULT 0 NOT NULL, "
                        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
                    )
                )
            await run_migrations(engine)
            await _seed_user(engine)

            summary = await run_schema_repair(engine)

            assert summary["ran"] is False
            assert stub_backup == [], "SQLite width drift must not trigger a backup"
            assert summary["marker_set"] is True
            assert any("resource_type" in r for r in summary["reported"])
        finally:
            await engine.dispose()

    async def test_fresh_database_marks_without_work(self, stub_backup):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await run_migrations(engine)

            summary = await run_schema_repair(engine)
            assert summary["marker_set"] is True
            assert summary["applied"] == []
            assert stub_backup == [], "fresh database should not be backed up"
        finally:
            await engine.dispose()


class TestDdlGeneration:
    def test_add_column_ddl_targets_the_right_column(self):
        from sqlalchemy.dialects import sqlite

        from app.services.schema_repair import Drift

        ddl = _add_column_ddl(Drift("skills", "budget_weight", "missing_column"), sqlite.dialect())
        assert ddl is not None
        assert ddl.startswith("ALTER TABLE skills ADD COLUMN budget_weight")

    def test_backfill_uses_the_model_default(self):
        from app.services.schema_repair import Drift

        sql = _backfill_sql(Drift("skills", "budget_weight", "missing_column"))
        assert sql == (
            "UPDATE skills SET budget_weight = 1.0 WHERE budget_weight IS NULL;"
        )

    def test_widening_is_a_noop_on_sqlite_but_real_elsewhere(self):
        from app.services.schema_repair import Drift

        drift = Drift("map_stage_resources", "resource_type", "narrow_column")
        assert _widen_column_ddl(drift, "sqlite") is None
        assert "TYPE VARCHAR(32)" in _widen_column_ddl(drift, "postgresql")
        assert "MODIFY COLUMN" in _widen_column_ddl(drift, "mysql")
