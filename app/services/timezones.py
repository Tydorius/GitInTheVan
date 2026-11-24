from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_TIMEZONE_LENGTH = 64


def validate_timezone(tz: str | None) -> str:
    """Validate an IANA timezone identifier.

    Returns ``""`` when ``tz`` is ``None`` or blank (meaning default/inherit).
    Raises ``ValueError`` if ``tz`` exceeds 64 characters or is not a known
    IANA timezone.
    """
    cleaned = (tz or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_TIMEZONE_LENGTH:
        raise ValueError(f"Timezone name must be at most {MAX_TIMEZONE_LENGTH} characters")
    try:
        ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone '{cleaned}'") from exc
    return cleaned


def get_zoneinfo(tz: str | None) -> ZoneInfo | None:
    """Return a ``ZoneInfo`` for ``tz`` if it is a valid non-empty IANA name, else ``None``."""
    cleaned = (tz or "").strip()
    if not cleaned or len(cleaned) > MAX_TIMEZONE_LENGTH:
        return None
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def resolve_effective_timezone(user_tz: str | None, global_tz: str | None) -> str:
    """Resolve the effective timezone: user override -> global default -> ''."""
    return (user_tz or "").strip() or (global_tz or "").strip() or ""
