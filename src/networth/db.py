"""SQLite storage: column configuration, entries (one per snapshot date), income and settings.

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

from . import formula as fx
from .columns import DEFAULT_GROUP_ORDER, Column, default_columns, validate

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
    highlight TEXT NOT NULL DEFAULT ''
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
                 "highlight"]

DEFAULT_SETTINGS = {
    "target": "1000000",
    "group_order": ", ".join(DEFAULT_GROUP_ORDER),
    "show_distribution": "1",
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
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()  # serialize writes from the thread pool
        with self.tx() as con:
            con.executescript(SCHEMA)
            for key, value in DEFAULT_SETTINGS.items():
                con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value))
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0')")
            if con.execute("SELECT COUNT(*) FROM columns").fetchone()[0] == 0:
                for col in default_columns():
                    self._insert_column(con, col)

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
        return [g.strip() for g in self.settings()["group_order"].split(",") if g.strip()]

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

    def save_column(self, col: Column) -> Column:
        """Insert (``col.id is None``) or update a column. Renames are applied to all formulas referencing it."""
        with self.tx() as con:
            columns = self.columns(con)
            if col.id is None:
                col = replace(col, position=max((c.position for c in columns), default=0) + 1)
                columns.append(col)
            else:
                old = next(c for c in columns if c.id == col.id)
                if old.kind != col.kind:
                    raise ValueError("Der Typ einer bestehenden Spalte kann nicht geändert werden")
                col = replace(col, position=old.position)
                columns = [col if c.id == col.id else c for c in columns]
                if old.name != col.name:
                    columns = [self._renamed(c, old.name, col.name) if c.id != col.id else c for c in columns]
            validate(columns)
            if col.id is None:
                col = replace(col, id=self._insert_column(con, col))
            for c in columns:
                if c.id is not None:
                    con.execute(f"UPDATE columns SET {', '.join(f'{f} = ?' for f in COLUMN_FIELDS)} WHERE id = ?",
                                [*(getattr(c, f) for f in COLUMN_FIELDS), c.id])
            self._bump(con)
            return col

    @staticmethod
    def _renamed(col: Column, old: str, new: str) -> Column:
        formula = fx.rename(col.formula, old, new) if col.kind == "formula" else col.formula
        return replace(col, formula=formula, net_into=new if col.net_into == old else col.net_into)

    def delete_column(self, column_id: int) -> None:
        from .columns import referencing

        with self.tx() as con:
            columns = self.columns(con)
            col = next(c for c in columns if c.id == column_id)
            users = referencing(columns, col.name)
            rest = [c for c in columns if c.id != column_id]
            if users:
                raise ValueError(f"{col.name!r} wird noch verwendet von: {', '.join(users)}")
            validate(rest)  # e.g. a range {A}:{B} must not lose one of its ends
            con.execute("DELETE FROM columns WHERE id = ?", (column_id,))
            self._bump(con)

    def move_column(self, column_id: int, offset: int) -> None:
        with self.tx() as con:
            columns = self.columns(con)
            index = next(i for i, c in enumerate(columns) if c.id == column_id)
            new_index = min(max(index + offset, 0), len(columns) - 1)
            columns.insert(new_index, columns.pop(index))
            columns = [replace(c, position=i) for i, c in enumerate(columns, start=1)]
            validate(columns)
            for c in columns:
                con.execute("UPDATE columns SET position = ? WHERE id = ?", (c.position, c.id))
            self._bump(con)

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

        ``new_columns`` are appended to the configuration first; cells of ``entries`` may refer to them by
        negative ids (-1 = first new column, ...).
        """
        with self.tx() as con:
            columns = self.columns(con)
            start = max((c.position for c in columns), default=0)
            added = [replace(c, position=start + i, id=None) for i, c in enumerate(new_columns, start=1)]
            validate(columns + added)
            ids = {-i: self._insert_column(con, c) for i, c in enumerate(added, start=1)}
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
