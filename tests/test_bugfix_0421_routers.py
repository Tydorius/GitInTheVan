from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.admin as _admin_module
import app.services.content_guard as _content_guard_module
from app.database import get_db
from app.main import app
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.cantrip import Cantrip
from app.models.cantrip_data import CantripData
from app.models.chat_data import ChatData
from app.models.endpoint import Endpoint
from app.models.lorebook import Lorebook
from app.models.map import Map
from app.models.resource_snapshot import ResourceSnapshot
from app.models.skill import Skill
from app.models.user import User
from app.models.user_data import UserData
from app.services.auth import create_access_token, hash_password
from app.services.backup import _mysql_backup, confirm_restore, request_restore
from app.services.git_sync import GitCloneError, fetch_repo_info
from app.services.map_transfer import _update_resource, build_map_from_export
from app.services.safety_scanner import scan_cantrip
from app.services.sanitization import scan_urls
from app.services.schema_repair import Drift, _backfill_sql
from tests.conftest import TestSessionLocal


@pytest.fixture
async def db_session():
    async with TestSessionLocal() as session:
        yield session


async def _create_user(db: AsyncSession, username: str, is_admin: bool = False) -> tuple[User, dict[str, str]]:
    user = User(
        username=username,
        password_hash=hash_password("StrongPass123!"),
        gitv_api_key=f"gitv_test_{username}",
        is_admin=is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    token = create_access_token(user.id, user.username, user.is_admin)
    return user, {"Authorization": f"Bearer {token}"}


class TestSqliteDeadlockAndScanner:
    @pytest.mark.asyncio
    async def test_update_cantrip_file_backed_sqlite_no_deadlock(self, tmp_path: Path):
        db_path = tmp_path / "deadlock_test.db"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            echo=False,
            connect_args={"timeout": 1},
        )
        session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def _override_get_db():
            async with session_maker() as session:
                yield session

        orig_get_db = app.dependency_overrides.get(get_db)
        orig_admin_session = _admin_module.async_session
        orig_guard_session = _content_guard_module.async_session
        app.dependency_overrides[get_db] = _override_get_db
        _admin_module.async_session = session_maker
        _content_guard_module.async_session = session_maker

        try:
            async with session_maker() as db:
                user, headers = await _create_user(db, "sqlite_lock_user")
                cantrip = Cantrip(user_id=user.id, name="LockTest", code="console.log(1);")
                db.add(cantrip)
                await db.commit()
                await db.refresh(cantrip)
                cantrip_id = cantrip.id

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put(
                    f"/api/cantrips/{cantrip_id}",
                    headers=headers,
                    json={
                        "code": "eval('1+1');",
                        "response_tags": ["mood"],
                    },
                )
            assert resp.status_code == 200, resp.text
            assert resp.json()["response_tags"] == ["mood"]

            async with session_maker() as db:
                snaps = (await db.execute(
                    select(ResourceSnapshot).where(ResourceSnapshot.resource_id == cantrip_id)
                )).scalars().all()
                assert len(snaps) == 1
                logs = (await db.execute(
                    select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.action == "content_safety_scan")
                )).scalars().all()
                assert len(logs) == 1
        finally:
            if orig_get_db is not None:
                app.dependency_overrides[get_db] = orig_get_db
            else:
                app.dependency_overrides.pop(get_db, None)
            _admin_module.async_session = orig_admin_session
            _content_guard_module.async_session = orig_guard_session
            await engine.dispose()

    def test_scan_cantrip_respects_custom_max_content_size(self):
        code = "x" * 60_000
        default_scan = scan_cantrip(code)
        assert any(f.pattern == "size" for f in default_scan.findings)
        custom_scan = scan_cantrip(code, max_content_size=100_000)
        assert not any(f.pattern == "size" for f in custom_scan.findings)

    @pytest.mark.asyncio
    async def test_update_lorebook_entry_rejected_422_writes_no_snapshot(self, client: AsyncClient, db_session: AsyncSession):
        user, headers = await _create_user(db_session, "lb_snap_user")
        lb = Lorebook(user_id=user.id, name="LB")
        db_session.add(lb)
        await db_session.commit()
        await db_session.refresh(lb)

        add_resp = await client.post(
            f"/api/lorebooks/{lb.id}/entries",
            headers=headers,
            json={"name": "E1", "keys": ["k"], "content": "safe content"},
        )
        assert add_resp.status_code == 201
        entry_id = add_resp.json()["id"]

        bad_resp = await client.put(
            f"/api/lorebooks/{lb.id}/entries/{entry_id}",
            headers=headers,
            json={"name": "E1", "keys": ["k"], "content": "<script>alert(1)</script>"},
        )
        assert bad_resp.status_code == 422

        snaps = (await db_session.execute(
            select(ResourceSnapshot).where(ResourceSnapshot.resource_id == lb.id)
        )).scalars().all()
        assert len(snaps) == 0


