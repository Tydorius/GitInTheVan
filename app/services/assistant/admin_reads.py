"""Opt-in, read-only admin tools (sub-phase 26d).

Gated twice before a call ever reaches here: `registry.visible_tools` hides
every tool in this module unless the caller is an admin *and*
`assistant_admin_reads_enabled` is on, and each `Tool` below also carries
`admin_only=True` so a future group-flag change alone cannot expose it. There
is no admin *write* tool anywhere in the assistant -- `schema_report` never
calls `run_schema_repair`, and nothing here mutates state.

Each check is a coroutine `run(ctx, args) -> list[selfcheck.CheckResult]`,
reusing the same shape self-checks use so `registry.py` can wrap both with one
kind of handler. Like self-checks, nothing here may raise past its own
boundary.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select

from app.database import async_session, engine
from app.services.assistant.selfcheck import CheckResult, _fail

logger = logging.getLogger(__name__)


def _guard(name: str, fn):
    async def run(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
        try:
            return await fn(ctx, args)
        except Exception as exc:  # noqa: BLE001 - admin reads must never 500 the turn
            return _fail(name, exc)

    return run


# ---------------------------------------------------------------------------
# schema_report
# ---------------------------------------------------------------------------


async def _schema_report(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    """Three independent categories. Each is wrapped on its own so a problem
    reading the migrations bookkeeping table (for example) does not erase an
    otherwise-successful schema diff -- the same "one exception, one failed
    category" shape `install_health` uses below."""
    from app.services import migrations as migrations_module
    from app.services.schema_repair import _add_column_ddl, _diff_schema, _marker_present

    results: list[CheckResult] = []

    try:
        drifts = await _diff_schema(engine)
        if not drifts:
            results.append(CheckResult("schema drift", True, "The live schema matches the ORM metadata exactly"))
        else:
            dialect = engine.dialect
            for d in drifts:
                preview = _add_column_ddl(d, dialect) if d.kind != "missing_table" else None
                results.append(
                    CheckResult(
                        name=f"drift: {d.table}.{d.column}" if d.column else f"drift: {d.table}",
                        passed=False,
                        message=f"{d.kind}: {d.detail}",
                        detail=(preview or ""),
                    )
                )
    except Exception as exc:
        results.append(CheckResult("schema drift", False, f"{type(exc).__name__}: {exc}"))

    try:
        marker_set = await _marker_present(engine)
        results.append(
            CheckResult(
                name="repair marker",
                passed=True,
                message=("The 0.15->0.18 schema-drift repair has already run" if marker_set else "The 0.15->0.18 schema-drift repair marker is not set"),
            )
        )
    except Exception as exc:
        results.append(CheckResult("repair marker", False, f"{type(exc).__name__}: {exc}"))

    try:
        applied = await migrations_module._get_applied_migrations(engine)
        known = [name for name, _ in migrations_module.MIGRATIONS]
        missing = [name for name in known if name not in applied]
        results.append(
            CheckResult(
                name="applied migrations",
                passed=not missing,
                message=(f"All {len(known)} known migration(s) applied" if not missing else f"{len(missing)} migration(s) not yet applied"),
                detail=json.dumps({"missing": missing, "applied_count": len(applied), "known_count": len(known)}),
            )
        )
    except Exception as exc:
        results.append(CheckResult("applied migrations", False, f"{type(exc).__name__}: {exc}"))

    return results


# ---------------------------------------------------------------------------
# read_server_logs
# ---------------------------------------------------------------------------


