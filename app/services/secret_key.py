"""JWT signing-key bootstrap.

`Settings.secret_key` ships with the placeholder value that also appears in
`.env.example` and therefore in the public repository. An install that never
changed it signs every session token with a key an attacker can simply read
off GitHub. `require_admin` re-checks the database rather than trusting the
token's `is_admin` claim, and user ids are UUID4, so this is not by itself an
instant takeover -- but it makes every token forgeable to anyone who learns a
single user id, with an expiry the attacker chooses, and it survives a
password change because nothing else binds a token to a session.

Rather than fail startup (which would strand existing installs mid-upgrade),
a real key is generated on first boot and persisted under `data/`, which is
the mounted volume in the Docker images and is excluded from release zips, so
it survives both container recreation and in-app updates.
"""

from __future__ import annotations

import logging
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# The value in config.py's default and in .env.example.
PLACEHOLDER_SECRET = "change-me-in-production"

SECRET_KEY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "secret_key"


def _is_placeholder(value: str) -> bool:
    return not value.strip() or value.strip() == PLACEHOLDER_SECRET


def ensure_secret_key() -> str:
    """Give this process a non-public JWT signing key.

    Returns one of "configured", "loaded", "generated", or "ephemeral".
    An explicitly configured key always wins -- this never overrides one.
    """
    from app.config import settings

    if not _is_placeholder(settings.secret_key):
        return "configured"

    if SECRET_KEY_PATH.exists():
        try:
            stored = SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning("Could not read %s: %s", SECRET_KEY_PATH, e)
            stored = ""
        if stored and not _is_placeholder(stored):
            settings.secret_key = stored
            return "loaded"

    generated = secrets.token_urlsafe(48)
    settings.secret_key = generated

    try:
        SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_PATH.write_text(generated + "\n", encoding="utf-8")
        try:
            # Owner-only. No-op on Windows, which ignores these bits.
            SECRET_KEY_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    except OSError as e:
        logger.critical(
            "GITV_SECRET_KEY is unset or still the shipped placeholder, and a "
            "generated replacement could not be written to %s (%s). Using a "
            "key that lives only for this process -- everyone will be signed "
            "out on every restart. Set GITV_SECRET_KEY in .env to fix this.",
            SECRET_KEY_PATH, e,
        )
        return "ephemeral"

    logger.warning(
        "GITV_SECRET_KEY was unset or still the shipped placeholder "
        "('%s'), which is public in the source repository -- session tokens "
        "signed with it are forgeable. A random key has been generated and "
        "saved to %s. Existing sessions are now invalid; everyone must log in "
        "again, once. To manage the key yourself, set GITV_SECRET_KEY in .env "
        "and delete that file.",
        PLACEHOLDER_SECRET, SECRET_KEY_PATH,
    )
    return "generated"