class TestEmptyTagClearing:
    @pytest.mark.asyncio
    async def test_cantrip_and_lorebook_can_clear_tag_to_empty(self, client: AsyncClient, db_session: AsyncSession):
        user, headers = await _create_user(db_session, "tag_user")
        c1 = Cantrip(user_id=user.id, name="C1", tag="")
        c2 = Cantrip(user_id=user.id, name="C2", tag="mytag")
        lb1 = Lorebook(user_id=user.id, name="L1", tag="")
        lb2 = Lorebook(user_id=user.id, name="L2", tag="lbtag")
        db_session.add_all([c1, c2, lb1, lb2])
        await db_session.commit()
        await db_session.refresh(c2)
        await db_session.refresh(lb2)

        r1 = await client.put(f"/api/cantrips/{c2.id}", headers=headers, json={"tag": ""})
        assert r1.status_code == 200
        assert r1.json()["tag"] == ""

        r2 = await client.put(f"/api/lorebooks/{lb2.id}", headers=headers, json={"tag": ""})
        assert r2.status_code == 200
        assert r2.json()["tag"] == ""


class TestBackupFixes:
    @pytest.mark.asyncio
    async def test_confirm_restore_removes_wal_and_shm(self, tmp_path: Path, db_session: AsyncSession):
        from app.models.backup_run import BackupRun

        live_db = tmp_path / "live.db"
        live_db.write_bytes(b"old")
        wal = tmp_path / "live.db-wal"
        shm = tmp_path / "live.db-shm"
        wal.write_bytes(b"stale-wal")
        shm.write_bytes(b"stale-shm")

        backup_file = tmp_path / "backup.db"
        backup_file.write_bytes(b"restored")

        run = BackupRun(id="b1", triggered_by="manual", status="success", file_path=str(backup_file))
        db_session.add(run)
        await db_session.commit()

        token = request_restore("b1")
        with (
            patch("app.services.backup._dialect", return_value="sqlite"),
            patch("app.services.backup._sqlite_path", return_value=live_db),
        ):
            await confirm_restore("b1", token)

        assert live_db.read_bytes() == b"restored"
        assert not wal.exists()
        assert not shm.exists()

    @pytest.mark.asyncio
    async def test_mysql_backup_uses_env_not_cli_password(self, tmp_path: Path):
        dest = tmp_path / "dump.sql"
        captured = {}

        async def _fake_run(args, dest_path, env=None):
            captured["args"] = args
            captured["env"] = env
            return 10

        with (
            patch("app.services.backup.settings") as mock_settings,
            patch("app.services.backup._run_dump_subprocess", side_effect=_fake_run),
        ):
            mock_settings.database_url = "mysql+aiomysql://u:s3cr3t@localhost:3306/db"
            size = await _mysql_backup(dest)

        assert size == 10
        assert not any("s3cr3t" in a for a in captured["args"])
        assert captured["env"]["MYSQL_PWD"] == "s3cr3t"


