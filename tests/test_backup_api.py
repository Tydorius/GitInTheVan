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


@pytest.mark.asyncio
class TestAssistantAdminSettings:
    """Phase 26a: admin caps/flags gating the Assistant Pane."""

    async def test_admin_settings_expose_assistant_defaults(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["assistant_enabled"] is True
        assert data["assistant_admin_reads_enabled"] is False
        assert data["assistant_packs_enabled"] is False
        assert data["max_assistant_tool_calls_per_turn"] == 16
        assert data["max_assistant_conversations"] == 20
        assert data["max_assistant_tool_result_kb"] == 32

    async def test_admin_can_update_assistant_settings(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put(
            "/api/admin/settings",
            json={
                "assistant_enabled": False,
                "assistant_admin_reads_enabled": True,
                "assistant_packs_enabled": True,
                "max_assistant_tool_calls_per_turn": 8,
                "max_assistant_conversations": 5,
                "max_assistant_tool_result_kb": 64,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["assistant_enabled"] is False
        assert data["assistant_admin_reads_enabled"] is True
        assert data["assistant_packs_enabled"] is True
        assert data["max_assistant_tool_calls_per_turn"] == 8
        assert data["max_assistant_conversations"] == 5
        assert data["max_assistant_tool_result_kb"] == 64

        # GET reflects the same values PUT returned -- both go through the
        # shared _settings_response projection.
        get_resp = await client.get("/api/admin/settings")
        assert get_resp.json()["max_assistant_conversations"] == 5

    async def test_non_admin_cannot_update_assistant_settings(self, admin_client):
        """A 401 is not a 403 -- use a logged-in non-admin, and confirm the
        request is genuinely authenticated first (see the testing standards)."""
        client, _, _ = admin_client
        created = await client.post(
            "/api/users", json={"username": "assistant-member", "password": "memberpass123"}
        )
        assert created.status_code == 201

        client.headers.pop("Authorization", None)
        login = await client.post(
            "/api/auth/login",
            json={"username": "assistant-member", "password": "memberpass123"},
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["is_admin"] is False

        resp = await client.put(
            "/api/admin/settings", json={"assistant_enabled": False}
        )
        assert resp.status_code == 403