async def _read_server_logs(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    from app.models.endpoint import Endpoint
    from app.services import log_manager
    from app.services.assistant.executor import redact_text

    try:
        lines = int(args.get("lines", 200))
    except (TypeError, ValueError):
        lines = 200
    lines = min(max(lines, 1), 1000)

    raw_lines = log_manager.read_recent_logs(lines)
    joined = "\n".join(raw_lines)

    async with async_session() as db:
        all_keys = (await db.execute(select(Endpoint.api_key))).scalars().all()
    secrets = tuple(k for k in all_keys if k) + tuple(ctx.secrets or ())

    redacted = redact_text(joined, secrets)

    return [
        CheckResult(
            name="read_server_logs",
            passed=True,
            message=f"{len(raw_lines)} log line(s) returned, redacted",
            detail=redacted,
        )
    ]


# ---------------------------------------------------------------------------
# install_health
# ---------------------------------------------------------------------------


async def _install_health(ctx: Any, args: dict[str, Any]) -> list[CheckResult]:
    import shutil

    import httpx

    results: list[CheckResult] = []

    # --- signing key -----------------------------------------------------
    try:
        from app.config import settings as app_settings
        from app.services.secret_key import SECRET_KEY_PATH, _is_placeholder

        configured_ok = not _is_placeholder(app_settings.secret_key)
        persisted_ok = SECRET_KEY_PATH.exists()
        readable_ok = True
        if persisted_ok:
            try:
                stored = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
                readable_ok = bool(stored) and not _is_placeholder(stored)
            except OSError:
                readable_ok = False
        results.append(
            CheckResult(
                name="signing key",
                passed=configured_ok,
                message=(
                    "A non-placeholder signing key is in use"
                    if configured_ok
                    else "The JWT signing key is unset or is still the shipped placeholder"
                ),
                detail=json.dumps({"persisted": persisted_ok, "readable": readable_ok}),
            )
        )
    except Exception as exc:
        results.append(CheckResult("signing key", False, f"{type(exc).__name__}: {exc}"))

    # --- backups -----------------------------------------------------------
    try:
        from app.services.admin import get_admin_settings
        from app.services.backup import _backup_dir, _dialect, list_backups

        admin_settings = await get_admin_settings()
        backup_dir = await _backup_dir()
        dir_ok = backup_dir.exists() and backup_dir.is_dir()
        writable = False
        if dir_ok:
            try:
                probe = backup_dir / ".selfcheck-write-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                writable = True
            except OSError:
                writable = False
        elif not backup_dir.exists():
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                writable = True
                dir_ok = True
            except OSError:
                writable = False

        dialect = _dialect()
        dump_tool_ok = True
        dump_tool_name = ""
        if dialect == "postgresql":
            dump_tool_name = "pg_dump"
            dump_tool_ok = shutil.which("pg_dump") is not None
        elif dialect in ("mysql", "mariadb"):
            dump_tool_name = "mariadb-dump or mysqldump"
            dump_tool_ok = shutil.which("mariadb-dump") is not None or shutil.which("mysqldump") is not None

        backups = await list_backups()
        retention = admin_settings.backup_retention_count
        newest_age = None
        if backups:
            from datetime import UTC, datetime

            newest_age = (datetime.now(UTC) - backups[0].started_at).total_seconds() / 3600.0

        results.append(
            CheckResult(
                name="backup directory",
                passed=dir_ok and writable,
                message=(
                    f"'{backup_dir}' exists and is writable"
                    if dir_ok and writable
                    else f"'{backup_dir}' is missing or not writable"
                ),
            )
        )
        results.append(
            CheckResult(
                name="backup dump tool",
                passed=dump_tool_ok,
                message=(
                    "No external dump tool is required for sqlite"
                    if not dump_tool_name
                    else f"{dump_tool_name} found on PATH"
                    if dump_tool_ok
                    else f"{dump_tool_name} not found on PATH; backups for this dialect will fail"
                ),
            )
        )
        results.append(
            CheckResult(
                name="backup history",
                passed=True,
                message=(
                    f"{len(backups)} backup(s) on record (retention {retention}); newest is "
                    f"{newest_age:.1f}h old"
                    if backups and newest_age is not None
                    else f"No backups on record yet (retention {retention})"
                ),
            )
        )
    except Exception as exc:
        results.append(CheckResult("backups", False, f"{type(exc).__name__}: {exc}"))

    # --- SSL -----------------------------------------------------------------
    try:
        from app.services.ssl_manager import check_cert_ip_mismatch, get_ssl_status

        status = get_ssl_status()
        results.append(
            CheckResult(
                name="SSL status",
                passed=True,
                message=("Active (cert present)" if status.get("is_active") else "Not active"),
                detail=json.dumps({k: v for k, v in status.items() if k != "cert_info"}),
            )
        )
        mismatch = check_cert_ip_mismatch(force=True)
        results.append(
            CheckResult(
                name="certificate / LAN address match",
                passed=not mismatch.get("mismatch", False),
                message=(
                    "No mismatch between the certificate and the host's current LAN addresses"
                    if not mismatch.get("mismatch")
                    else "The certificate does not cover a current LAN address"
                ),
                detail=json.dumps(mismatch),
            )
        )
    except Exception as exc:
        results.append(CheckResult("SSL", False, f"{type(exc).__name__}: {exc}"))

    # --- .env drift ------------------------------------------------------
    try:
        from pathlib import Path

        from app.services.env_sync import parse_env_keys

        root = Path(__file__).resolve().parent.parent.parent.parent
        env_keys = parse_env_keys(root / ".env")
        example_keys = parse_env_keys(root / ".env.example")
        missing = sorted(set(example_keys) - set(env_keys))
        results.append(
            CheckResult(
                name=".env drift",
                passed=not missing,
                message=(f"{len(missing)} key(s) present in .env.example but missing from .env" if missing else ".env has every key .env.example declares"),
                detail=json.dumps(missing),
            )
        )
    except Exception as exc:
        results.append(CheckResult(".env drift", False, f"{type(exc).__name__}: {exc}"))

    # --- update chain --------------------------------------------------------
    try:
        from app.services.updater import _chain_expired, chain_status, read_chain

        status = chain_status()
        chain = read_chain()
        expired = bool(chain and _chain_expired(chain))
        stalled = bool(status.get("active") and status.get("error"))
        healthy = not (expired or stalled)
        results.append(
            CheckResult(
                name="update chain",
                passed=healthy,
                message=(
                    "No update chain in progress"
                    if not status.get("active")
                    else "Update chain is stalled with an error"
                    if stalled
                    else "Update chain has been idle long enough to be considered expired"
                    if expired
                    else f"Update chain in progress: step {status.get('current_step')} of {status.get('total_steps')}"
                ),
                detail=json.dumps({"status": status.get("status"), "error": status.get("error")}),
            )
        )
    except Exception as exc:
        results.append(CheckResult("update chain", False, f"{type(exc).__name__}: {exc}"))

    # --- self-reachability -----------------------------------------------
    try:
        from app.services.updater import _health_url

        url = _health_url()
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        results.append(
            CheckResult(
                name="self-reachability",
                passed=resp.status_code == 200,
                message=f"{url} returned {resp.status_code}",
            )
        )
    except Exception as exc:
        results.append(CheckResult("self-reachability", False, f"{type(exc).__name__}: {exc}"))

    return results


HANDLERS: dict[str, Any] = {
    "schema_report": _guard("schema_report", _schema_report),
    "read_server_logs": _guard("read_server_logs", _read_server_logs),
    "install_health": _guard("install_health", _install_health),
}
