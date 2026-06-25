import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.models.endpoint import Endpoint
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.auth import hash_api_key

logger = logging.getLogger(__name__)


class RoutingResult:
    __slots__ = ("base_url", "api_key", "model", "user_id", "api_base_path", "bypass_method", "provider")

    def __init__(self, base_url: str, api_key: str, model: str, user_id: str, api_base_path: str = "", bypass_method: str = "none", provider: str = ""):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.user_id = user_id
        self.api_base_path = api_base_path
        self.bypass_method = bypass_method
        self.provider = provider


async def resolve_routing(bearer_token: str, db: AsyncSession) -> RoutingResult | None:
    key_hash = hash_api_key(bearer_token)

    api_key_row = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
    )
    api_key = api_key_row.scalar_one_or_none()

    if api_key is not None:
        user_result = await db.execute(select(User).where(User.id == api_key.user_id))
        user = user_result.scalar_one_or_none()
        if user is None or user.is_disabled:
            logger.warning("Routing blocked: api key %s...%s user invalid or disabled", key_hash[:8], key_hash[-4:])
            return None

        api_key.last_used_at = datetime.now(UTC)

        endpoint: Endpoint | None = None
        if api_key.endpoint_id:
            ep_result = await db.execute(
                select(Endpoint).where(
                    Endpoint.id == api_key.endpoint_id,
                    Endpoint.user_id == user.id,
                    Endpoint.enabled.is_(True),
                )
            )
            endpoint = ep_result.scalar_one_or_none()

        if endpoint is None:
            endpoint = await _resolve_default_endpoint(db, user.id)

        if endpoint is None:
            logger.warning("Routing failed: no enabled endpoint for user %s", user.username)
            return None

        logger.info(
            "Routing resolved via api_keys table: user=%s endpoint=%s (%s)",
            user.username, endpoint.name, endpoint.base_url,
        )
        return RoutingResult(
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
            model="",
            user_id=user.id,
            api_base_path=endpoint.api_base_path or "",
            bypass_method=endpoint.bypass_method or "none",
            provider=endpoint.provider or "",
        )

    result = await db.execute(select(User).where(User.gitv_api_key == key_hash))
    user = result.scalar_one_or_none()
    if user is None:
        logger.warning("Routing failed: no user for API key hash %s...%s", key_hash[:8], key_hash[-4:])
        return None

    if user.is_disabled:
        logger.warning("Routing blocked: user %s is disabled", user.username)
        return None

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    user_settings = settings_result.scalar_one_or_none()

    endpoint = None
    model_override: str | None = None

    if user_settings and user_settings.default_endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == user_settings.default_endpoint_id,
                Endpoint.user_id == user.id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()
        model_override = user_settings.default_model or None

    if endpoint is None:
        endpoint = await _resolve_default_endpoint(db, user.id)

    if endpoint is None:
        logger.warning("Routing failed: no enabled endpoint for user %s", user.username)
        return None

    effective_model = model_override or ""

    logger.info(
        "Routing resolved: user=%s endpoint=%s (%s)",
        user.username,
        endpoint.name,
        endpoint.base_url,
    )

    return RoutingResult(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        model=effective_model,
        user_id=user.id,
        api_base_path=endpoint.api_base_path or "",
        bypass_method=endpoint.bypass_method or "none",
        provider=endpoint.provider or "",
    )


async def _resolve_default_endpoint(db: AsyncSession, user_id: str) -> Endpoint | None:
    ep_result = await db.execute(
        select(Endpoint)
        .where(Endpoint.user_id == user_id, Endpoint.enabled.is_(True))
        .order_by(Endpoint.created_at)
        .limit(1)
    )
    return ep_result.scalar_one_or_none()