class TestPacksAndGitSync:
    @pytest.mark.asyncio
    async def test_install_blocks_critical_scan_unless_overridden(self, client: AsyncClient, db_session: AsyncSession, tmp_path: Path):
        user, headers = await _create_user(db_session, "pack_user")
        repo_dir = tmp_path / "pack_repo"
        (repo_dir / "cantrips").mkdir(parents=True)
        (repo_dir / "cantrips" / "bad.json").write_text(
            json.dumps({"name": "Bad", "code": "fetch('https://evil.example');"}),
            encoding="utf-8",
        )
        from app.models.linked_repo import LinkedRepo
        repo = LinkedRepo(user_id=user.id, name="Local", url=str(repo_dir), branch="local", is_local=True)
        db_session.add(repo)
        await db_session.commit()
        await db_session.refresh(repo)

        blocked = await client.post(
            "/api/packs/install",
            headers=headers,
            json={"repo_id": repo.id, "file_path": "cantrips/bad.json"},
        )
        assert blocked.status_code == 422

        allowed = await client.post(
            "/api/packs/install",
            headers=headers,
            json={"repo_id": repo.id, "file_path": "cantrips/bad.json", "override_scan": True},
        )
        assert allowed.status_code == 201

    @pytest.mark.asyncio
    async def test_install_enforces_max_map_stages(self, client: AsyncClient, db_session: AsyncSession, tmp_path: Path):
        user, headers = await _create_user(db_session, "map_pack_user")
        repo_dir = tmp_path / "map_repo"
        (repo_dir / "maps").mkdir(parents=True)
        too_many_stages = {"name": "BigMap", "stages": [{"name": f"S{i}", "resources": []} for i in range(10)]}
        (repo_dir / "maps" / "big.json").write_text(json.dumps(too_many_stages), encoding="utf-8")

        from app.models.linked_repo import LinkedRepo
        repo = LinkedRepo(user_id=user.id, name="LocalMap", url=str(repo_dir), branch="local", is_local=True)
        db_session.add(repo)
        await db_session.commit()
        await db_session.refresh(repo)

        resp = await client.post(
            "/api/packs/install",
            headers=headers,
            json={"repo_id": repo.id, "file_path": "maps/big.json"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_pack_sanitizes_zip_slip_and_headers(self, client: AsyncClient, db_session: AsyncSession):
        user, headers = await _create_user(db_session, "zip_user")
        cantrip = Cantrip(user_id=user.id, name="../../evil\r\nname", code="console.log(1);")
        db_session.add(cantrip)
        await db_session.commit()
        await db_session.refresh(cantrip)

        resp = await client.post(
            "/api/packs/create",
            headers=headers,
            json={
                "pack_name": "../bad\r\npack",
                "resources": [{"type": "cantrip", "id": cantrip.id}],
            },
        )
        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        assert "\r" not in disp and "\n" not in disp and ".." not in disp

        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                assert ".." not in name
                assert not name.startswith("/")

    def test_git_sync_scrubs_token_and_passes_branch(self):
        with patch("dulwich.porcelain.clone", side_effect=RuntimeError("https://token:SECRET_TOK@example.com failed")) as mock_clone:
            with pytest.raises(GitCloneError) as exc_info:
                fetch_repo_info("https://example.com/repo.git", branch="dev", token="SECRET_TOK")
            assert "SECRET_TOK" not in str(exc_info.value)
            assert mock_clone.call_args.kwargs.get("branch") == b"dev"


class TestAdminAuditAndOwnershipFixes:
    @pytest.mark.asyncio
    async def test_admin_audit_logs_returns_all_users_and_total_count(self, client: AsyncClient, db_session: AsyncSession):
        admin, admin_headers = await _create_user(db_session, "audit_admin", is_admin=True)
        other, _ = await _create_user(db_session, "audit_other")
        db_session.add_all([
            AuditLog(user_id=admin.id, action="a1"),
            AuditLog(user_id=other.id, action="a2"),
            AuditLog(user_id=other.id, action="a3"),
        ])
        await db_session.commit()

        resp = await client.get("/api/admin/audit?limit=1&offset=0", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["logs"]) == 1
        assert data["total"] == 3

        # Attribution: with every user's rows in one list, each row must say
        # whose it is (user id + resolved username).
        resp = await client.get("/api/admin/audit?limit=10", headers=admin_headers)
        assert resp.status_code == 200
        rows = resp.json()["logs"]
        assert {(row["user_id"], row["username"]) for row in rows} == {
            (admin.id, "audit_admin"),
            (other.id, "audit_other"),
        }

    @pytest.mark.asyncio
    async def test_storage_import_ownership_and_export_all(self, client: AsyncClient, db_session: AsyncSession):
        owner, owner_headers = await _create_user(db_session, "store_owner")
        other, other_headers = await _create_user(db_session, "store_other")
        c_other = Cantrip(user_id=other.id, name="OtherCantrip", code="")
        c_owner = Cantrip(user_id=owner.id, name="OwnerCantrip", code="")
        db_session.add_all([c_other, c_owner])
        await db_session.commit()
        await db_session.refresh(c_other)
        await db_session.refresh(c_owner)

        db_session.add(ChatData(user_id=owner.id, conversation_id="conv1", key="k1", value_json="not-json"))
        db_session.add(CantripData(user_id=owner.id, cantrip_id=c_owner.id, key="ck1", value_json=json.dumps({"a": 1})))
        await db_session.commit()

        # Cannot import into another user's cantrip
        imp_resp = await client.post(
            "/api/storage/import",
            headers=owner_headers,
            json={
                "target_cantrip_id": c_other.id,
                "payload": {"format": "gitv-storage-export", "version": 1, "scope": "cantrip", "rows": [{"key": "k", "value": 1}]},
            },
        )
        assert imp_resp.status_code == 404

        # Export all includes cantrip data, handles invalid JSON, and does not duplicate conversation_id
        exp_resp = await client.get("/api/storage/export?scope=all&conversation_id=conv1", headers=owner_headers)
        assert exp_resp.status_code == 200
        body = exp_resp.json()
        scopes = [b["scope"] for b in body["bundles"]]
        assert scopes.count("chat") == 1
        assert "cantrip" in scopes

    @pytest.mark.asyncio
    async def test_settings_clear_map_and_ownership_checks(self, client: AsyncClient, db_session: AsyncSession):
        u1, h1 = await _create_user(db_session, "set_u1")
        u2, _ = await _create_user(db_session, "set_u2")
        m1 = Map(user_id=u1.id, name="M1")
        m2 = Map(user_id=u2.id, name="M2")
        ep2 = Endpoint(user_id=u2.id, name="EP2", base_url="http://localhost:1234")
        db_session.add_all([m1, m2, ep2])
        await db_session.commit()
        await db_session.refresh(m1)
        await db_session.refresh(m2)
        await db_session.refresh(ep2)

        assert (await client.put("/api/settings", headers=h1, json={"default_map_id": m1.id})).status_code == 200
        clear_resp = await client.put("/api/settings", headers=h1, json={"default_map_id": None})
        assert clear_resp.status_code == 200
        assert clear_resp.json()["default_map_id"] is None

        assert (await client.put("/api/settings", headers=h1, json={"default_map_id": m2.id})).status_code == 404
        assert (await client.put("/api/settings", headers=h1, json={"default_endpoint_id": ep2.id})).status_code == 404
        assert (await client.put("/api/settings", headers=h1, json={"lexicon_endpoint_id": ep2.id})).status_code == 404

    def test_sanitization_url_blocklist_matches_hostname_only(self):
        assert scan_urls("https://good.com/?next=evil.com", ["evil.com"]) == []
        assert scan_urls("https://sub.evil.com/path", ["evil.com"]) == ["https://sub.evil.com/path"]
        assert scan_urls("https://notevil.com/path", ["evil.com"]) == []

    @pytest.mark.asyncio
    async def test_map_transfer_defaults_and_snapshot(self, db_session: AsyncSession):
        user, _ = await _create_user(db_session, "map_tx_user")
        await build_map_from_export(
            db_session,
            user.id,
            {
                "name": "TxMap",
                "stages": [{
                    "name": "S1",
                    "resources": [{
                        "resource_type": "cantrip",
                        "resource_content": {"name": "TxCantrip", "code": "console.log(1);"},
                    }],
                }],
            },
        )
        c = (await db_session.execute(select(Cantrip).where(Cantrip.user_id == user.id))).scalar_one()
        assert c.run_driver_callable is False

        await _update_resource(db_session, "cantrip", c, {"code": "console.log(2);"})
        snaps = (await db_session.execute(
            select(ResourceSnapshot).where(ResourceSnapshot.resource_id == c.id)
        )).scalars().all()
        assert len(snaps) == 1

    def test_schema_repair_backfill_sql_bool_and_quotes(self):
        from sqlalchemy import Boolean, Column, String, Table

        from app.models import Base as AppBase

        # Test bool default and string quote escaping via temporary column on Base.metadata
        meta = AppBase.metadata
        tbl = Table(
            "_tmp_repair_test",
            meta,
            Column("flag", Boolean, default=False),
            Column("label", String(32), default="it's"),
        )
        try:
            sql_bool = _backfill_sql(Drift("_tmp_repair_test", "flag", "missing_column"))
            sql_str = _backfill_sql(Drift("_tmp_repair_test", "label", "missing_column"))
            assert "FALSE" in (sql_bool or "")
            assert "'it''s'" in (sql_str or "")
        finally:
            meta.remove(tbl)

    @pytest.mark.asyncio
    async def test_cascade_delete_user_cleans_all_tables(self, client: AsyncClient, db_session: AsyncSession):
        _, admin_headers = await _create_user(db_session, "del_admin", is_admin=True)
        target, _ = await _create_user(db_session, "del_target")
        db_session.add_all([
            ApiKey(user_id=target.id, label="k", key_hash="h" + target.id),
            AuditLog(user_id=target.id, action="test"),
            Skill(user_id=target.id, name="s", content="c"),
            UserData(user_id=target.id, key="k", value_json="1"),
        ])
        await db_session.commit()

        resp = await client.delete(f"/api/users/{target.id}", headers=admin_headers)
        assert resp.status_code == 204
        assert (await db_session.execute(select(ApiKey).where(ApiKey.user_id == target.id))).scalar_one_or_none() is None
        assert (await db_session.execute(select(AuditLog).where(AuditLog.user_id == target.id))).scalar_one_or_none() is None
        assert (await db_session.execute(select(Skill).where(Skill.user_id == target.id))).scalar_one_or_none() is None
        assert (await db_session.execute(select(UserData).where(UserData.user_id == target.id))).scalar_one_or_none() is None
