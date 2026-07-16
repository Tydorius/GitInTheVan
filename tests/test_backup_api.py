import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings as app_settings
from app.services import backup as backup_service


@pytest.fixture
def scratch_sqlite(tmp_path, monkeypatch):
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
class TestBackupApi:
    async def test_run_backup_endpoint(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        _, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            resp = await client.post("/api/admin/backup/run")
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    async def test_list_backups_endpoint(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        _, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            await client.post("/api/admin/backup/run")
            resp = await client.get("/api/admin/backup/list")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_download_backup_endpoint(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        _, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            create_resp = await client.post("/api/admin/backup/run")
            backup_id = create_resp.json()["id"]
            resp = await client.get(f"/api/admin/backup/download/{backup_id}")
        assert resp.status_code == 200

    async def test_delete_backup_endpoint(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        _, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            create_resp = await client.post("/api/admin/backup/run")
            backup_id = create_resp.json()["id"]
            resp = await client.delete(f"/api/admin/backup/{backup_id}")
        assert resp.status_code == 204

    async def test_delete_missing_backup_404(self, admin_client):
        client, _, _ = admin_client
        resp = await client.delete("/api/admin/backup/does-not-exist")
        assert resp.status_code == 404

    async def test_restore_request_and_confirm(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        db_file, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            create_resp = await client.post("/api/admin/backup/run")
            backup_id = create_resp.json()["id"]

            request_resp = await client.post(f"/api/admin/backup/restore/{backup_id}/request")
            assert request_resp.status_code == 200
            token = request_resp.json()["token"]

            confirm_resp = await client.post(
                f"/api/admin/backup/restore/{backup_id}/confirm", json={"token": token}
            )
        assert confirm_resp.status_code == 200
        assert confirm_resp.json()["success"] is True

    async def test_restore_confirm_wrong_token_rejected(self, admin_client, scratch_sqlite):
        client, _, _ = admin_client
        _, backup_dir = scratch_sqlite
        with patch.object(backup_service, "_backup_dir", new=AsyncMock(return_value=backup_dir)):
            create_resp = await client.post("/api/admin/backup/run")
            backup_id = create_resp.json()["id"]
            resp = await client.post(
                f"/api/admin/backup/restore/{backup_id}/confirm", json={"token": "wrong"}
            )
        assert resp.status_code == 400

    async def test_backup_endpoints_require_admin(self, client):
        resp = await client.post("/api/admin/backup/run")
        assert resp.status_code in (401, 403)
        resp = await client.get("/api/admin/backup/list")
        assert resp.status_code in (401, 403)

    async def test_admin_settings_expose_backup_schedule_defaults(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["backup_schedule_enabled"] is False
        assert data["backup_schedule_time"] == "03:00"
        assert data["backup_retention_count"] == 7

    async def test_admin_can_update_backup_schedule(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put(
            "/api/admin/settings",
            json={
                "backup_schedule_enabled": True,
                "backup_schedule_days": "mon,wed,fri",
                "backup_schedule_time": "04:30",
                "backup_retention_count": 14,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["backup_schedule_enabled"] is True
        assert data["backup_schedule_days"] == "mon,wed,fri"
        assert data["backup_schedule_time"] == "04:30"
        assert data["backup_retention_count"] == 14
