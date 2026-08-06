"""One-time schema drift repair for databases botched by a 0.15.x -> 0.18.0 upgrade.

0.18.0 added `skills.budget_weight` to the model with no migration. Because
`Base.metadata.create_all` only creates *missing tables* and never alters an
existing one, the column was present on fresh installs and absent on every
upgrade -- surfacing as `no such column: skills.budget_weight` on the proxy hot
path. Migration `042` fixes that specific column going forward; this module
exists for databases already in the broken state, and finds drift generically
rather than from a hardcoded list so it also catches omissions we have not found.

The approach is reflect-and-repair, additive only. There is deliberately no
"rebuild into a clean database" path: everything found so far is additively
repairable, and a full extract-and-reimport risks silent data loss on any mapping
gap across three dialects. Drift that cannot be fixed additively is reported and
the completion marker is withheld, so the repair retries on the next boot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateColumn

from app.services.migrations import (
    _acquire_lock,
    _ensure_migrations_table,
    _is_tolerable_error,
    _release_lock,
)

logger = logging.getLogger(__name__)

# Recorded in the existing _migrations table rather than a file in data/. That
# makes the flag database-scoped: restoring a pre-0.18.0 backup correctly
# re-triggers the repair, where a file flag would wrongly suppress it.
# run_migrations() only iterates its own MIGRATIONS list and checks membership,
# so a row with a name it does not know is inert.
REPAIR_MARKER = "repair_001_schema_drift_0_15_to_0_18"

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_REPAIR_LOG_PATH = _DATA_DIR / "schema-repair.log"


@dataclass
class Drift:
    """One difference between the live database and the ORM metadata."""

    table: str
    column: str
    kind: str  # "missing_column" | "narrow_column" | "missing_table"
    detail: str = ""
    repairable: bool = True


@dataclass
class RepairSummary:
    ran: bool = False
    marker_set: bool = False
    backup_file: str = ""
    applied: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "marker_set": self.marker_set,
            "backup_file": self.backup_file,
            "applied": self.applied,
            "reported": self.reported,
            "remaining": self.remaining,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

def _column_length(type_: Any) -> int | None:
    return getattr(type_, "length", None)


def _diff_schema_sync(sync_conn) -> list[Drift]:
    """Compare reflected database columns against Base.metadata."""
    from app.models import Base

    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    drifts: list[Drift] = []

    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            # create_all has already run by this point; a still-missing table is
            # a real problem but not one an ADD COLUMN can fix.
            drifts.append(
                Drift(table_name, "", "missing_table", "table absent after create_all", False)
            )
            continue

        live = {c["name"]: c for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in live:
                drifts.append(
                    Drift(table_name, column.name, "missing_column", str(column.type))
                )
                continue

            want = _column_length(column.type)
            have = _column_length(live[column.name]["type"])
            if want and have and have < want:
                drifts.append(
                    Drift(
                        table_name,
                        column.name,
                        "narrow_column",
                        f"database has length {have}, model expects {want}",
                    )
                )

    return drifts


async def _diff_schema(engine: AsyncEngine) -> list[Drift]:
    async with engine.connect() as conn:
        return await conn.run_sync(_diff_schema_sync)


# ---------------------------------------------------------------------------
# DDL generation
# ---------------------------------------------------------------------------

def _add_column_ddl(drift: Drift, dialect) -> str | None:
    """Render `ALTER TABLE <t> ADD COLUMN <col spec>` for the live dialect."""
    from app.models import Base

    table = Base.metadata.tables.get(drift.table)
    if table is None:
        return None
    column = table.columns.get(drift.column)
    if column is None:
        return None

    try:
        spec = str(CreateColumn(column).compile(dialect=dialect))
    except Exception as e:
        logger.warning("Could not compile DDL for %s.%s: %s", drift.table, drift.column, e)
        return None

    # A NOT NULL column added to a table with existing rows needs a default to
    # backfill with. Without one the ALTER is rejected, so relax to NULL and let
    # the backfill pass populate it.
    if "NOT NULL" in spec.upper() and "DEFAULT" not in spec.upper():
        spec = spec.replace(" NOT NULL", "").replace(" not null", "")

    return f"ALTER TABLE {drift.table} ADD COLUMN {spec};"


def _widen_column_ddl(drift: Drift, dialect_name: str) -> str | None:
    """Widen a VARCHAR. SQLite ignores length entirely, so it needs nothing."""
    from app.models import Base

    table = Base.metadata.tables.get(drift.table)
    if table is None:
        return None
    column = table.columns.get(drift.column)
    if column is None:
        return None
    length = _column_length(column.type)
    if not length:
        return None

    if dialect_name == "postgresql":
        return (
            f"ALTER TABLE {drift.table} "
            f"ALTER COLUMN {drift.column} TYPE VARCHAR({length});"
        )
    if dialect_name == "mysql":
        nullable = "NULL" if column.nullable else "NOT NULL"
        return (
            f"ALTER TABLE {drift.table} "
            f"MODIFY COLUMN {drift.column} VARCHAR({length}) {nullable};"
        )
    # SQLite does not enforce VARCHAR length; nothing to do.
    return None


def _backfill_sql(drift: Drift) -> str | None:
    """Populate a freshly added column so NULLs never reach the ORM."""
    from app.models import Base

    table = Base.metadata.tables.get(drift.table)
    column = table.columns.get(drift.column) if table is not None else None
    if column is None:
        return None

    default = None
    if column.server_default is not None and hasattr(column.server_default, "arg"):
        default = str(column.server_default.arg)
    elif column.default is not None and not callable(getattr(column.default, "arg", None)):
        default = getattr(column.default, "arg", None)

    if default is None:
        return None

    literal = default if _looks_numeric(default) else f"'{str(default)}'"
    return (
        f"UPDATE {drift.table} SET {drift.column} = {literal} "
        f"WHERE {drift.column} IS NULL;"
    )


def _looks_numeric(value: Any) -> bool:
    try:
        float(str(value))
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _marker_present(engine: AsyncEngine) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM _migrations WHERE name = :name;"), {"name": REPAIR_MARKER}
        )
        return result.first() is not None


async def _set_marker(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO _migrations (name) VALUES (:name);"), {"name": REPAIR_MARKER}
        )


async def _execute(engine: AsyncEngine, statement: str) -> str:
    """Run one DDL/DML statement in its own transaction.

    Per-statement transactions because PostgreSQL poisons the whole transaction
    when any statement errors, which makes shared-transaction error tolerance
    impossible -- the same reason migrations.py does it this way.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement))
        return ""
    except Exception as exc:
        if _is_tolerable_error(exc):
            logger.debug("Schema repair: tolerable error, skipping: %s", statement[:80])
            return ""
        return str(exc)


