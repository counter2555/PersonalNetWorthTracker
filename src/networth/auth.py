"""Authentication: login form with a signed session cookie, or HTTP Basic auth (for scripts)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

from fastapi import Request

from .config import Config

if TYPE_CHECKING:
    from .users import User, Users

ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _basic_auth(users: Users, request: Request) -> User | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        username, _, password = base64.b64decode(value).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    return users.authenticate(username, password)


def current_user(config: Config, users: Users, request: Request) -> User | None:
    """The logged in user: from the session cookie, else HTTP Basic auth; the configured admin without login."""
    if config.auth_disabled:
        return users.by_name(config.username) or next((u for u in users.all() if u.is_admin), None)
    user_id = request.session.get("uid")
    if isinstance(user_id, int):
        user = users.get(user_id)  # a deleted account is logged out immediately
        if user is not None:
            return user
        request.session.clear()
    return _basic_auth(users, request)


def ensure_configured(config: Config, users: Users) -> None:
    """Create the first administrator from NWT_USERNAME/NWT_PASSWORD(_HASH) on the first start."""
    if users.count():
        return
    if not config.auth_disabled and not (config.password or config.password_hash):
        raise SystemExit("Noch kein Benutzer angelegt: für den ersten Administrator NWT_PASSWORD oder "
                         "NWT_PASSWORD_HASH setzen (oder NWT_AUTH_DISABLED=true, z. B. hinter einem Reverse-Proxy "
                         "mit eigener Anmeldung).")
    users.bootstrap(config.username, config.password, config.password_hash)
