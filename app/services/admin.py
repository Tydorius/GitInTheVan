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


async def get_caps() -> dict[str, int]:
    """Get the effective global caps."""
    s = await get_admin_settings()
    return {
        "max_driver_callable_turns": s.max_driver_callable_turns,
        "max_verification_retries": s.max_verification_retries,
        "rate_limit_proxy_per_min": s.rate_limit_proxy_per_min,
        "rate_limit_api_per_min": s.rate_limit_api_per_min,
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
        return settings


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