def _write_repair_log(summary: RepairSummary) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        lines = [f"{datetime.now(UTC).isoformat()} schema repair"]
        for label, entries in (
            ("applied", summary.applied),
            ("reported", summary.reported),
            ("remaining", summary.remaining),
        ):
            for entry in entries:
                lines.append(f"  {label}: {entry}")
        if summary.backup_file:
            lines.append(f"  backup: {summary.backup_file}")
        if summary.error:
            lines.append(f"  error: {summary.error}")
        with _REPAIR_LOG_PATH.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        logger.warning("Could not write schema-repair.log: %s", e)


async def run_schema_repair(engine: AsyncEngine) -> dict[str, Any]:
    """Detect and additively repair schema drift, once per database."""
    summary = RepairSummary()
    dialect = engine.dialect.name

    await _ensure_migrations_table(engine, dialect)
    if await _marker_present(engine):
        return summary.as_dict()

    drifts = await _diff_schema(engine)
    repairable = [d for d in drifts if d.kind == "missing_column"]
    unrepairable = [d for d in drifts if not d.repairable]

    # A VARCHAR widening is a real fix on PostgreSQL and MySQL but a no-op on
    # SQLite, which does not enforce length. Split them so a SQLite install is
    # not backed up and "repaired" over a difference that cannot affect it.
    widenings, cosmetic = [], []
    for drift in (d for d in drifts if d.kind == "narrow_column"):
        (widenings if _widen_column_ddl(drift, dialect) else cosmetic).append(drift)

    if not repairable and not widenings:
        for drift in cosmetic:
            summary.reported.append(
                f"{drift.table}.{drift.column}: {drift.detail} (not enforced on {dialect})"
            )
        for drift in unrepairable:
            summary.reported.append(f"{drift.table}: {drift.detail}")
        if not unrepairable:
            await _set_marker(engine)
            summary.marker_set = True
        logger.info("Schema repair: nothing to repair on %s", dialect)
        return summary.as_dict()

    summary.ran = True
    logger.warning(
        "Schema repair: found %d drift item(s): %s",
        len(drifts),
        ", ".join(f"{d.table}.{d.column or '*'} ({d.kind})" for d in drifts),
    )

    # Never mutate a schema that could not be snapshotted first.
    try:
        from app.services.backup import create_backup

        run = await create_backup(triggered_by="repair")
        if run.status != "success":
            summary.error = f"Backup failed ({run.error_message}); schema repair aborted"
            logger.error("Schema repair: %s", summary.error)
            _write_repair_log(summary)
            return summary.as_dict()
        summary.backup_file = run.file_path
    except Exception as e:
        summary.error = f"Backup failed ({e}); schema repair aborted"
        logger.exception("Schema repair aborted: backup failed")
        _write_repair_log(summary)
        return summary.as_dict()

    await _acquire_lock(engine, dialect)
    try:
        for drift in repairable:
            ddl = _add_column_ddl(drift, engine.dialect)
            if ddl is None:
                summary.reported.append(f"{drift.table}.{drift.column}: could not render DDL")
                continue
            error = await _execute(engine, ddl)
            if error:
                summary.reported.append(f"{drift.table}.{drift.column}: {error}")
                continue
            summary.applied.append(f"added {drift.table}.{drift.column}")

            backfill = _backfill_sql(drift)
            if backfill:
                await _execute(engine, backfill)

        for drift in widenings:
            ddl = _widen_column_ddl(drift, dialect)
            error = await _execute(engine, ddl) if ddl else ""
            if error:
                summary.reported.append(f"{drift.table}.{drift.column}: {error}")
            elif ddl:
                summary.applied.append(f"widened {drift.table}.{drift.column}")

        for drift in cosmetic:
            summary.reported.append(
                f"{drift.table}.{drift.column}: {drift.detail} (not enforced on {dialect})"
            )

        for drift in unrepairable:
            summary.reported.append(f"{drift.table}: {drift.detail}")
    finally:
        await _release_lock(engine, dialect)

    # Verify before marking: a marker set over unrepaired drift would suppress
    # every future attempt.
    remaining = [d for d in await _diff_schema(engine) if d.kind == "missing_column"]
    summary.remaining = [f"{d.table}.{d.column}" for d in remaining]

    if remaining:
        summary.error = "Drift remains after repair; will retry on next start"
        logger.error("Schema repair: %s -- %s", summary.error, summary.remaining)
    else:
        await _set_marker(engine)
        summary.marker_set = True
        logger.info(
            "Schema repair complete: %s", ", ".join(summary.applied) or "no changes needed"
        )

    _write_repair_log(summary)
    return summary.as_dict()
