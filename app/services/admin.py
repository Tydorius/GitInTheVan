from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.admin_settings import AdminSettings

logger = logging.getLogger(__name__)

_SETTINGS_ID = 1


async def get_admin_settings() -> AdminSettings:
    """Get or create the singleton admin settings row."""
    async with async_session() as db:
        result = await db.execute(select(AdminSettings).where(AdminSettings.id == _SETTINGS_ID))
        settings = result.scalar_one_or_none()
        if settings is None:
            settings = AdminSettings(id=_SETTINGS_ID)
            db.add(settings)
            await db.commit()
            await db.refresh(settings)
        return settings


async def get_caps() -> dict[str, int | bool]:
    """Get the effective global caps."""
    s = await get_admin_settings()
    return {
        "max_driver_callable_turns": s.max_driver_callable_turns,
        "max_verification_retries": s.max_verification_retries,
        "max_map_stages": s.max_map_stages,
        "rate_limit_proxy_per_min": s.rate_limit_proxy_per_min,
        "rate_limit_api_per_min": s.rate_limit_api_per_min,
        "max_saved_debug_runs": s.max_saved_debug_runs,
        "max_debug_exchange_kb": s.max_debug_exchange_kb,
        "assistant_enabled": s.assistant_enabled,
        "assistant_admin_reads_enabled": s.assistant_admin_reads_enabled,
        "assistant_packs_enabled": s.assistant_packs_enabled,
        "max_assistant_tool_calls_per_turn": s.max_assistant_tool_calls_per_turn,
        "max_assistant_conversations": s.max_assistant_conversations,
        "max_assistant_tool_result_kb": s.max_assistant_tool_result_kb,
        "passenger_calling_enabled": s.passenger_calling_enabled,
        "max_passenger_calls_per_request": s.max_passenger_calls_per_request,
        "passenger_timeout_seconds": s.passenger_timeout_seconds,
    }


async def get_url_blocklist() -> list[str]:
    """Get the admin-configured URL blocklist as a list of domains."""
    s = await get_admin_settings()
    return [d.strip() for d in s.url_blocklist.split(",") if d.strip()]


async def update_admin_settings(updates: dict[str, Any]) -> AdminSettings:
    """Update admin settings fields."""
    async with async_session() as db:
        result = await db.execute(select(AdminSettings).where(AdminSettings.id == _SETTINGS_ID))
        settings = result.scalar_one_or_none()
        if settings is None:
            settings = AdminSettings(id=_SETTINGS_ID)
            db.add(settings)

        for key, value in updates.items():
            if hasattr(settings, key) and value is not None:
                setattr(settings, key, value)

        await db.commit()
        await db.refresh(settings)

    apply_admin_runtime_overrides(settings)
    return settings


def apply_admin_runtime_overrides(s: AdminSettings) -> None:
    """Push admin-owned values into the running process.

    The rate limiter reads ``app.config.settings`` on every request, so an
    admin_settings row alone never changed actual limiting -- the Admin UI's
    rate-limit fields were a silent no-op until this ran. Applied on write
    and again at startup, a changed value is durable while the env value
    stays the seed.
    """
    from app.config import settings as runtime_settings
    from app.services import deno_runner
    from app.services.log_manager import apply_log_timezone

    runtime_settings.rate_limit_proxy_per_min = s.rate_limit_proxy_per_min
    runtime_settings.rate_limit_api_per_min = s.rate_limit_api_per_min
    if s.runtime_log_level:
        apply_runtime_log_level(s.runtime_log_level)
    apply_log_timezone(s.log_timezone)

    pool = getattr(deno_runner, "deno_pool", None)
    if pool is not None:
        pool.configure(
            min_idle=s.deno_pool_min_idle,
            max_idle=s.deno_pool_max_idle,
            idle_ttl_seconds=s.deno_pool_idle_ttl_seconds,
        )


async def apply_admin_overrides_at_startup() -> None:
    """Re-apply admin-owned runtime overrides after a restart.

    Without this, an admin's rate-limit change reverted to the env seed on
    every restart -- persisted in the table, read back by the UI, and never
    obeyed by the limiter.
    """
    try:
        s = await get_admin_settings()
    except Exception:
        logger.exception("Could not load admin settings for runtime overrides")
        return
    apply_admin_runtime_overrides(s)


def apply_runtime_log_level(level: str) -> None:
    """Change the logging level at runtime without restart."""
    if not level:
        return
    numeric_level = getattr(logging, level.upper(), None)
    if numeric_level is None:
        return
    logging.getLogger().setLevel(numeric_level)
    for handler in logging.getLogger().handlers:
        handler.setLevel(numeric_level)
    logger.info("Runtime log level changed to: %s", level.upper())


async def get_effective_log_level() -> str:
    """Get the current effective log level (runtime override or startup config)."""
    s = await get_admin_settings()
    if s.runtime_log_level:
        return s.runtime_log_level.upper()
    from app.config import settings as app_config
    return app_config.log_level.upper()
