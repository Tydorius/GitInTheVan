"""The JWT signing key must never stay at the value published in the repo."""

import pytest

from app.config import settings
from app.services import secret_key as sk
from app.services.secret_key import PLACEHOLDER_SECRET, ensure_secret_key


@pytest.fixture
def key_store(tmp_path, monkeypatch):
    """Redirect the persisted key to a temp path and isolate settings."""
    path = tmp_path / "secret_key"
    monkeypatch.setattr(sk, "SECRET_KEY_PATH", path)
    monkeypatch.setattr(settings, "secret_key", PLACEHOLDER_SECRET)
    return path


def test_explicit_key_is_never_overridden(key_store, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "an-operator-chosen-key-value")

    assert ensure_secret_key() == "configured"
    assert settings.secret_key == "an-operator-chosen-key-value"
    assert not key_store.exists(), "Must not write a key when one is configured"


def test_placeholder_is_replaced_and_persisted(key_store):
    result = ensure_secret_key()

    assert result == "generated"
    assert settings.secret_key != PLACEHOLDER_SECRET
    assert len(settings.secret_key) >= 32
    assert key_store.exists()
    assert key_store.read_text(encoding="utf-8").strip() == settings.secret_key


def test_empty_key_is_treated_as_unset(key_store, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "   ")

    assert ensure_secret_key() == "generated"
    assert settings.secret_key.strip() != ""


def test_persisted_key_is_reused_across_restarts(key_store):
    ensure_secret_key()
    first = settings.secret_key

    # Simulate a restart: process state resets, the file does not.
    settings.secret_key = PLACEHOLDER_SECRET
    result = ensure_secret_key()

    assert result == "loaded"
    assert settings.secret_key == first, "Restart must not sign users out again"


def test_generated_keys_are_unique(key_store, tmp_path, monkeypatch):
    ensure_secret_key()
    first = settings.secret_key

    monkeypatch.setattr(sk, "SECRET_KEY_PATH", tmp_path / "other" / "secret_key")
    monkeypatch.setattr(settings, "secret_key", PLACEHOLDER_SECRET)
    ensure_secret_key()

    assert settings.secret_key != first


def test_unwritable_store_falls_back_to_an_ephemeral_key(key_store, monkeypatch):
    """A read-only volume must not leave the placeholder in place."""
    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(sk.Path, "mkdir", boom)

    result = ensure_secret_key()

    assert result == "ephemeral"
    assert settings.secret_key != PLACEHOLDER_SECRET, (
        "Falling back must still abandon the public placeholder"
    )


def test_stored_placeholder_is_not_trusted(key_store):
    """A file containing the placeholder is as bad as no file."""
    key_store.write_text(PLACEHOLDER_SECRET + "\n", encoding="utf-8")

    assert ensure_secret_key() == "generated"
    assert settings.secret_key != PLACEHOLDER_SECRET


def test_forged_token_is_rejected_once_a_real_key_is_set(key_store):
    """The concrete attack: a token signed with the public placeholder."""
    from jose import jwt

    from app.services.auth import JWT_ALGORITHM, decode_access_token

    forged = jwt.encode(
        {"sub": "any-user-id", "username": "x", "is_admin": True, "exp": 9999999999},
        PLACEHOLDER_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    assert decode_access_token(forged) is not None, "fixture check: forgeable before"

    ensure_secret_key()

    assert decode_access_token(forged) is None
