"""Service configuration from environment variables (see .env.example)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    username: str  # first administrator, created on the first start (NWT_USERNAME)
    password: str  # its password in plain text (NWT_PASSWORD), only if no hash is given
    password_hash: str  # NWT_PASSWORD_HASH, created with `networth hash-password`
    secret_key: str
    auth_disabled: bool
    secure_cookies: bool
    session_days: int

    @staticmethod
    def from_env() -> "Config":
        data_dir = Path(os.environ.get("NWT_DATA_DIR", "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return Config(
            data_dir=data_dir,
            username=os.environ.get("NWT_USERNAME", "admin"),
            password=os.environ.get("NWT_PASSWORD", ""),
            password_hash=os.environ.get("NWT_PASSWORD_HASH", ""),
            secret_key=os.environ.get("NWT_SECRET_KEY") or _persistent_secret(data_dir),
            auth_disabled=_flag("NWT_AUTH_DISABLED"),
            secure_cookies=_flag("NWT_SECURE_COOKIES"),
            session_days=int(os.environ.get("NWT_SESSION_DAYS", "30")),
        )


def _persistent_secret(data_dir: Path) -> str:
    """Session signing key, generated once and kept in the data volume, so logins survive restarts."""
    path = data_dir / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip()
