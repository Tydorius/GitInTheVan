from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple in-memory sliding window rate limiter."""

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, limit_per_min: int) -> None:
        now = time.monotonic()
        window = 60.0

        self._requests[key] = [t for t in self._requests[key] if now - t < window]

        if len(self._requests[key]) >= limit_per_min:
            logger.warning("Rate limit exceeded for %s: %d/min", key[:16], limit_per_min)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please slow down.",
                headers={"Retry-After": "60"},
            )

        self._requests[key].append(now)

    def cleanup(self) -> None:
        now = time.monotonic()
        window = 120.0
        stale_keys = [k for k, times in self._requests.items() if not times or now - times[-1] > window]
        for k in stale_keys:
            del self._requests[k]


proxy_limiter = RateLimiter()
api_limiter = RateLimiter()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_proxy_rate_limit(request: Request) -> None:
    from app.config import settings

    if not settings.rate_limit_enabled:
        return

    auth = request.headers.get("authorization", "")
    ip = get_client_ip(request)
    key = f"proxy:{auth[:16]}:{ip}" if auth else f"proxy:{ip}"
    proxy_limiter.check(key, settings.rate_limit_proxy_per_min)


async def check_api_rate_limit(request: Request) -> None:
    from app.config import settings

    if not settings.rate_limit_enabled:
        return

    auth = request.headers.get("authorization", "")
    ip = get_client_ip(request)
    key = f"api:{auth[:16]}:{ip}" if auth else f"api:{ip}"
    api_limiter.check(key, settings.rate_limit_api_per_min)
