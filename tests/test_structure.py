import sqlite3

import pytest

from networth.columns import Column
from networth.db import Store
from networth.sheet import compute
from networth.structure import build, default_columns, migrate

OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE columns (id INTEGER PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    formula TEXT NOT NULL DEFAULT '', number_format TEXT NOT NULL DEFAULT 'currency', width REAL NOT NULL DEFAULT 12,
    asset_group TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '', net_into TEXT NOT NULL DEFAULT '',
    highlight TEXT NOT NULL DEFAULT '');
CREATE TABLE entries (id INTEGER PRIMARY KEY, date TEXT NOT NULL);
CREATE TABLE cells (entry_id INTEGER NOT NULL, column_id INTEGER NOT NULL, value REAL, text TEXT,
    expr TEXT NOT NULL DEFAULT '', override INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (entry_id, column_id));
"""

# the layout of the previous version for new users: (name, kind, formula, format, asset_group, role, net_into)
OLD_BASIC = [
    ("Girokonto", "input", "", "currency", "Konten", "", ""),
    ("Sparkonto", "input", "", "currency", "Konten", "", ""),
    ("Kreditkarte", "input", "", "currency", "", "", "Girokonto"),
    ("Depot", "input", "", "currency", "Depot", "depot_value", ""),
    ("Depot Entwicklung", "input", "", "currency", "", "depot_gain", ""),
    ("Kest", "formula", "IF({Depot Entwicklung}>0,-{Depot Entwicklung}*0.25,0)", "currency", "", "tax", ""),
    ("Summe", "formula", "SUM({Girokonto}:{Depot})+{Kest}", "currency", "", "total", ""),
    ("Bar", "formula", "SUM({Girokonto}:{Sparkonto})", "currency", "", "", ""),
    ("Bar Anteil", "formula", "({Girokonto}+{Sparkonto})/{Summe}", "percent", "", "", ""),
    ("Änderung Gesamt", "formula", "({Summe}-{Summe@prev})/{Summe@prev}", "percent", "", "", ""),
    ("Eigene", "formula", "MAX({Girokonto}:{Sparkonto})*2", "currency", "", "", ""),
    ("Kommentar", "text", "", "currency", "", "comment", ""),
]


def old_database(path, columns=OLD_BASIC, group_order="Depot, Konten"):
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("group_order", group_order), ("revision", "7")])
    for i, (name, kind, formula, fmt, group, role, net_into) in enumerate(columns, start=1):
        con.execute("INSERT INTO columns (id, position, name, kind, formula, number_format, asset_group, role, "
                    "net_into) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (i, i, name, kind, formula, fmt, group, role,
                                                                     net_into))
    con.execute("INSERT INTO entries VALUES (1, '2024-01-31')")
    ids = {c[0]: i for i, c in enumerate(columns, start=1)}
    for name, value in (("Girokonto", 1000), ("Sparkonto", 500), ("Kreditkarte", -100), ("Depot", 2000),
                        ("Depot Entwicklung", 400)):
        if name in ids:
            con.execute("INSERT INTO cells (entry_id, column_id, value) VALUES (1, ?, ?)", (ids[name], value))
    con.commit()
    con.close()


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    store = Store(path)
    by_name = {c.name: c for c in store.columns()}
    specs = {name: c.spec for name, c in by_name.items()}
    assert specs == {"Girokonto": "asset", "Sparkonto": "asset", "Kreditkarte": "liability", "Depot": "depot",
                     "Depot Entwicklung": "depot_gain", "Kest": "tax", "Summe": "total", "Bar": "sum",
                     "Bar Anteil": "share", "Änderung Gesamt": "change_prev", "Eigene": "custom",
                     "Kommentar": "comment"}
    assert store.tax_rate() == 0.25 and store.structure_notes() == []
    assert store.group_order() == ["Konten", "Depot"]  # order of the entry form and the Excel
    assert by_name["Eigene"].formula == "MAX({Girokonto},{Sparkonto})*2"  # ranges no longer depend on positions
    assert by_name["Summe"].formula == "{Girokonto}+{Kreditkarte}+{Sparkonto}+{Depot}+{Kest}"

    row = compute(store.columns(), store.entries())[0].values
    assert row["Summe"] == pytest.approx(1000 + 500 - 100 + 2000 - 100)
    assert row["Bar"] == 1500 and row["Eigene"] == 2000

    reopened = Store(path)  # migrated once
    assert [c.name for c in reopened.columns()] == [c.name for c in store.columns()]


def test_migration_keeps_a_differing_total(tmp_path):
    path = tmp_path / "old.sqlite"
    columns = [c if c[0] != "Summe" else ("Summe", "formula", "{Girokonto}+{Sparkonto}", "currency", "", "total", "")
               for c in OLD_BASIC]
    old_database(path, columns)
    store = Store(path)
    by_name = {c.name: c for c in store.columns()}
    assert by_name["Summe"].spec == "total" and by_name["Summe (alt)"].spec == "custom"
    assert by_name["Summe (alt)"].formula == "{Girokonto}+{Sparkonto}"
    assert by_name["Bar Anteil"].formula.endswith("/{Summe}")  # now the automatic total
    notes = store.structure_notes()
    assert len(notes) == 1 and "Summe (alt)" in notes[0]
    store.dismiss_structure_notes()
    assert store.structure_notes() == []


def test_migration_without_depot_and_odd_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path, [
        ("Konto", "input", "", "currency", "Konten", "", ""),
        ("Karte", "input", "", "currency", "", "", "Notizen"),  # booked against a text column
        ("Gewinn", "input", "", "currency", "", "depot_gain", ""),  # no depot
        ("Notizen", "text", "", "currency", "", "", ""),
    ], group_order="Konten")
    store = Store(path)
    specs = {c.name: c.spec for c in store.columns()}
    assert specs == {"Konto": "asset", "Karte": "value", "Gewinn": "value", "Notizen": "note",
                     "Summe": "total", "Kommentar": "comment"}
    assert len(store.structure_notes()) == 2


def test_migrating_the_current_layouts_changes_nothing():
    for layout in ("full", "basic"):
        columns = default_columns(layout)
        result = migrate([Column(**{**c.__dict__, "spec": "", "id": i}) for i, c in enumerate(columns, start=1)])
        rebuilt, _ = build(result.columns, result.groups, result.tax_rate)
        assert result.notes == []
        assert [(c.name, c.formula) for c in rebuilt] == [(c.name, c.formula) for c in columns]


def test_new_database_and_build_rules(tmp_path):
    store = Store(tmp_path / "new.sqlite", layout="basic")
    assert {c.spec for c in store.columns()} >= {"total", "comment", "depot", "depot_gain", "tax"}
    columns = store.columns()
    with pytest.raises(ValueError, match="nur ein Feld der Art Depot"):
        build(columns + [Column(name="Zweitdepot", spec="depot", asset_group="Depot")], ["Depot"], 0.275)
    with pytest.raises(ValueError, match="Gruppe"):
        build(columns + [Column(name="Lose", spec="asset")], [], 0.275)
    with pytest.raises(ValueError, match="zu dem die Schulden gehören"):
        build(columns + [Column(name="Schuld", spec="liability", net_into="Kreditkarte")], [], 0.275)
