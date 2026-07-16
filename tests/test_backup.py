import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings as app_settings
from app.services import backup as backup_service


@pytest.fixture
def scratch_sqlite(tmp_path, monkeypatch):
    """Point settings.database_url at a throwaway SQLite file for this test only.

    Never exercise backup.py's real sqlite3.connect() path against the
    project's real data/gitinthevan.db - see Planning/security-control-document.md
    for why this matters (a prior session accidentally deleted the real dev DB
    during manual verification).
    """
    db_file = tmp_path / "scratch.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO marker (note) VALUES ('hello')")
    conn.commit()
    conn.close()

    original_url = app_settings.database_url
    monkeypatch.setattr(app_settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    backup_dir = tmp_path / "backups"
    yield db_file, backup_dir
    monkeypatch.setattr(app_settings, "database_url", original_url)


@pytest.mark.asyncio
class TestBackupService:
    async def test_create_backup_sqlite_success(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")

        assert run.status == "success"
        assert run.size_bytes > 0
        assert run.triggered_by == "manual"

        conn = sqlite3.connect(run.file_path)
        rows = conn.execute("SELECT note FROM marker").fetchall()
        conn.close()
        assert rows == [("hello",)]

    async def test_create_backup_failure_records_error(self, scratch_sqlite, monkeypatch):
        db_file, backup_dir = scratch_sqlite
        monkeypatch.setattr(app_settings, "database_url", "sqlite+aiosqlite:///./does/not/exist.db")

        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")

        assert run.status == "failed"
        assert run.error_message

    async def test_list_backups(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            await backup_service.create_backup(triggered_by="manual")
            runs = await backup_service.list_backups()
        assert len(runs) >= 1

    async def test_delete_backup(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")
            deleted = await backup_service.delete_backup(run.id)
        assert deleted is True
        from pathlib import Path
        assert not Path(run.file_path).exists()

    async def test_delete_nonexistent_backup_returns_false(self, scratch_sqlite):
        deleted = await backup_service.delete_backup("does-not-exist")
        assert deleted is False

    async def test_restore_requires_valid_token(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")

        with pytest.raises(ValueError, match="Invalid or expired"):
            await backup_service.confirm_restore(run.id, "wrong-token")

    async def test_restore_round_trip(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")

        # Corrupt the "live" db to prove restore actually overwrites it.
        conn = sqlite3.connect(str(db_file))
        conn.execute("DELETE FROM marker")
        conn.commit()
        conn.close()

        token = backup_service.request_restore(run.id)
        await backup_service.confirm_restore(run.id, token)

        conn = sqlite3.connect(str(db_file))
        rows = conn.execute("SELECT note FROM marker").fetchall()
        conn.close()
        assert rows == [("hello",)]

    async def test_restore_token_single_use(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            run = await backup_service.create_backup(triggered_by="manual")

        token = backup_service.request_restore(run.id)
        await backup_service.confirm_restore(run.id, token)

        with pytest.raises(ValueError, match="Invalid or expired"):
            await backup_service.confirm_restore(run.id, token)

    async def test_prune_backups_respects_retention(self, scratch_sqlite):
        db_file, backup_dir = scratch_sqlite
        from app.services.admin import update_admin_settings
        try:
            await update_admin_settings({"backup_retention_count": 2})

            with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
                for _ in range(4):
                    await backup_service.create_backup(triggered_by="manual")

            runs = await backup_service.list_backups()
            successful = [r for r in runs if r.status == "success"]
            assert len(successful) == 2
        finally:
            await update_admin_settings({"backup_retention_count": 7})
