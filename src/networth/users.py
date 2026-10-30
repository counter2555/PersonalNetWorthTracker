"""User accounts: one SQLite file for the accounts (users.sqlite), one data store per user (users/<id>.sqlite).

Each user gets an own :class:`~networth.db.Store`, so entries, columns, income and settings are fully separated
and the store itself needs no notion of users.
"""
from __future__ import annotations

import datetime as dt
import functools
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .auth import hash_password, verify_password
from .db import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reuse ids: they name the data files
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_login TEXT
);
"""

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}")
MIN_PASSWORD_LENGTH = 8
UNUSABLE_PASSWORD = "!"  # never matches (see auth.verify_password)


class UserError(ValueError):
    """Invalid input or a forbidden change (e.g. removing the last admin); the message is shown to the user."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    password_hash: str
    is_admin: bool
    created: str
    last_login: str | None = None


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise UserError("Benutzername: 2–32 Zeichen, nur Buchstaben, Ziffern, Punkt, Binde- und Unterstrich")
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Passwort: mindestens {MIN_PASSWORD_LENGTH} Zeichen")


class Users:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "users.sqlite"
        (self.data_dir / "users").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stores: dict[int, Store] = {}
        with self.tx() as con:
            con.executescript(SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('registration', '1')")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            try:
                with con:
                    yield con
            finally:
                con.close()

    # -- settings -----------------------------------------------------------

    def registration_open(self) -> bool:
        with self.tx() as con:
            return con.execute("SELECT value FROM meta WHERE key = 'registration'").fetchone()[0] == "1"

    def set_registration_open(self, value: bool) -> None:
        with self.tx() as con:
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('registration', ?)", ("1" if value else "0",))

    # -- queries ------------------------------------------------------------

    def all(self) -> list[User]:
        with self.tx() as con:
            return [User(**r) for r in con.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")]

    def get(self, user_id: int) -> User | None:
        with self.tx() as con:
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(**row) if row else None

    def by_name(self, username: str) -> User | None:
        with self.tx() as con:
            row = con.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        return User(**row) if row else None

    def count(self) -> int:
        with self.tx() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.by_name(username)
        if user is None:
            verify_password(password, _dummy_hash())  # same timing as for an existing user
            return None
        return user if verify_password(password, user.password_hash) else None

    def touch_login(self, user_id: int) -> None:
        with self.tx() as con:
            con.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(), user_id))

    # -- changes ------------------------------------------------------------

    def create(self, username: str, password: str, is_admin: bool = False, *, password_hash: str | None = None) -> User:
        """New account; ``password_hash`` instead of ``password`` for an already hashed one (NWT_PASSWORD_HASH)."""
        username = validate_username(username)
        if password_hash is None:
            validate_password(password)
            password_hash = hash_password(password)
        with self.tx() as con:
            if con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise UserError(f"Benutzername „{username}“ ist bereits vergeben")
            cur = con.execute("INSERT INTO users (username, password_hash, is_admin, created) VALUES (?, ?, ?, ?)",
                              (username, password_hash, int(is_admin), _now()))
            user_id = int(cur.lastrowid)
        user = self.get(user_id)
        assert user is not None
        return user

    def set_password(self, user_id: int, password: str) -> None:
        validate_password(password)
        with self.tx() as con:
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))

    def set_admin(self, user_id: int, is_admin: bool) -> None:
        with self.tx() as con:
            if not is_admin:
                self._keep_an_admin(con, user_id)
            con.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))

    def delete(self, user_id: int) -> None:
        """Remove the account and all of its data."""
        with self.tx() as con:
            self._keep_an_admin(con, user_id)
            con.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._stores.pop(user_id, None)
            self.store_path(user_id).unlink(missing_ok=True)

    @staticmethod
    def _keep_an_admin(con: sqlite3.Connection, user_id: int) -> None:
        admins = [r[0] for r in con.execute("SELECT id FROM users WHERE is_admin = 1")]
        if admins == [user_id]:
            raise UserError("Der letzte Administrator kann nicht entfernt werden")

    # -- data ---------------------------------------------------------------

    def store_path(self, user_id: int) -> Path:
        return self.data_dir / "users" / f"{user_id}.sqlite"

    def store(self, user_id: int) -> Store:
        with self._lock:
            if user_id not in self._stores:
                self._stores[user_id] = Store(self.store_path(user_id), layout="basic")
            return self._stores[user_id]

    def bootstrap(self, username: str, password: str = "", password_hash: str = "") -> User | None:
        """Create the first administrator from the configuration when there are no accounts yet.

        The data of the former single-user installation (networth.sqlite) becomes the data of this account.
        """
        if self.count():
            return None
        if not password_hash:  # no password (NWT_AUTH_DISABLED): an account nobody can log in to
            password_hash = hash_password(password) if password else UNUSABLE_PASSWORD
        user = self.create(username, password, is_admin=True, password_hash=password_hash)
        legacy = self.data_dir / "networth.sqlite"
        if legacy.exists() and not self.store_path(user.id).exists():
            os.replace(legacy, self.store_path(user.id))
        else:  # the owner of the installation starts with the layout of their NetWorth.xlsx
            Store(self.store_path(user.id), layout="full")
        return user


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@functools.cache
def _dummy_hash() -> str:
    return hash_password("dummy-password-for-timing")
