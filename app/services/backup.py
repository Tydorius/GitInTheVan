from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import async_session
from app.models.backup_run import BackupRun

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DEFAULT_BACKUP_DIR = _DATA_DIR / "backups"

# Short-lived restore confirmation tokens: backup_id -> (token, expires_at).
# In-process only - matches the "best-effort, not enterprise" scope of this
# feature. A server restart (which drops these) simply requires the admin to
# request a new confirmation, which is the safe failure mode.
_RESTORE_TOKENS: dict[str, tuple[str, float]] = {}
_RESTORE_TOKEN_TTL_SECONDS = 300


async def _backup_dir() -> Path:
    from app.services.admin import get_admin_settings

    settings_row = await get_admin_settings()
    if settings_row.backup_dir:
        return Path(settings_row.backup_dir)
    return _DEFAULT_BACKUP_DIR


def _dialect() -> str:
    return make_url(settings.database_url).get_backend_name()


def _sqlite_path() -> Path:
    db_part = settings.database_url.split("///")[-1]
    return Path(db_part)


def _sqlite_backup_sync(dest: Path) -> int:
    """Use SQLite's online backup API for a consistent copy of a live database."""
    src_path = _sqlite_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src_path))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    return dest.stat().st_size


async def _run_dump_subprocess(args: list[str], dest: Path, env: dict[str, str] | None = None) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=f, stderr=asyncio.subprocess.PIPE, env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Dump command failed: {stderr.decode(errors='replace')[:2000]}")
    return dest.stat().st_size


async def _postgres_backup(dest: Path) -> int:
    import os

    url = make_url(settings.database_url)
    args = [
        "pg_dump",
        "-h", url.host or "localhost",
        "-p", str(url.port or 5432),
        "-U", url.username or "",
        "-d", url.database or "",
        "-f", str(dest),
    ]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE, env=env)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {stderr.decode(errors='replace')[:2000]}")
    return dest.stat().st_size


async def _mysql_backup(dest: Path) -> int:
    url = make_url(settings.database_url)
    args = [
        "mariadb-dump" if shutil.which("mariadb-dump") else "mysqldump",
        "-h", url.host or "localhost",
        "-P", str(url.port or 3306),
        "-u", url.username or "",
        f"-p{url.password or ''}",
        url.database or "",
    ]
    return await _run_dump_subprocess(args, dest)


