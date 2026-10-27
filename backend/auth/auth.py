"""JWT token creation and verification, plus credential validation."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt


_BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")


def _require_env(name: str, *, min_length: int | None = None) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} environment variable is required.")
    value = value.strip()
    if min_length is not None and len(value) < min_length:
        raise RuntimeError(f"{name} must be at least {min_length} characters long.")
    return value


def _require_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")
    return value


def _is_supported_password_hash(password_hash: str) -> bool:
    return bool(_BCRYPT_HASH_RE.fullmatch(password_hash.strip()))


def _load_users() -> dict[str, str]:
    """
    Load login credentials from the environment.

    Preferred format:
      AUTH_USERS_JSON='{"user@example.com":"<bcrypt-hash>"}'

    Single-user fallback:
      AUTH_USER_EMAIL=user@example.com
      AUTH_USER_PASSWORD_HASH=<bcrypt-hash>
    """
    users_json = os.getenv("AUTH_USERS_JSON", "").strip()
    if users_json:
        try:
            parsed = json.loads(users_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AUTH_USERS_JSON must contain valid JSON.") from exc

        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError("AUTH_USERS_JSON must be a non-empty JSON object.")

        users: dict[str, str] = {}
        for email, password_hash in parsed.items():
            if not isinstance(email, str) or not isinstance(password_hash, str):
                raise RuntimeError("AUTH_USERS_JSON keys and values must be strings.")
            password_hash = password_hash.strip()
            if not _is_supported_password_hash(password_hash):
                raise RuntimeError(f"Unsupported password hash for {email!r}.")
            users[email.strip().lower()] = password_hash
        return users

    email = _require_env("AUTH_USER_EMAIL")
    password_hash = _require_env("AUTH_USER_PASSWORD_HASH")
    if not _is_supported_password_hash(password_hash):
        raise RuntimeError("AUTH_USER_PASSWORD_HASH must contain a supported password hash.")
    return {email.lower(): password_hash}


# Settings

SECRET_KEY = _require_env("SECRET_KEY", min_length=32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = _require_positive_int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 15)
REFRESH_TOKEN_EXPIRE_DAYS = _require_positive_int_env("REFRESH_TOKEN_EXPIRE_DAYS", 30)

_USERS = _load_users()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def authenticate_user(email: str, password: str) -> dict | None:
    """
    Returns user payload dict if credentials are valid, else None.
    """
    lookup = (email or "").strip().lower()
    hashed = _USERS.get(lookup)
    if not hashed:
        return None
    if not verify_password(password, hashed):
        return None
    return {"sub": lookup}


def _create_token(data: dict, *, token_type: str, expires_delta: timedelta) -> str:
    payload = data.copy()
    if not payload.get("sub"):
        raise ValueError("JWT payload requires a 'sub' claim.")

    now = datetime.now(timezone.utc)
    payload["iat"] = now
    payload["exp"] = now + expires_delta
    payload["type"] = token_type
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(data: dict) -> str:
    return _create_token(
        data,
        token_type="access",
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(data: dict) -> str:
    return _create_token(
        data,
        token_type="refresh",
        expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )


def verify_token(token: str, *, token_type: str = "access") -> dict:
    """
    Decodes and validates a JWT token.
    Returns the payload dict.
    Raises JWTError if invalid or expired.
    """
    payload = jwt.decode(
        token,
        SECRET_KEY,
        algorithms=[ALGORITHM],
        options={"verify_exp": True},
    )

    if not payload.get("sub"):
        raise JWTError("Missing subject claim.")
    if "exp" not in payload:
        raise JWTError("Missing expiration claim.")
    if payload.get("type") != token_type:
        raise JWTError("Invalid token type.")

    return payload
