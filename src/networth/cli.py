"""Command line: run the server, import/export Excel files, manage users, create a password hash."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Sequence

from .auth import ensure_configured, hash_password
from .config import Config
from .db import Store
from .excel import export_workbook, import_workbook
from .sheet import compute
from .users import UserError, Users


def _users() -> Users:
    config = Config.from_env()
    users = Users(config.data_dir)
    if not users.count() and (config.password or config.password_hash or config.auth_disabled):
        ensure_configured(config, users)
    return users


def _store(username: str | None) -> Store:
    """Data of ``username``; without a name the one of the only account or of NWT_USERNAME."""
    users = _users()
    accounts = users.all()
    if username is None:
        username = accounts[0].username if len(accounts) == 1 else Config.from_env().username
    user = users.by_name(username)
    if user is None:
        raise SystemExit(f"Benutzer {username!r} nicht gefunden (vorhanden: {', '.join(u.username for u in accounts)})")
    return users.store(user.id)


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("networth.app:create_app", factory=True, host=args.host, port=args.port,
                root_path=os.environ.get("NWT_ROOT_PATH", ""), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


def import_excel(args: argparse.Namespace) -> None:
    store = _store(args.user)
    result = import_workbook(args.file.read_bytes(), store.columns())
    store.replace_all(result.new_columns, result.entries, result.income)
    print(f"{len(result.entries)} Einträge importiert"
          + (f", {len(result.income)} Einkommens-Einträge" if result.income is not None else "")
          + (f", neue Spalten: {', '.join(c.name for c in result.new_columns)}" if result.new_columns else "")
          + (f", {result.fixed_values} Festwerte" if result.fixed_values else ""))


def export_excel(args: argparse.Namespace) -> None:
    store = _store(args.user)
    columns, entries = store.columns(), store.entries()
    args.file.write_bytes(export_workbook(columns, entries, compute(columns, entries), store.income()))
    print(f"Exportiert nach {args.file}")


def _ask_password() -> str:
    value = getpass.getpass("Passwort: ")
    if value != getpass.getpass("Wiederholen: "):
        raise SystemExit("Passwörter stimmen nicht überein")
    return value


def create_user(args: argparse.Namespace) -> None:
    try:
        user = _users().create(args.username, _ask_password(), is_admin=args.admin)
    except UserError as exc:
        raise SystemExit(str(exc))
    print(f"Benutzer {user.username!r} angelegt" + (" (Administrator)" if user.is_admin else ""))


def set_password(args: argparse.Namespace) -> None:
    users = _users()
    user = users.by_name(args.username)
    if user is None:
        raise SystemExit(f"Benutzer {args.username!r} nicht gefunden")
    try:
        users.set_password(user.id, _ask_password())
        if args.admin:
            users.set_admin(user.id, True)
    except UserError as exc:
        raise SystemExit(str(exc))
    print(f"Passwort von {user.username!r} geändert" + (", jetzt Administrator" if args.admin else ""))


def list_users(args: argparse.Namespace) -> None:
    for user in _users().all():
        print(f"{user.username:<32} {'Administrator' if user.is_admin else 'Benutzer':<14} angelegt {user.created[:10]}")


def password(args: argparse.Namespace) -> None:
    value = _ask_password()
    encoded = hash_password(value)
    print(encoded)
    print("In docker-compose/.env die $-Zeichen verdoppeln: NWT_PASSWORD_HASH=" + encoded.replace("$", "$$"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("networth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="Webserver starten")
    p.add_argument("--host", default=os.environ.get("NWT_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("NWT_PORT", "8000")))
    p.set_defaults(func=serve)

    p = sub.add_parser("import", help="Excel importieren (ersetzt alle Einträge)")
    p.add_argument("file", type=Path)
    p.add_argument("--user", help="Benutzer (Standard: der einzige Benutzer bzw. NWT_USERNAME)")
    p.set_defaults(func=import_excel)

    p = sub.add_parser("export", help="Excel exportieren")
    p.add_argument("file", type=Path)
    p.add_argument("--user", help="Benutzer (Standard: der einzige Benutzer bzw. NWT_USERNAME)")
    p.set_defaults(func=export_excel)

    p = sub.add_parser("create-user", help="Benutzer anlegen")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="als Administrator")
    p.set_defaults(func=create_user)

    p = sub.add_parser("set-password", help="Passwort eines Benutzers setzen (z. B. Admin-Passwort vergessen)")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="zusätzlich zum Administrator machen")
    p.set_defaults(func=set_password)

    p = sub.add_parser("users", help="Benutzer auflisten")
    p.set_defaults(func=list_users)

    p = sub.add_parser("hash-password", help="Passwort-Hash für NWT_PASSWORD_HASH erzeugen")
    p.set_defaults(func=password)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
