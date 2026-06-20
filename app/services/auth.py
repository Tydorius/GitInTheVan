import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from app.config import settings

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24
API_KEY_PREFIX = "gitv_"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_access_token(user_id: str, username: str, is_admin: bool) -> str:
    expire = datetime.now(UTC) + timedelta(hours=JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_api_key() -> tuple[str, str]:
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(raw_key)
    return raw_key, key_hash


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
