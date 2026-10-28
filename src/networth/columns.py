"""Column configuration of the net worth sheet.

The first column is always the date ("Datum"); every other column is configured here and stored in the database:

* kind ``input``    a number entered per snapshot (account balance, ...)
* kind ``formula``  computed from other columns, see :mod:`networth.formula`
* kind ``text``     free text, e.g. the comment

The dashboard needs to know what a column means. ``asset_group`` stacks an account into a group of the wealth
chart; ``role`` marks the columns with a special meaning (see ``ROLES``); ``net_into`` books a column (e.g. a credit
card balance) against another account in the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import formula as fx

DATE_COLUMN = "Datum"

KINDS = {"input": "Eingabe", "formula": "Formel", "text": "Text"}
FORMATS = {"currency": "Euro", "percent": "Prozent", "number": "Zahl"}
ROLES = {
    "total": "Gesamtvermögen (Summe)",
    "depot_value": "Depotwert",
    "depot_gain": "Depot-Entwicklung (Gewinn vor Steuer)",
    "tax": "Kapitalertragsteuer (KESt, ≤ 0)",
    "comment": "Kommentar (Anmerkungen im Chart)",
}
HIGHLIGHT_STYLES = {"red", "yellow", "green"}


@dataclass
class Column:
    name: str
    kind: str = "input"
    formula: str = ""
    number_format: str = "currency"
    width: float = 12.0
    asset_group: str = ""
    role: str = ""
    net_into: str = ""
    highlight: str = ""  # conditional format in the Excel export, e.g. ">25000:red; <15000:yellow"
    position: int = 0
    id: int | None = None

    @property
    def is_number(self) -> bool:
        return self.kind != "text"

    @property
    def parsed(self) -> fx.Node:
        return fx.parse(self.formula)


@dataclass(frozen=True)
class HighlightRule:
    op: str
    value: float
    style: str


def parse_highlight(text: str) -> list[HighlightRule]:
    """``">25000:red; <15000:red"`` -> rules; raises ``ValueError`` on bad input."""
    rules = []
    for part in filter(None, (p.strip() for p in text.replace("\n", ";").split(";"))):
        condition, _, style = part.partition(":")
        style = (style.strip() or "red").lower()
        if style not in HIGHLIGHT_STYLES:
            raise ValueError(f"Unbekannte Farbe {style!r} (erlaubt: {', '.join(sorted(HIGHLIGHT_STYLES))})")
        condition = condition.strip()
        op = next((op for op in (">=", "<=", "<>", ">", "<", "=") if condition.startswith(op)), None)
        try:
            value = float(fx.normalize_number_input(condition[len(op):])) if op else None
        except ValueError:
            value = None
        if value is None:
            raise ValueError(f"Ungültige Regel {part!r}, Beispiel: >25000:red")
        rules.append(HighlightRule(op, value, style))
    return rules


# ---------------------------------------------------------------------------
# Default layout = the original NetWorth.xlsx
# ---------------------------------------------------------------------------

def _c(name: str, kind: str = "input", formula: str = "", fmt: str = "currency", width: float = 12.0, **kw) -> Column:
    return Column(name=name, kind=kind, formula=formula, number_format=fmt, width=width, **kw)


DEFAULT_GROUP_ORDER = ["Vorsorge", "Anleihen", "Konten", "Depot"]  # bottom to top in the wealth chart

DEFAULT_COLUMNS = [
    _c("Girokonto", width=11.7, asset_group="Konten"),
    _c("Sparkonto", width=11.3, asset_group="Konten"),
    _c("Urlaubskonto", width=13.0, asset_group="Konten"),
    _c("Kreditkarte", width=14.9, net_into="Girokonto"),
    _c("Flatex Geld", width=11.1, asset_group="Konten"),
    _c("Flatex Depot", width=12.3, asset_group="Depot", role="depot_value"),
    _c("Depot Entwicklung", width=17.7, role="depot_gain"),
    _c("Depot Einzahlung", "formula", "{Flatex Depot}-{Depot Entwicklung}", width=17.7),
    _c("Kest", "formula", "IF({Depot Entwicklung}>0,-{Depot Entwicklung}*0.275,0)", width=17.7, role="tax"),
    _c("Netto Gewinn", "formula", "{Depot Entwicklung}+{Kest}", width=12.0),
    _c("Bausparer", width=10.1, asset_group="Vorsorge"),
    _c("Bundespensionskasse", width=20.6, asset_group="Vorsorge"),
    _c("Allianz Vorsorgekasse", width=20.4, asset_group="Vorsorge"),
    _c("Bundesschatz", width=13.1, asset_group="Anleihen"),
    _c("Summe", "formula", "SUM({Girokonto}:{Flatex Depot})+SUM({Bausparer}:{Bundesschatz})+{Kest}", width=17.6,
       role="total"),
    _c("Summe Geld", "formula", "SUM({Girokonto}:{Flatex Geld})+{Bundesschatz}", width=12.4,
       highlight=">25000:red; <15000:red"),
    _c("Cash Rate", "formula", "{Summe Geld}/{Summe}", "percent", 9.9),
    _c("Liquides Vermögen", "formula", "{Urlaubskonto}+{Girokonto}+{Sparkonto}+{Flatex Geld}+{Flatex Depot}"
       "+{Kreditkarte}+{Bundesschatz}+{Kest}", width=18.3),
    _c("Liquide Rate", "formula", "{Liquides Vermögen}/{Summe}", "percent", 12.0),
    _c("Anlagevermögen", "formula", "{Flatex Depot}+{Bundesschatz}+{Bundespensionskasse}+{Allianz Vorsorgekasse}"
       "+{Kest}", width=16.3),
    _c("Anlage Rate", "formula", "{Anlagevermögen}/{Summe}", "percent", 11.7),
    _c("Aktien Rate", "formula", "({Flatex Depot}+{Kest})/{Summe}", "percent", 11.7),
    _c("Depot Gewinn Anteil", "formula", "{Depot Entwicklung}/{Flatex Depot}", "percent", 19.9),
    _c("Verzinstes Vermögen", "formula", "{Sparkonto}+{Flatex Depot}+{Bausparer}+{Bundespensionskasse}"
       "+{Bundesschatz}+{Allianz Vorsorgekasse}+{Urlaubskonto}+{Kest}", width=20.1),
    _c("Verzinster Anteil", "formula", "{Verzinstes Vermögen}/{Summe}", "percent", 16.1,
       highlight="<0.85:red; <0.9:yellow"),
    _c("Änderung Anlage", "formula", "({Anlagevermögen}-{Anlagevermögen@prev})/{Summe@prev}", "percent", 16.4),
    _c("Änderung Gesamt", "formula", "({Summe}-{Summe@prev})/{Summe@prev}", "percent", 17.1),
    _c("Änderung seit Start", "formula", "({Summe}-{Summe@first})/{Summe@first}", "percent", 18.3),
    _c("Änderung seit Start (Abs)", "formula", "{Summe}-{Summe@first}", width=23.4),
    _c("Kommentar", "text", width=30.0, role="comment"),
]


def default_columns() -> list[Column]:
    return [replace(col, position=i) for i, col in enumerate(DEFAULT_COLUMNS, start=1)]


# ---------------------------------------------------------------------------
# Dependencies and validation
# ---------------------------------------------------------------------------

def range_members(columns: list[Column], rng: fx.Range) -> list[Column]:
    """Columns between the two ends of a range, by position (like a cell range in Excel)."""
    by_name = {c.name: c for c in columns}
    a, b = by_name[rng.start.name].position, by_name[rng.end.name].position
    low, high = min(a, b), max(a, b)
    return [c for c in sorted(columns, key=lambda c: c.position) if low <= c.position <= high]


def dependencies(columns: list[Column], col: Column) -> set[str]:
    """Columns of the same entry that must be known before ``col`` can be computed."""
    if col.kind != "formula":
        return set()
    node = col.parsed
    deps = {n.name for n in fx.walk(node) if isinstance(n, fx.Ref) and n.mode in ("row", "first")}
    for rng in fx.ranges(node):
        deps |= {c.name for c in range_members(columns, rng)}
    deps.discard(col.name)  # {X@first} in X itself: the first entry has no earlier value
    return deps


def evaluation_order(columns: list[Column]) -> list[Column]:
    """Columns sorted so that every formula comes after the columns it uses; raises on cycles."""
    by_name = {c.name: c for c in columns}
    ordered: list[Column] = []
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(col: Column, path: list[str]) -> None:
        if state.get(col.name) == 2:
            return
        if state.get(col.name) == 1:
            cycle = path[path.index(col.name):] + [col.name]
            raise ValueError(f"Zirkelbezug: {' → '.join(cycle)}")
        state[col.name] = 1
        for dep in sorted(dependencies(columns, col)):
            visit(by_name[dep], path + [col.name])
        state[col.name] = 2
        ordered.append(col)

    for col in sorted(columns, key=lambda c: c.position):
        visit(col, [])
    return ordered


def validate(columns: list[Column]) -> None:
    """Raise ``ValueError`` with a German message if the configuration is inconsistent."""
    names = [c.name for c in columns]
    by_name = {c.name: c for c in columns}
    for col in columns:
        if not col.name.strip():
            raise ValueError("Spaltenname darf nicht leer sein")
        if col.name.strip() != col.name:
            raise ValueError(f"Spaltenname {col.name!r} hat Leerzeichen am Anfang/Ende")
        if any(ch in col.name for ch in "{}@"):
            raise ValueError(f"Spaltenname {col.name!r} darf keine Zeichen {{ }} @ enthalten")
        if col.name == DATE_COLUMN or names.count(col.name) > 1:
            raise ValueError(f"Spaltenname {col.name!r} ist schon vergeben")
        if col.kind not in KINDS:
            raise ValueError(f"Unbekannter Typ {col.kind!r}")
        if col.number_format not in FORMATS:
            raise ValueError(f"Unbekanntes Format {col.number_format!r}")
        if col.role and col.role not in ROLES:
            raise ValueError(f"Unbekannte Rolle {col.role!r}")
        if col.kind == "formula":
            try:
                node = col.parsed
            except fx.FormulaError as exc:
                raise ValueError(f"Formel von {col.name!r}: {exc}") from exc
            for ref in fx.references(node):
                if ref not in by_name:
                    raise ValueError(f"Formel von {col.name!r} verweist auf unbekannte Spalte {{{ref}}}")
                if not by_name[ref].is_number:
                    raise ValueError(f"Formel von {col.name!r} verweist auf die Textspalte {{{ref}}}")
        elif col.formula:
            raise ValueError(f"Nur Formelspalten haben eine Formel ({col.name!r})")
        if col.net_into:
            target = by_name.get(col.net_into)
            if target is None or target.kind == "text" or target is col:
                raise ValueError(f"{col.name!r}: 'verrechnen mit' muss eine andere Zahlenspalte sein")
            if col.asset_group:
                raise ValueError(f"{col.name!r}: entweder Anlagegruppe oder 'verrechnen mit', nicht beides")
        if col.role == "comment" and col.kind != "text":
            raise ValueError("Die Kommentar-Rolle braucht eine Textspalte")
        if col.role and col.role != "comment" and col.kind == "text":
            raise ValueError(f"Die Rolle {ROLES[col.role]} braucht eine Zahlenspalte")
        if col.asset_group and col.kind == "text":
            raise ValueError("Textspalten können keiner Anlagegruppe angehören")
        if col.highlight:
            parse_highlight(col.highlight)
    roles = [c.role for c in columns if c.role]
    duplicate = next((r for r in roles if roles.count(r) > 1), None)
    if duplicate:
        raise ValueError(f"Die Rolle {ROLES[duplicate]} ist mehreren Spalten zugeordnet")
    evaluation_order(columns)


def referencing(columns: list[Column], name: str) -> list[str]:
    """Columns whose formula (incl. ranges) or 'net_into' depends on column ``name``."""
    result = []
    for col in columns:
        if col.net_into == name:
            result.append(col.name)
        elif col.kind == "formula" and col.name != name:
            node = col.parsed
            if name in fx.references(node):
                result.append(col.name)
    return result


@dataclass
class Layout:
    """What the dashboard needs to know about the columns."""
    groups: dict[str, list[str]] = field(default_factory=dict)  # stack order, bottom to top
    net_into: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)  # role -> column name

    @staticmethod
    def from_columns(columns: list[Column], group_order: list[str]) -> "Layout":
        ordered = sorted(columns, key=lambda c: c.position)
        names = [c.asset_group for c in ordered if c.asset_group]
        group_names = [g for g in group_order if g in names] + [g for g in dict.fromkeys(names) if g not in group_order]
        return Layout(
            groups={g: [c.name for c in ordered if c.asset_group == g] for g in group_names},
            net_into={c.name: c.net_into for c in ordered if c.net_into},
            roles={c.role: c.name for c in ordered if c.role},
        )
