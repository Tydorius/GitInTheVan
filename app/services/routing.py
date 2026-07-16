import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey
from app.models.endpoint import Endpoint
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.auth import hash_api_key

logger = logging.getLogger(__name__)


@dataclass
class FailoverEndpoint:
    """A single candidate in a failover chain. Each candidate is fully
    self-contained — different endpoints may carry different models, API keys,
    providers, or bypass methods (e.g. a free OpenRouter key failing over to a
    paid endpoint with a different model)."""

    base_url: str
    api_key: str = ""
    api_base_path: str = ""
    bypass_method: str = "none"
    provider: str = ""
    model: str = ""
    endpoint_id: str = ""
    endpoint_name: str = ""
    priority: int = 1


@dataclass
class RoutingResult:
    """Routing resolution for a single request. The scalar fields (base_url,
    api_key, etc.) hold the first candidate's values for backward compatibility;
    `failover_chain` holds the full ordered list (including the first candidate)
    for the proxy failover loop. A single-endpoint setup produces a chain of
    length 1 — byte-identical behavior to pre-failover routing."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    user_id: str = ""
    api_base_path: str = ""
    bypass_method: str = "none"
    provider: str = ""
    endpoint_id: str = ""
    failover_chain: list[FailoverEndpoint] = field(default_factory=list)


def _routing_result_from_primary(
    primary: Endpoint,
    user_id: str,
    chain: list[FailoverEndpoint],
    model: str = "",
) -> RoutingResult:
    """Build a RoutingResult from the primary endpoint plus a pre-built chain.
    The scalar fields mirror `primary` so existing callers that read them
    directly are unaffected."""
    return RoutingResult(
        base_url=primary.base_url,
        api_key=primary.api_key,
        model=model,
        user_id=user_id,
        api_base_path=primary.api_base_path or "",
        bypass_method=primary.bypass_method or "none",
        provider=primary.provider or "",
        endpoint_id=primary.id,
        failover_chain=chain,
    )


async def _build_failover_chain(
    db: AsyncSession, primary: Endpoint, user_id: str
) -> list[FailoverEndpoint]:
    """Build the ordered failover chain for a primary endpoint.

    The primary is first; then every other enabled endpoint for the user that
    shares the primary's role_tag, ordered by priority ascending then created_at.
    The primary's own tag-mates are included so a tagged-primary setup (e.g.
    two endpoints both tagged 'driver') failover correctly. The DB session is
    used only here — callers close it before any upstream call."""
    tag = primary.role_tag or "default"
    result = await db.execute(
        select(Endpoint)
        .where(
            Endpoint.user_id == user_id,
            Endpoint.enabled.is_(True),
            Endpoint.role_tag == tag,
        )
        .order_by(Endpoint.priority, Endpoint.created_at)
    )
    tagged = list(result.scalars().all())

    chain: list[FailoverEndpoint] = []
    seen_ids: set[str] = set()

    # Primary first (preserves the exact endpoint routing resolved to).
    chain.append(_endpoint_to_candidate(primary))
    seen_ids.add(primary.id)

    # Then tag-mates not already included.
    for ep in tagged:
        if ep.id not in seen_ids:
            chain.append(_endpoint_to_candidate(ep))
            seen_ids.add(ep.id)

    return chain


def _endpoint_to_candidate(ep: Endpoint, model: str = "") -> FailoverEndpoint:
    return FailoverEndpoint(
        base_url=ep.base_url,
        api_key=ep.api_key,
        api_base_path=ep.api_base_path or "",
        bypass_method=ep.bypass_method or "none",
        provider=ep.provider or "",
        model=model or ep.default_model or "",
        endpoint_id=ep.id,
        endpoint_name=ep.name,
        priority=ep.priority,
    )


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

        chain = await _build_failover_chain(db, endpoint, user.id)

        logger.info(
            "Routing resolved via api_keys table: user=%s endpoint=%s (%s) chain=%d",
            user.username, endpoint.name, endpoint.base_url, len(chain),
        )
        return _routing_result_from_primary(endpoint, user.id, chain)

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
        model_override = (endpoint.default_model if endpoint else None) or None

    if endpoint is None:
        endpoint = await _resolve_default_endpoint(db, user.id)

    if endpoint is None:
        logger.warning("Routing failed: no enabled endpoint for user %s", user.username)
        return None

    chain = await _build_failover_chain(db, endpoint, user.id)
    effective_model = model_override or ""

    logger.info(
        "Routing resolved: user=%s endpoint=%s (%s) chain=%d",
        user.username,
        endpoint.name,
        endpoint.base_url,
        len(chain),
    )

    return _routing_result_from_primary(endpoint, user.id, chain, model=effective_model)


async def resolve_endpoints_by_tag(
    db: AsyncSession, user_id: str, tag: str
) -> list[Endpoint]:
    """Resolve all enabled endpoints matching a role tag, ordered by priority.

    Used by the map pipeline for tag-based stage endpoint resolution. The
    session is closed by the caller before any upstream call."""
    result = await db.execute(
        select(Endpoint)
        .where(
            Endpoint.user_id == user_id,
            Endpoint.enabled.is_(True),
            Endpoint.role_tag == tag,
        )
        .order_by(Endpoint.priority, Endpoint.created_at)
    )
    return list(result.scalars().all())


async def _resolve_default_endpoint(db: AsyncSession, user_id: str) -> Endpoint | None:
    ep_result = await db.execute(
        select(Endpoint)
        .where(Endpoint.user_id == user_id, Endpoint.enabled.is_(True))
        .order_by(Endpoint.priority, Endpoint.created_at)
        .limit(1)
    )
    return ep_result.scalar_one_or_none()
