"""Command line: run the server, import/export Excel files, create a password hash."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
from typing import Sequence

from .auth import hash_password
from .config import Config
from .db import Store
from .excel import export_workbook, import_workbook
from .sheet import compute


def _store() -> Store:
    return Store(Config.from_env().database)


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("networth.app:create_app", factory=True, host=args.host, port=args.port,
                root_path=os.environ.get("NWT_ROOT_PATH", ""), proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


def import_excel(args: argparse.Namespace) -> None:
    store = _store()
    result = import_workbook(args.file.read_bytes(), store.columns())
    store.replace_all(result.new_columns, result.entries, result.income)
    print(f"{len(result.entries)} Einträge importiert"
          + (f", {len(result.income)} Einkommens-Einträge" if result.income is not None else "")
          + (f", neue Spalten: {', '.join(c.name for c in result.new_columns)}" if result.new_columns else "")
          + (f", {result.fixed_values} Festwerte" if result.fixed_values else ""))


def export_excel(args: argparse.Namespace) -> None:
    store = _store()
    columns, entries = store.columns(), store.entries()
    args.file.write_bytes(export_workbook(columns, entries, compute(columns, entries), store.income()))
    print(f"Exportiert nach {args.file}")


def password(args: argparse.Namespace) -> None:
    value = getpass.getpass("Passwort: ")
    if value != getpass.getpass("Wiederholen: "):
        raise SystemExit("Passwörter stimmen nicht überein")
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
    p.set_defaults(func=import_excel)

    p = sub.add_parser("export", help="Excel exportieren")
    p.add_argument("file", type=Path)
    p.set_defaults(func=export_excel)

    p = sub.add_parser("hash-password", help="Passwort-Hash für NWT_PASSWORD_HASH erzeugen")
    p.set_defaults(func=password)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
