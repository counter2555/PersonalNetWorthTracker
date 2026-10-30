"""SQLite storage: column configuration (accounts, metrics, text fields), entries (one per snapshot date), income
and settings.

Values are stored per cell (entry x column), so columns can be added, renamed and removed without schema changes.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from .columns import Column
from .structure import (AUTOMATIC, DEFAULT_TAX_RATE, KIND_OF, LAYOUTS, TOP_ACCOUNTS, blockers, build,
                        default_columns, migrate, rename, section, spec_of, without)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS columns (
    id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    formula TEXT NOT NULL DEFAULT '',
    number_format TEXT NOT NULL DEFAULT 'currency',
    width REAL NOT NULL DEFAULT 12,
    asset_group TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    net_into TEXT NOT NULL DEFAULT '',
    highlight TEXT NOT NULL DEFAULT '',
    spec TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, date TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS entries_date ON entries (date);
CREATE TABLE IF NOT EXISTS cells (
    entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
    column_id INTEGER NOT NULL REFERENCES columns (id) ON DELETE CASCADE,
    value REAL,
    text TEXT,
    expr TEXT NOT NULL DEFAULT '',     -- input typed as an expression, e.g. "6604.12+895.2"
    override INTEGER NOT NULL DEFAULT 0, -- formula column: fixed value instead of the formula (from an import)
    PRIMARY KEY (entry_id, column_id)
);
CREATE TABLE IF NOT EXISTS income (id INTEGER PRIMARY KEY, date TEXT NOT NULL, amount REAL NOT NULL);
"""

COLUMN_FIELDS = ["position", "name", "kind", "formula", "number_format", "width", "asset_group", "role", "net_into",
                 "highlight", "spec"]
STRUCTURE_VERSION = "1"  # columns have a spec (accounts, metrics, text fields)

DEFAULT_SETTINGS = {
    "target": "1000000",
    "show_distribution": "1",
    "tax_rate": f"{DEFAULT_TAX_RATE:.10g}",
}


@dataclass
class Cell:
    value: float | None = None
    text: str | None = None
    expr: str = ""
    override: bool = False


@dataclass
class Entry:
    date: dt.date
    cells: dict[int, Cell] = field(default_factory=dict)  # column id -> cell
    id: int | None = None


@dataclass
class Income:
    date: dt.date
    amount: float
    id: int | None = None


