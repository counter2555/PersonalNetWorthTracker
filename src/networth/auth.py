"""Single-user authentication: login form with a signed session cookie, or HTTP Basic auth (for scripts)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from fastapi import Request

from .config import Config

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


def check_credentials(config: Config, username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), config.username.encode())
    if config.password_hash:
        password_ok = verify_password(password, config.password_hash)
    else:
        password_ok = bool(config.password) and hmac.compare_digest(password.encode(), config.password.encode())
    return user_ok and password_ok


def _basic_auth(config: Config, request: Request) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        username, _, password = base64.b64decode(value).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return check_credentials(config, username, password)


def is_authenticated(config: Config, request: Request) -> bool:
    if config.auth_disabled:
        return True
    if request.session.get("user") == config.username:
        return True
    return _basic_auth(config, request)


def ensure_configured(config: Config) -> None:
    if not config.auth_disabled and not (config.password or config.password_hash):
        raise SystemExit("Kein Passwort gesetzt: NWT_PASSWORD oder NWT_PASSWORD_HASH setzen "
                         "(oder NWT_AUTH_DISABLED=true, z. B. hinter einem Reverse-Proxy mit eigener Anmeldung).")