async def create_backup(triggered_by: str) -> BackupRun:
    """Create a database backup using the dialect-appropriate mechanism."""
    dialect = _dialect()
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    ext = "db" if dialect == "sqlite" else "sql"
    dest = (await _backup_dir()) / f"gitinthevan_backup_{timestamp}.{ext}"

    async with async_session() as db:
        run = BackupRun(
            id=str(uuid.uuid4()),
            triggered_by=triggered_by,
            status="running",
            file_path=str(dest),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id

    try:
        if dialect == "sqlite":
            size = await asyncio.to_thread(_sqlite_backup_sync, dest)
        elif dialect == "postgresql":
            size = await _postgres_backup(dest)
        elif dialect in ("mysql", "mariadb"):
            size = await _mysql_backup(dest)
        else:
            raise RuntimeError(f"Unsupported dialect for backup: {dialect}")

        async with async_session() as db:
            result = await db.execute(select(BackupRun).where(BackupRun.id == run_id))
            run = result.scalar_one()
            run.status = "success"
            run.size_bytes = size
            run.completed_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(run)
            await prune_backups(db)
            return run
    except Exception as exc:
        logger.exception("Backup failed")
        async with async_session() as db:
            result = await db.execute(select(BackupRun).where(BackupRun.id == run_id))
            run = result.scalar_one()
            run.status = "failed"
            run.error_message = str(exc)[:2000]
            run.completed_at = datetime.now(UTC)
            await db.commit()
            await db.refresh(run)
            return run


async def list_backups() -> list[BackupRun]:
    async with async_session() as db:
        result = await db.execute(select(BackupRun).order_by(BackupRun.started_at.desc()))
        return list(result.scalars().all())


async def delete_backup(backup_id: str) -> bool:
    async with async_session() as db:
        result = await db.execute(select(BackupRun).where(BackupRun.id == backup_id))
        run = result.scalar_one_or_none()
        if run is None:
            return False
        file_path = Path(run.file_path)
        if file_path.exists():
            file_path.unlink()
        await db.delete(run)
        await db.commit()
        return True


async def prune_backups(db) -> None:
    from app.services.admin import get_admin_settings

    settings_row = await get_admin_settings()
    retention = settings_row.backup_retention_count
    if retention <= 0:
        return
    result = await db.execute(
        select(BackupRun).where(BackupRun.status == "success").order_by(BackupRun.started_at.desc())
    )
    successes = list(result.scalars().all())
    for run in successes[retention:]:
        file_path = Path(run.file_path)
        if file_path.exists():
            file_path.unlink()
        await db.delete(run)
    await db.commit()


_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def backup_scheduler_loop() -> None:
    """Background task: checks once a minute whether a scheduled backup is due.

    Deliberately simple (single job type, in-memory last-run tracking) per the
    "best-effort, not enterprise" scope of this feature - see project-plan.md.
    """
    from app.services.admin import get_admin_settings

    last_run_key: str | None = None
    while True:
        try:
            settings_row = await get_admin_settings()
            if settings_row.backup_schedule_enabled:
                now = datetime.now(UTC)
                today_name = _DAY_NAMES[now.weekday()]
                scheduled_days = [d.strip().lower() for d in settings_row.backup_schedule_days.split(",") if d.strip()]
                current_hm = now.strftime("%H:%M")
                run_key = f"{now.date()}_{settings_row.backup_schedule_time}"
                if (
                    (not scheduled_days or today_name in scheduled_days)
                    and current_hm == settings_row.backup_schedule_time
                    and run_key != last_run_key
                ):
                    last_run_key = run_key
                    logger.info("Scheduled backup triggered")
                    await create_backup(triggered_by="scheduled")
        except Exception:
            logger.exception("Scheduled backup check failed")
        await asyncio.sleep(60)


def request_restore(backup_id: str) -> str:
    """Generate a short-lived confirmation token for a restore request."""
    token = str(uuid.uuid4())
    _RESTORE_TOKENS[backup_id] = (token, time.monotonic() + _RESTORE_TOKEN_TTL_SECONDS)
    return token


def _validate_restore_token(backup_id: str, token: str) -> bool:
    entry = _RESTORE_TOKENS.get(backup_id)
    if entry is None:
        return False
    stored_token, expires_at = entry
    if time.monotonic() > expires_at:
        del _RESTORE_TOKENS[backup_id]
        return False
    return stored_token == token


async def confirm_restore(backup_id: str, token: str) -> None:
    """Restore a backup over the live database. Requires a manual server restart afterward."""
    if not _validate_restore_token(backup_id, token):
        raise ValueError("Invalid or expired restore confirmation token")
    del _RESTORE_TOKENS[backup_id]

    async with async_session() as db:
        result = await db.execute(select(BackupRun).where(BackupRun.id == backup_id))
        run = result.scalar_one_or_none()
        if run is None or run.status != "success":
            raise ValueError("Backup not found or was not successful")
        backup_file = Path(run.file_path)
        if not backup_file.exists():
            raise ValueError("Backup file no longer exists on disk")

    dialect = _dialect()
    if dialect == "sqlite":
        live_path = _sqlite_path()
        shutil.copy2(backup_file, live_path)
    else:
        raise ValueError(
            f"Automatic restore is only implemented for SQLite. For {dialect}, "
            f"restore the dump manually using the native tool (psql/mysql) "
            f"against file {backup_file}."
        )