class Store:
    def __init__(self, path: str | Path, layout: str = "full"):
        """``layout``: column layout (see ``structure.LAYOUTS``) of a new database; an existing one keeps its columns."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # serialize writes from the thread pool
        with self.tx() as con:
            con.executescript(SCHEMA)
            for key, value in DEFAULT_SETTINGS.items():
                con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value))
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('group_order', ?)",
                        (", ".join(LAYOUTS[layout][2]),))
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0')")
            if "spec" not in {r["name"] for r in con.execute("PRAGMA table_info(columns)")}:
                con.execute("ALTER TABLE columns ADD COLUMN spec TEXT NOT NULL DEFAULT ''")
            version = con.execute("SELECT value FROM meta WHERE key = 'structure_version'").fetchone()
            if con.execute("SELECT COUNT(*) FROM columns").fetchone()[0] == 0:
                self._write_structure(con, default_columns(layout), LAYOUTS[layout][2])
            elif version is None:
                self._migrate(con)
            con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('structure_version', ?)",
                        (STRUCTURE_VERSION,))

    def _migrate(self, con: sqlite3.Connection) -> None:
        """Columns from before accounts and metrics get their spec (see :func:`structure.migrate`)."""
        rate = float(con.execute("SELECT value FROM meta WHERE key = 'tax_rate'").fetchone()[0])
        result = migrate(self.columns(con), rate)
        self._write_structure(con, result.columns, result.groups, result.tax_rate)
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('structure_notes', ?)",
                    ("\n".join(result.notes),))

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Connection in a transaction: commits on success, rolls back on errors."""
        with self._lock:
            con = sqlite3.connect(self.path, timeout=30)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            try:
                with con:
                    yield con
            finally:
                con.close()

    @staticmethod
    def _bump(con: sqlite3.Connection) -> None:
        con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")

    def revision(self) -> int:
        with self.tx() as con:
            return int(con.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])

    # -- settings -----------------------------------------------------------

    def settings(self) -> dict[str, str]:
        with self.tx() as con:
            return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}

    def set_settings(self, values: dict[str, str]) -> None:
        with self.tx() as con:
            for key, value in values.items():
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
            self._bump(con)

    def target(self) -> float:
        return float(self.settings()["target"])

    def group_order(self) -> list[str]:
        """Groups of the accounts: order of the entry form and the Excel, bottom to top in the wealth chart."""
        return [g.strip() for g in self.settings()["group_order"].split(",") if g.strip()]

    def tax_rate(self) -> float:
        return float(self.settings()["tax_rate"])

    def structure_notes(self) -> list[str]:
        """Hints from the migration to accounts and metrics, until the user dismisses them."""
        return [n for n in self.settings().get("structure_notes", "").split("\n") if n]

    def dismiss_structure_notes(self) -> None:
        self.set_settings({"structure_notes": ""})

    # -- columns ------------------------------------------------------------

    @staticmethod
    def _column(row: sqlite3.Row) -> Column:
        return Column(id=row["id"], **{f: row[f] for f in COLUMN_FIELDS})

    def columns(self, con: sqlite3.Connection | None = None) -> list[Column]:
        if con is None:
            with self.tx() as con:
                return self.columns(con)
        return [self._column(r) for r in con.execute("SELECT * FROM columns ORDER BY position, id")]

    @staticmethod
    def _insert_column(con: sqlite3.Connection, col: Column) -> int:
        cur = con.execute(f"INSERT INTO columns ({', '.join(COLUMN_FIELDS)}) VALUES ({', '.join('?' * len(COLUMN_FIELDS))})",
                          [getattr(col, f) for f in COLUMN_FIELDS])
        return int(cur.lastrowid)

    def _structure(self, con: sqlite3.Connection) -> tuple[list[Column], list[str]]:
        groups = con.execute("SELECT value FROM meta WHERE key = 'group_order'").fetchone()[0]
        return self.columns(con), [g.strip() for g in groups.split(",") if g.strip()]

    def _write_structure(self, con: sqlite3.Connection, columns: list[Column], groups: list[str],
                         tax_rate: float | None = None) -> list[Column]:
        """Build ``columns`` (see :func:`structure.build`) and store them with the groups. Stored columns missing
        from the list are deleted with their values. Returns the stored columns."""
        if tax_rate is None:
            tax_rate = float(con.execute("SELECT value FROM meta WHERE key = 'tax_rate'").fetchone()[0])
        columns, groups = build(columns, groups, tax_rate)
        kept = {c.id for c in columns if c.id is not None}
        for row in con.execute("SELECT id FROM columns").fetchall():
            if row["id"] not in kept:
                con.execute("DELETE FROM columns WHERE id = ?", (row["id"],))
        con.execute("UPDATE columns SET name = '~' || id")  # free the names (UNIQUE) for renames
        stored = []
        for c in columns:
            if c.id is None:
                c = replace(c, id=self._insert_column(con, c))
            else:
                con.execute(f"UPDATE columns SET {', '.join(f'{f} = ?' for f in COLUMN_FIELDS)} WHERE id = ?",
                            [*(getattr(c, f) for f in COLUMN_FIELDS), c.id])
            stored.append(c)
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('group_order', ?)", (", ".join(groups),))
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('tax_rate', ?)", (f"{tax_rate:.10g}",))
        self._bump(con)
        return stored

    def save_column(self, col: Column, tax_rate: float | None = None) -> Column:
        """Insert (``col.id is None``) or update a column. Renames are applied to all formulas referencing it.

        ``tax_rate`` changes the capital gains tax rate in the same step (edited with the depot)."""
        with self.tx() as con:
            columns, groups = self._structure(con)
            if col.id is None:
                col = replace(col, position=max((c.position for c in columns), default=0) + 1)
                columns.append(col)
            else:
                old = next(c for c in columns if c.id == col.id)
                old_spec, new_spec = spec_of(old), spec_of(col)
                if KIND_OF[old_spec] != KIND_OF[new_spec]:
                    raise ValueError("Aus einem Konto wird keine Kennzahl und umgekehrt – bitte neu anlegen")
                if old_spec == "depot" and new_spec != "depot":
                    raise ValueError("Ein Depot bleibt ein Depot – zum Entfernen samt Gewinn-Werten löschen")
                if (old_spec in AUTOMATIC | {"comment"}) != (new_spec in AUTOMATIC | {"comment"}) \
                        or old_spec in AUTOMATIC and new_spec != old_spec:
                    raise ValueError(f"Die Art von {old.name!r} kann nicht geändert werden")
                col = replace(col, position=old.position)
                if old.name != col.name:
                    columns = rename(columns, old.name, col.name)
                    col = rename([col], old.name, col.name)[0]
                columns = [col if c.id == col.id else c for c in columns]
            stored = self._write_structure(con, columns, groups, tax_rate)
            return next(c for c in stored if c.name == col.name)

    def delete_column(self, column_id: int) -> list[str]:
        """Delete a column with its values; a depot takes its gain, tax and depot metrics along.

        Returns the names of all deleted columns."""
        with self.tx() as con:
            columns, groups = self._structure(con)
            col = next(c for c in columns if c.id == column_id)
            if spec_of(col) in AUTOMATIC | {"comment"}:
                raise ValueError(f"{col.name!r} gehört fest dazu und kann nicht gelöscht werden")
            rest = without(columns, col.name)
            remaining = {c.name for c in rest}
            deleted = [c.name for c in columns if c.name not in remaining]
            users = list(dict.fromkeys(n for name in deleted for n in blockers(columns, name) if n in remaining))
            if users:
                raise ValueError(f"{col.name!r} wird noch verwendet von: {', '.join(users)}")
            self._write_structure(con, rest, groups)
            return deleted

    def move_column(self, column_id: int, offset: int) -> None:
        """Move a column up or down within its section (e.g. among the accounts of its group)."""
        with self.tx() as con:
            columns, groups = self._structure(con)
            col = next(c for c in columns if c.id == column_id)
            peers = [c for c in columns if section(c) == section(col)]
            index = peers.index(col)
            other = peers[min(max(index + offset, 0), len(peers) - 1)]
            positions = {col.id: other.position, other.id: col.position}
            self._write_structure(con, [replace(c, position=positions.get(c.id, c.position)) for c in columns],
                                  groups)

    def order_accounts(self, order: dict[str, list[int]]) -> None:
        """Groups in this order with these accounts (``group -> account ids``, e.g. after drag and drop)."""
        with self.tx() as con:
            columns, groups = self._structure(con)
            placed = {cid: (group, i) for group, ids in order.items() for i, cid in enumerate(ids)}
            columns = [replace(c, asset_group=placed[c.id][0], position=placed[c.id][1])
                       if c.id in placed and spec_of(c) in TOP_ACCOUNTS else c for c in columns]
            self._write_structure(con, columns, list(order) + [g for g in groups if g not in order])

    # -- groups -------------------------------------------------------------

    @staticmethod
    def _group_name(name: str) -> str:
        name = name.strip()
        if not name or "," in name:
            raise ValueError("Bitte einen Gruppennamen ohne Komma angeben")
        return name

    def add_group(self, name: str) -> None:
        name = self._group_name(name)
        with self.tx() as con:
            columns, groups = self._structure(con)
            if name in groups:
                raise ValueError(f"Die Gruppe {name!r} gibt es schon")
            self._write_structure(con, columns, groups + [name])

    def rename_group(self, old: str, new: str) -> None:
        new = self._group_name(new)
        with self.tx() as con:
            columns, groups = self._structure(con)
            if new != old and new in groups:
                raise ValueError(f"Die Gruppe {new!r} gibt es schon")
            columns = [replace(c, asset_group=new) if c.asset_group == old else c for c in columns]
            self._write_structure(con, columns, [new if g == old else g for g in groups])

    def move_group(self, name: str, offset: int) -> None:
        with self.tx() as con:
            columns, groups = self._structure(con)
            index = groups.index(name)
            groups.insert(min(max(index + offset, 0), len(groups) - 1), groups.pop(index))
            self._write_structure(con, columns, groups)

    def delete_group(self, name: str) -> None:
        with self.tx() as con:
            columns, groups = self._structure(con)
            if any(c.asset_group == name and spec_of(c) in TOP_ACCOUNTS for c in columns):
                raise ValueError(f"In der Gruppe {name!r} sind noch Konten")
            self._write_structure(con, columns, [g for g in groups if g != name])

    def reset_columns(self, layout: str) -> list[str]:
        """Replace the column configuration by a default layout; returns the names of the removed columns.

        Columns with the same name (and type) as in the layout keep their values, the others are deleted with them.
        """
        new = default_columns(layout)
        with self.tx() as con:
            old = {c.name: c for c in self.columns(con)}
            columns = [replace(c, id=old[c.name].id) if c.name in old and old[c.name].kind == c.kind else c
                       for c in new]
            self._write_structure(con, columns, LAYOUTS[layout][2], DEFAULT_TAX_RATE)
            con.execute("UPDATE meta SET value = '' WHERE key = 'structure_notes'")
            return [name for name in old if name not in {c.name for c in new}]

    # -- entries ------------------------------------------------------------

    def entries(self) -> list[Entry]:
        """All entries sorted by date (entries of the same day in the order they were added)."""
        with self.tx() as con:
            entries = {r["id"]: Entry(id=r["id"], date=dt.date.fromisoformat(r["date"]))
                       for r in con.execute("SELECT id, date FROM entries ORDER BY date, id")}
            for r in con.execute("SELECT * FROM cells"):
                entries[r["entry_id"]].cells[r["column_id"]] = Cell(r["value"], r["text"], r["expr"], bool(r["override"]))
        return list(entries.values())

    def entry(self, entry_id: int) -> Entry | None:
        return next((e for e in self.entries() if e.id == entry_id), None)

    @staticmethod
    def _write_cells(con: sqlite3.Connection, entry_id: int, cells: dict[int, Cell]) -> None:
        con.execute("DELETE FROM cells WHERE entry_id = ?", (entry_id,))
        con.executemany(
            "INSERT INTO cells (entry_id, column_id, value, text, expr, override) VALUES (?, ?, ?, ?, ?, ?)",
            [(entry_id, cid, c.value, c.text, c.expr, int(c.override)) for cid, c in cells.items()
             if c.value is not None or c.text or c.override])

    def save_entry(self, entry: Entry) -> int:
        with self.tx() as con:
            if entry.id is None:
                entry_id = int(con.execute("INSERT INTO entries (date) VALUES (?)",
                                           (entry.date.isoformat(),)).lastrowid)
            else:
                entry_id = entry.id
                con.execute("UPDATE entries SET date = ? WHERE id = ?", (entry.date.isoformat(), entry_id))
            self._write_cells(con, entry_id, entry.cells)
            self._bump(con)
            return entry_id

    def delete_entry(self, entry_id: int) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            self._bump(con)

    # -- income -------------------------------------------------------------

    def income(self) -> list[Income]:
        with self.tx() as con:
            return [Income(dt.date.fromisoformat(r["date"]), r["amount"], r["id"])
                    for r in con.execute("SELECT * FROM income ORDER BY date, id")]

    def save_income(self, item: Income) -> None:
        with self.tx() as con:
            if item.id is None:
                con.execute("INSERT INTO income (date, amount) VALUES (?, ?)", (item.date.isoformat(), item.amount))
            else:
                con.execute("UPDATE income SET date = ?, amount = ? WHERE id = ?",
                            (item.date.isoformat(), item.amount, item.id))
            self._bump(con)

    def delete_income(self, income_id: int) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM income WHERE id = ?", (income_id,))
            self._bump(con)

    # -- import -------------------------------------------------------------

    def replace_all(self, new_columns: list[Column], entries: list[Entry], income: list[Income] | None) -> None:
        """Replace all entries (and the income if given) in one transaction.

        ``new_columns`` are added to the configuration first (as other values or notes); cells of ``entries`` may
        refer to them by negative ids (-1 = first new column, ...).
        """
        with self.tx() as con:
            columns, groups = self._structure(con)
            start = max((c.position for c in columns), default=0)
            added = [replace(c, position=start + i, id=None) for i, c in enumerate(new_columns, start=1)]
            stored = {c.name: c.id for c in self._write_structure(con, columns + added, groups)}
            ids = {-i: stored[c.name] for i, c in enumerate(added, start=1)}
            con.execute("DELETE FROM entries")
            for entry in entries:
                entry_id = int(con.execute("INSERT INTO entries (date) VALUES (?)",
                                           (entry.date.isoformat(),)).lastrowid)
                self._write_cells(con, entry_id, {ids.get(cid, cid): c for cid, c in entry.cells.items()})
            if income is not None:
                con.execute("DELETE FROM income")
                con.executemany("INSERT INTO income (date, amount) VALUES (?, ?)",
                                [(i.date.isoformat(), i.amount) for i in income])
            self._bump(con)
