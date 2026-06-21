from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_add_lorebook_is_active",
        "ALTER TABLE lorebooks ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL;",
    ),
    (
        "002_add_endpoint_api_base_path",
        "ALTER TABLE endpoints ADD COLUMN api_base_path VARCHAR(128) DEFAULT '' NOT NULL;",
    ),
    (
        "003_add_user_settings_verification_fields",
        """
        ALTER TABLE user_settings ADD COLUMN verification_enabled BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN verification_endpoint_id VARCHAR(36);
        ALTER TABLE user_settings ADD COLUMN verification_model VARCHAR(128) DEFAULT '' NOT NULL;
        """,
    ),
    (
        "004_add_user_settings_ux_fields",
        """
        ALTER TABLE user_settings ADD COLUMN preserve_thinking BOOLEAN DEFAULT 1 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN gitv_status BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN simulated_streaming_speed INTEGER DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "005_add_tagging_columns",
        """
        ALTER TABLE lorebooks ADD COLUMN tag VARCHAR(128) DEFAULT '' NOT NULL;
        ALTER TABLE cantrips ADD COLUMN tag VARCHAR(128) DEFAULT '' NOT NULL;
        ALTER TABLE verification_rules ADD COLUMN tag VARCHAR(128) DEFAULT '' NOT NULL;
        """,
    ),
    (
        "006_seed_default_tags",
        """
        UPDATE lorebooks SET tag = (
            SELECT COUNT(*) FROM lorebooks AS l2 WHERE l2.rowid < lorebooks.rowid
        ) + 1 WHERE tag = '' OR tag IS NULL;
        UPDATE cantrips SET tag = (
            SELECT COUNT(*) FROM cantrips AS c2 WHERE c2.rowid < cantrips.rowid
        ) + 1 WHERE tag = '' OR tag IS NULL;
        UPDATE verification_rules SET tag = (
            SELECT COUNT(*) FROM verification_rules AS v2 WHERE v2.rowid < verification_rules.rowid
        ) + 1 WHERE tag = '' OR tag IS NULL;
        """,
    ),
    (
        "007_fix_double_prefixed_tags",
        """
        UPDATE lorebooks SET tag = REPLACE(REPLACE(tag, 'lore-', ''), 'lore_', '') WHERE tag LIKE 'lore-%' OR tag LIKE 'lore_%';
        UPDATE cantrips SET tag = REPLACE(REPLACE(tag, 'cantrip-', ''), 'cantrip_', '') WHERE tag LIKE 'cantrip-%' OR tag LIKE 'cantrip_%';
        UPDATE verification_rules SET tag = REPLACE(REPLACE(tag, 'verify-', ''), 'verify_', '') WHERE tag LIKE 'verify-%' OR tag LIKE 'verify_%';
        """,
    ),
]


async def _ensure_migrations_table(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS _migrations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(128) UNIQUE NOT NULL, "
                "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ");"
            )
        )


async def _get_applied_migrations(engine: AsyncEngine) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT name FROM _migrations;"))
        return {row[0] for row in result.fetchall()}


async def _mark_migration_applied(engine: AsyncEngine, name: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO _migrations (name) VALUES (:name);"),
            {"name": name},
        )


async def run_migrations(engine: AsyncEngine) -> None:
    await _ensure_migrations_table(engine)
    applied = await _get_applied_migrations(engine)

    for name, sql in MIGRATIONS:
        if name in applied:
            continue

        statements = [s.strip() for s in sql.split(";") if s.strip()]
        try:
            async with engine.begin() as conn:
                for stmt in statements:
                    try:
                        await conn.execute(text(stmt))
                    except Exception as e:
                        err_lower = str(e).lower()
                        if "duplicate column name" in err_lower or "no such column" in err_lower:
                            logger.debug("Migration %s: skipping: %s", name, stmt[:80])
                        else:
                            raise
            await _mark_migration_applied(engine, name)
            logger.info("Migration applied: %s", name)
        except Exception:
            logger.exception("Migration failed: %s", name)
            raise
