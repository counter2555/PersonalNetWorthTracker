"""Compute the full sheet: input values plus all formula columns for every entry."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from . import formula as fx
from .columns import DATE_COLUMN, Column, evaluation_order, range_members
from .db import Cell, Entry

CellValue = float | str | None


@dataclass
class Row:
    date: dt.date
    values: dict[str, CellValue]
    overridden: set[str] = field(default_factory=set)  # formula columns holding a fixed value
    entry_id: int | None = None


class Calculator:
    """Evaluates entries one after another (in date order), so ``@prev`` and ``@first`` can be resolved."""

    def __init__(self, columns: list[Column]):
        self.columns = columns
        self.order = evaluation_order(columns)
        self.parsed = {c.name: c.parsed for c in columns if c.kind == "formula"}
        self.ranges = {}
        for node in self.parsed.values():
            for rng in fx.ranges(node):
                self.ranges[rng] = [c.name for c in range_members(columns, rng)]
        self.rows: list[Row] = []

    def compute(self, entry: Entry,
                decide: Callable[[Column, CellValue], tuple[CellValue, bool]] | None = None) -> Row:
        """Compute one entry and remember it as the previous one for the next call.

        ``decide(column, computed)`` may replace a computed formula value; it returns the value and whether it is a
        fixed value (used by the import to keep values that differ from the formula).
        """
        row = Row(entry.date, {}, entry_id=entry.id)
        ctx = _RowContext(self, row)
        for col in self.order:
            cell = entry.cells.get(col.id, Cell()) if col.id is not None else Cell()
            if col.kind == "text":
                row.values[col.name] = cell.text or None
            elif col.kind == "input":
                row.values[col.name] = cell.value
            elif cell.override:
                row.values[col.name] = cell.value
                row.overridden.add(col.name)
            else:
                try:
                    value = fx.evaluate(self.parsed[col.name], ctx)
                    value = None if value is None else float(value)
                    if value is not None and not math.isfinite(value):
                        value = None
                except fx.EvalError:
                    value = None
                if decide is not None:
                    value, fixed = decide(col, value)
                    if fixed:
                        row.overridden.add(col.name)
                row.values[col.name] = value
        self.rows.append(row)
        return row


class _RowContext(fx.Context):
    def __init__(self, calc: Calculator, row: Row):
        self.calc, self.row = calc, row

    def value(self, ref: fx.Ref):
        if ref.mode == "row":
            return self.row.values.get(ref.name)
        rows = self.calc.rows
        if ref.mode == "prev":
            if not rows:
                raise fx.EvalError("kein vorheriger Eintrag")
            return rows[-1].values.get(ref.name)
        if not rows:  # this is the first entry
            if ref.name not in self.row.values:
                raise fx.EvalError("Selbstbezug im ersten Eintrag")
            return self.row.values.get(ref.name)
        return rows[0].values.get(ref.name)

    def range_values(self, rng: fx.Range):
        return [v for name in self.calc.ranges[rng] if isinstance(v := self.row.values.get(name), (int, float))]


def compute(columns: list[Column], entries: list[Entry]) -> list[Row]:
    calc = Calculator(columns)
    return [calc.compute(entry) for entry in entries]


def to_frame(columns: list[Column], rows: list[Row]) -> pd.DataFrame:
    """Sheet as a DataFrame in column order, first column "Datum" (Timestamp)."""
    names = [c.name for c in sorted(columns, key=lambda c: c.position)]
    data = {DATE_COLUMN: pd.to_datetime([r.date for r in rows])}
    for name in names:
        data[name] = [r.values.get(name) for r in rows]
    df = pd.DataFrame(data)
    for col in columns:
        if col.is_number:
            df[col.name] = pd.to_numeric(df[col.name], errors="coerce").astype(float)
    return df
