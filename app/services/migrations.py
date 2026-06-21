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
                        if "duplicate column name" in str(e).lower():
                            logger.debug("Migration %s: column already exists, skipping: %s", name, stmt[:60])
                        else:
                            raise
            await _mark_migration_applied(engine, name)
            logger.info("Migration applied: %s", name)
        except Exception:
            logger.exception("Migration failed: %s", name)
            raise
