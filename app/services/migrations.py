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
    (
        "008_add_user_settings_summarization_fields",
        """
        ALTER TABLE user_settings ADD COLUMN summarization_enabled BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN summarization_endpoint_id VARCHAR(36);
        ALTER TABLE user_settings ADD COLUMN summarization_model VARCHAR(128) DEFAULT '' NOT NULL;
        ALTER TABLE user_settings ADD COLUMN summarization_token_threshold INTEGER DEFAULT 8000 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN summarization_keep_recent INTEGER DEFAULT 6 NOT NULL;
        """,
    ),
    (
        "009_add_user_settings_summarization_prompt",
        """
        ALTER TABLE user_settings ADD COLUMN summarization_prompt TEXT DEFAULT 'Summarize the following roleplay conversation excerpt. Preserve key facts, character development, important plot points, established relationships, locations, items, and any commitments or promises made. Write in concise bullet points. Do not add new information. Output only the summary.' NOT NULL;
        """,
    ),
    (
        "010_add_cantrip_position_flags",
        """
        ALTER TABLE cantrips ADD COLUMN run_pre_driver BOOLEAN DEFAULT 1 NOT NULL;
        ALTER TABLE cantrips ADD COLUMN run_driver_callable BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE cantrips ADD COLUMN run_pre_navigator BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE cantrips ADD COLUMN run_post_navigator BOOLEAN DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "011_add_lorebook_position_flags",
        """
        ALTER TABLE lorebooks ADD COLUMN run_pre_driver BOOLEAN DEFAULT 1 NOT NULL;
        ALTER TABLE lorebooks ADD COLUMN run_driver_callable BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE lorebooks ADD COLUMN run_pre_navigator BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE lorebooks ADD COLUMN run_post_navigator BOOLEAN DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "012_add_user_settings_forbidden_words_fields",
        """
        ALTER TABLE user_settings ADD COLUMN forbidden_words_enabled BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN forbidden_words_case_sensitive BOOLEAN DEFAULT 0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN driver_callable_turns INTEGER DEFAULT 1 NOT NULL;
        """,
    ),
    (
        "013_create_forbidden_words_table",
        """
        CREATE TABLE IF NOT EXISTS forbidden_words (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            phrase TEXT NOT NULL,
            is_regex BOOLEAN DEFAULT 0 NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
    ),
    (
        "014_add_user_is_disabled",
        """
        ALTER TABLE users ADD COLUMN is_disabled BOOLEAN DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "015_add_llm_instructions",
        """
        ALTER TABLE cantrips ADD COLUMN llm_instructions TEXT DEFAULT '' NOT NULL;
        ALTER TABLE lorebooks ADD COLUMN llm_instructions TEXT DEFAULT '' NOT NULL;
        """,
    ),
    (
        "016_add_bypass_and_prefill_settings",
        """
        ALTER TABLE user_settings ADD COLUMN bypass_method VARCHAR(32) DEFAULT 'none' NOT NULL;
        ALTER TABLE user_settings ADD COLUMN prefill_enabled BOOLEAN DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "017_add_context_budget_fields",
        """
        ALTER TABLE cantrips ADD COLUMN budget_weight REAL DEFAULT 1.0 NOT NULL;
        ALTER TABLE lorebooks ADD COLUMN budget_weight REAL DEFAULT 1.0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN context_budget_percent REAL DEFAULT 10.0 NOT NULL;
        ALTER TABLE user_settings ADD COLUMN context_window_override INTEGER DEFAULT 0 NOT NULL;
        """,
    ),
    (
        "018_create_memory_rules_table",
        """
        CREATE TABLE IF NOT EXISTS memory_rules (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(128) NOT NULL,
            description TEXT DEFAULT '' NOT NULL,
            summarization_enabled BOOLEAN DEFAULT 1 NOT NULL,
            token_threshold INTEGER DEFAULT 0 NOT NULL,
            keep_recent INTEGER DEFAULT 0 NOT NULL,
            prompt TEXT DEFAULT '' NOT NULL,
            tag VARCHAR(128) DEFAULT '' NOT NULL,
            execution_order INTEGER DEFAULT 10 NOT NULL,
            is_active BOOLEAN DEFAULT 1 NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_memory_rules_user_id ON memory_rules (user_id);
        """,
    ),
    (
        "019_add_debug_mode_fields",
        """
        ALTER TABLE user_settings ADD COLUMN debug_mode BOOLEAN DEFAULT 0 NOT NULL;
        CREATE TABLE IF NOT EXISTS debug_exchanges (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chat_id VARCHAR(256) DEFAULT '' NOT NULL,
            model VARCHAR(128) DEFAULT '' NOT NULL,
            pipeline_data TEXT DEFAULT '{}' NOT NULL,
            response_content TEXT DEFAULT '' NOT NULL,
            verification_data TEXT DEFAULT '' NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_debug_exchanges_user_id ON debug_exchanges (user_id);
        """,
    ),
    (
        "020_add_per_rule_verification_endpoints",
        """
        ALTER TABLE verification_rules ADD COLUMN verification_endpoint_id VARCHAR(36);
        ALTER TABLE verification_rules ADD COLUMN verification_model VARCHAR(128) DEFAULT '' NOT NULL;
        """,
    ),
    (
        "021_add_api_key_endpoint_mapping",
        """
        ALTER TABLE api_keys ADD COLUMN endpoint_id VARCHAR(36) REFERENCES endpoints(id) ON DELETE SET NULL;
        ALTER TABLE api_keys ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL;
        ALTER TABLE api_keys ADD COLUMN last_used_at TIMESTAMP;
        """,
    ),
    (
        "022_add_security_hardening_fields",
        """
        ALTER TABLE user_settings ADD COLUMN allowed_origins TEXT DEFAULT '' NOT NULL;
        ALTER TABLE user_settings ADD COLUMN max_request_size INTEGER DEFAULT 0 NOT NULL;
        CREATE TABLE IF NOT EXISTS audit_logs (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action VARCHAR(128) NOT NULL,
            target_type VARCHAR(64) DEFAULT '' NOT NULL,
            target_id VARCHAR(36) DEFAULT '' NOT NULL,
            details TEXT DEFAULT '' NOT NULL,
            ip_address VARCHAR(64) DEFAULT '' NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs (user_id);
        CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);
        """,
    ),
    (
        "023_add_admin_settings_and_caps",
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            max_driver_callable_turns INTEGER DEFAULT 2 NOT NULL,
            max_verification_retries INTEGER DEFAULT 3 NOT NULL,
            rate_limit_proxy_per_min INTEGER DEFAULT 60 NOT NULL,
            rate_limit_api_per_min INTEGER DEFAULT 120 NOT NULL,
            runtime_log_level VARCHAR(16) DEFAULT '' NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO admin_settings (id, max_driver_callable_turns, max_verification_retries, rate_limit_proxy_per_min, rate_limit_api_per_min, runtime_log_level) VALUES (1, 2, 3, 60, 120, '');
        """,
    ),
    (
        "024_add_endpoint_bypass_method",
        """
        ALTER TABLE endpoints ADD COLUMN bypass_method VARCHAR(32) DEFAULT 'none' NOT NULL;
        """,
    ),
    (
        "025_create_maps_tables",
        """
        CREATE TABLE IF NOT EXISTS maps (
            id VARCHAR(36) PRIMARY KEY,
            user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(128) NOT NULL,
            description TEXT DEFAULT '' NOT NULL,
            tag VARCHAR(128) DEFAULT '' NOT NULL,
            is_public BOOLEAN DEFAULT 0 NOT NULL,
            is_active BOOLEAN DEFAULT 1 NOT NULL,
            version VARCHAR(32) DEFAULT '1.0' NOT NULL,
            author VARCHAR(128) DEFAULT '' NOT NULL,
            global_llm_instructions TEXT DEFAULT '' NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_maps_user_id ON maps (user_id);
        CREATE TABLE IF NOT EXISTS map_stages (
            id VARCHAR(36) PRIMARY KEY,
            map_id VARCHAR(36) NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
            stage_order INTEGER NOT NULL,
            name VARCHAR(128) NOT NULL,
            description TEXT DEFAULT '' NOT NULL,
            system_instructions TEXT DEFAULT '' NOT NULL,
            endpoint_id VARCHAR(36) REFERENCES endpoints(id) ON DELETE SET NULL,
            model_override VARCHAR(128) DEFAULT '' NOT NULL,
            driver_callable_turns INTEGER DEFAULT 0 NOT NULL,
            verification_enabled BOOLEAN DEFAULT 0 NOT NULL,
            verification_endpoint_id VARCHAR(36) REFERENCES endpoints(id) ON DELETE SET NULL,
            verification_model VARCHAR(128) DEFAULT '' NOT NULL,
            verification_max_retries INTEGER DEFAULT 2 NOT NULL,
            verification_instructions TEXT DEFAULT '' NOT NULL,
            output_mode VARCHAR(16) DEFAULT 'persist' NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_map_stages_map_id ON map_stages (map_id);
        CREATE TABLE IF NOT EXISTS map_stage_resources (
            id VARCHAR(36) PRIMARY KEY,
            map_stage_id VARCHAR(36) NOT NULL REFERENCES map_stages(id) ON DELETE CASCADE,
            resource_type VARCHAR(16) NOT NULL,
            resource_id VARCHAR(36) NOT NULL,
            position VARCHAR(32) DEFAULT 'pre_driver' NOT NULL,
            sticky BOOLEAN DEFAULT 0 NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_map_stage_resources_stage_id ON map_stage_resources (map_stage_id);
        """,
    ),
    (
        "026_add_max_map_stages",
        """
        ALTER TABLE admin_settings ADD COLUMN max_map_stages INTEGER DEFAULT 3 NOT NULL;
        """,
    ),
    (
        "027_add_default_map_id",
        """
        ALTER TABLE user_settings ADD COLUMN default_map_id VARCHAR(36) REFERENCES maps(id) ON DELETE SET NULL;
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
                        tolerable = [
                            "duplicate column name",
                            "no such column",
                            "already exists",
                            "no such table",
                            "object name already exists",
                        ]
                        if any(t in err_lower for t in tolerable):
                            logger.debug("Migration %s: skipping (already applied): %s", name, stmt[:80])
                        else:
                            raise
            await _mark_migration_applied(engine, name)
            logger.info("Migration applied: %s", name)
        except Exception:
            logger.exception("Migration failed: %s", name)
            raise
