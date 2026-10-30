"""Accounts, metrics and text fields: what users configure instead of raw columns.

* **Accounts** sit in groups: an asset (``asset``), debts booked against an account (``liability``, e.g. a credit
  card), the depot (``depot``, at most one) or another number that isn't part of the net worth (``value``).
* **Metrics** come from a catalog (change since the previous entry, sum or share of selected accounts, depot
  figures, ...) or are an own formula (``custom``).
* **Text fields**: the comment of every entry (``comment``) and further notes (``note``).

Every item is still stored as a :class:`~networth.columns.Column`, its ``spec`` says what it is. :func:`build` derives
everything else from the specs: the total (always the sum of all accounts), the depot gain field and the capital
gains tax, the formulas of the catalog metrics, roles, asset groups and the column order. Sheet, Excel and dashboard
keep working on plain columns.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from . import formula as fx
from .columns import Column, range_members
from .columns import validate as validate_columns

ACCOUNT_SPECS = {"asset": "Guthaben", "liability": "Schulden", "depot": "Depot", "value": "Sonstiger Wert"}
TOP_ACCOUNTS = {"asset", "depot"}  # accounts with an own place in a group
IN_TOTAL = {"asset", "liability", "depot"}

# metric -> (label, number format); custom formulas choose their format
METRICS = {
    "change_prev": ("Veränderung zum vorigen Eintrag (%)", "percent"),
    "change_prev_abs": ("Veränderung zum vorigen Eintrag (€)", "currency"),
    "change_start": ("Veränderung seit dem ersten Eintrag (%)", "percent"),
    "change_start_abs": ("Veränderung seit dem ersten Eintrag (€)", "currency"),
    "sum": ("Summe ausgewählter Konten", "currency"),
    "share": ("Anteil ausgewählter Konten an der Summe", "percent"),
    "depot_paid_in": ("Depot: eingezahltes Kapital (Wert − Gewinn)", "currency"),
    "depot_net_gain": ("Depot: Gewinn nach Steuer", "currency"),
    "depot_gain_share": ("Depot: Gewinnanteil am Depotwert", "percent"),
    "custom": ("Eigene Formel", ""),
}
MEMBER_METRICS = {"sum", "share"}
DEPOT_METRICS = {"depot_paid_in", "depot_net_gain", "depot_gain_share"}
TEXT_SPECS = {"comment": "Kommentar", "note": "Notizfeld"}
AUTOMATIC = {"total", "depot_gain", "tax"}  # created and removed together with what they belong to

SPECS = {**ACCOUNT_SPECS, **{k: v for k, (v, _) in METRICS.items()}, **TEXT_SPECS,
         "total": "Gesamtvermögen", "depot_gain": "Depot-Gewinn", "tax": "Kapitalertragsteuer"}
KIND_OF = {**{s: "input" for s in ACCOUNT_SPECS}, "depot_gain": "input",
           **{s: "formula" for s in METRICS}, "total": "formula", "tax": "formula",
           **{s: "text" for s in TEXT_SPECS}}
ROLE_OF = {"depot": "depot_value", "depot_gain": "depot_gain", "tax": "tax", "total": "total", "comment": "comment"}

DEFAULT_TAX_RATE = 0.275  # KESt in Austria
DEFAULT_NAMES = {"total": "Summe", "depot_gain": "Depot Entwicklung", "tax": "Kest", "comment": "Kommentar"}
OTHER_GROUP = "Sonstiges"


def spec_of(col: Column) -> str:
    """The column's spec; columns without one (e.g. from an import) are other values, own formulas or notes."""
    if col.spec:
        return col.spec
    if col.kind == "formula":
        return "custom"
    if col.kind == "text":
        return "note"
    return "asset" if col.asset_group else "value"


def is_metric(col: Column) -> bool:
    return spec_of(col) in METRICS


def auto_width(name: str) -> float:
    return round(min(max(len(name) * 1.1 + 2, 10.0), 30.0), 1)


def _ref(name: str, mode: str = "row") -> str:
    return fx.source_ref(fx.Ref(name, mode))


def _plus(names: list[str]) -> str:
    return "+".join(_ref(n) for n in names) or "0"


def member_formula(names: list[str]) -> str:
    """Formula of a "sum" or "share" metric before :func:`build` (which adds the division by the total)."""
    return _plus(names)


def tax_formula(gain: str, rate: float) -> str:
    return f"IF({_ref(gain)}>0,-{_ref(gain)}*{rate:.10g},0)"


def members(col: Column) -> list[str]:
    """Columns referenced by a "sum" or "share" metric in formula order (:func:`build` drops the total)."""
    if not col.formula:
        return []
    return list(dict.fromkeys(n.name for n in fx.walk(col.parsed) if isinstance(n, fx.Ref)))


def member_candidates(columns: list[Column]) -> list[Column]:
    """What a sum or share can add up: all number inputs and the capital gains tax."""
    return [c for c in sorted(columns, key=lambda c: c.position)
            if c.kind == "input" or spec_of(c) == "tax"]


def section(col: Column) -> tuple:
    """Columns that are sorted among each other (moving up/down stays within the section)."""
    spec = spec_of(col)
    if spec in TOP_ACCOUNTS:
        return ("account", col.asset_group)
    if spec == "liability":
        return ("liability", col.net_into)
    if spec in METRICS:
        return ("metric",)
    return (spec,)  # other values, notes; automatic fields are alone in their section


def unique_name(base: str, taken: set[str]) -> str:
    name, i = base, 2
    while name in taken:
        name, i = f"{base} {i}", i + 1
    return name


# ---------------------------------------------------------------------------
# Build: derive the columns from the specs
# ---------------------------------------------------------------------------

def build(columns: list[Column], groups: list[str], tax_rate: float) -> tuple[list[Column], list[str]]:
    """Columns with derived formulas, roles and order, plus the group order (bottom to top in the wealth chart).

    Creates the total and the comment if missing, and the depot gain and tax for a depot. Raises ``ValueError``
    if the configuration is inconsistent.
    """
    cols = [replace(c, spec=spec_of(c)) for c in sorted(columns, key=lambda c: (c.position, c.id or 0))]
    taken = {c.name for c in cols}

    def ensure(spec: str) -> None:
        if not any(c.spec == spec for c in cols):
            name = unique_name(DEFAULT_NAMES[spec], taken)
            taken.add(name)
            cols.append(Column(name=name, kind=KIND_OF[spec], spec=spec, width=auto_width(name),
                               position=max((c.position for c in cols), default=0) + 1))

    ensure("total")
    ensure("comment")
    if any(c.spec == "depot" for c in cols):
        ensure("depot_gain")
        ensure("tax")

    for c in cols:
        if c.spec not in SPECS:
            raise ValueError(f"Unbekannte Art {c.spec!r} ({c.name})")
    for spec in ("total", "comment", "depot", "depot_gain", "tax"):
        if sum(c.spec == spec for c in cols) > 1:
            raise ValueError(f"Es gibt nur ein Feld der Art {SPECS[spec]}")

    groups = list(dict.fromkeys([g for g in groups if g] + [c.asset_group for c in cols
                                                             if c.spec in TOP_ACCOUNTS and c.asset_group]))
    by_spec = {c.spec: c for c in cols if c.spec in ("total", "depot", "depot_gain", "tax")}
    total = by_spec["total"].name
    depot, gain, tax = (by_spec[s].name if s in by_spec else None for s in ("depot", "depot_gain", "tax"))
    accounts = {c.name: c for c in cols if c.spec in ACCOUNT_SPECS}
    numbers = {c.name for c in cols if c.kind == "input"} | ({tax} if tax else set())

    def derive(c: Column) -> Column:
        spec = c.spec
        values = dict(kind=KIND_OF[spec], role=ROLE_OF.get(spec, ""),
                      asset_group=c.asset_group if spec in TOP_ACCOUNTS else "",
                      net_into=c.net_into if spec == "liability" else "",
                      width=c.width if c.width > 0 else auto_width(c.name))
        if spec in ACCOUNT_SPECS and spec != "value" or spec == "depot_gain":
            values["number_format"] = "currency"
        if spec in METRICS and spec != "custom":
            values["number_format"] = METRICS[spec][1]
        if KIND_OF[spec] != "formula":
            values["formula"] = ""
        elif spec == "tax":
            values["formula"] = tax_formula(gain, tax_rate)
        elif spec == "total":
            values["formula"] = _plus([n for n in ordered_accounts if accounts[n].spec in IN_TOTAL]
                                      + ([tax] if tax else []))
        elif spec in MEMBER_METRICS:
            names = [n for n in members(c) if n in numbers]
            if not names:
                raise ValueError(f"{c.name!r}: bitte mindestens ein Konto auswählen")
            summed = _plus(names)
            if spec == "share":
                summed = f"{_ref(names[0])}/{_ref(total)}" if len(names) == 1 else f"({summed})/{_ref(total)}"
            values["formula"] = summed
        elif spec in DEPOT_METRICS:
            if depot is None:
                raise ValueError(f"{c.name!r} braucht ein Depot")
            values["formula"] = {"depot_paid_in": f"{_ref(depot)}-{_ref(gain)}",
                                 "depot_net_gain": f"{_ref(gain)}+{_ref(tax)}",
                                 "depot_gain_share": f"{_ref(gain)}/{_ref(depot)}"}[spec]
        elif spec.startswith("change"):
            mode = "prev" if spec.startswith("change_prev") else "first"
            diff = f"{_ref(total)}-{_ref(total, mode)}"
            values["formula"] = diff if spec.endswith("_abs") else f"({diff})/{_ref(total, mode)}"
        elif spec == "custom":
            node = c.parsed
            if fx.ranges(node):
                raise ValueError(f"Formel von {c.name!r}: Bereiche wie {{A}}:{{B}} gibt es nicht mehr – "
                                 "Konten einzeln angeben, z. B. SUM({Girokonto},{Sparkonto})")
        return replace(c, **values)

    # order: groups with their accounts (debts and the depot gain right after their account), other values,
    # tax and total, metrics, text fields
    top = [c for c in cols if c.spec in TOP_ACCOUNTS]
    for c in top:
        if not c.asset_group:
            raise ValueError(f"{c.name!r}: bitte eine Gruppe wählen")
    for c in cols:
        if c.spec == "liability" and (c.net_into not in accounts or accounts[c.net_into].spec not in TOP_ACCOUNTS):
            raise ValueError(f"{c.name!r}: bitte das Konto wählen, zu dem die Schulden gehören")
    ordered: list[Column] = []
    for group in groups:
        for account in (c for c in top if c.asset_group == group):
            ordered.append(account)
            ordered += [c for c in cols if c.spec == "liability" and c.net_into == account.name]
            if account.spec == "depot":
                ordered.append(by_spec["depot_gain"])
    ordered += [c for c in cols if c.spec == "value"]
    ordered += [by_spec[s] for s in ("tax", "total") if s in by_spec]
    ordered += [c for c in cols if c.spec in METRICS]
    ordered += [c for c in cols if c.spec == "comment"] + [c for c in cols if c.spec == "note"]
    ordered_accounts = [c.name for c in ordered if c.spec in ACCOUNT_SPECS]

    result = [replace(derive(c), position=i) for i, c in enumerate(ordered, start=1)]
    validate(result)
    return result, groups


def validate(columns: list[Column]) -> None:
    validate_columns(columns)
    for c in columns:
        if c.spec in {"depot_gain", "tax"} | DEPOT_METRICS and not any(x.spec == "depot" for x in columns):
            raise ValueError(f"{c.name!r} braucht ein Depot")


def rename(columns: list[Column], old: str, new: str) -> list[Column]:
    """Every formula and debt pointing to column ``old`` now points to ``new``."""
    return [replace(c, formula=fx.rename(c.formula, old, new) if c.kind == "formula" and c.formula else c.formula,
                    net_into=new if c.net_into == old else c.net_into) for c in columns]


def blockers(columns: list[Column], name: str) -> list[str]:
    """Columns that stop ``name`` from being deleted: own formulas using it and debts booked against it."""
    return [c.name for c in columns if c.name != name and (
        (spec_of(c) == "liability" and c.net_into == name)
        or (spec_of(c) == "custom" and name in fx.references(c.parsed)))]


def without(columns: list[Column], name: str) -> list[Column]:
    """Columns after deleting ``name``; a depot takes its gain, tax and depot metrics along."""
    col = next(c for c in columns if c.name == name)
    if spec_of(col) == "depot":
        dropped = {"depot", "depot_gain", "tax"} | DEPOT_METRICS
        rest = [c for c in columns if spec_of(c) not in dropped]
    else:
        rest = [c for c in columns if c.name != name]
    numbers = {c.name for c in member_candidates(rest)}
    return [c for c in rest if spec_of(c) not in MEMBER_METRICS or set(members(c)) & numbers]  # nothing left to add


# ---------------------------------------------------------------------------
# Overview for the configuration page
# ---------------------------------------------------------------------------

@dataclass
class AccountItem:
    account: Column
    debts: list[Column] = field(default_factory=list)
    gain: Column | None = None


@dataclass
class Overview:
    groups: list[tuple[str, list[AccountItem]]]
    values: list[Column]
    automatic: list[Column]  # total and tax
    metrics: list[Column]
    texts: list[Column]
    depot: Column | None = None


def overview(columns: list[Column], groups: list[str]) -> Overview:
    cols = sorted(columns, key=lambda c: c.position)
    gain = next((c for c in cols if c.spec == "depot_gain"), None)
    items = {g: [] for g in groups}
    for c in cols:
        if c.spec in TOP_ACCOUNTS:
            items.setdefault(c.asset_group, []).append(AccountItem(
                c, [d for d in cols if d.spec == "liability" and d.net_into == c.name],
                gain if c.spec == "depot" else None))
    return Overview(groups=list(items.items()), values=[c for c in cols if c.spec == "value"],
                    automatic=[c for c in cols if c.spec in ("total", "tax")],
                    metrics=[c for c in cols if c.spec in METRICS],
                    texts=[c for c in cols if c.spec in TEXT_SPECS],
                    depot=next((c for c in cols if c.spec == "depot"), None))


# ---------------------------------------------------------------------------
# Migration of a column configuration from before the specs
# ---------------------------------------------------------------------------

def _expand_ranges(node: fx.Node, columns: list[Column]) -> fx.Node:
    """``SUM({A}:{C})`` -> ``SUM({A},{B},{C})`` (number columns only), so nothing depends on the order any more."""
    if isinstance(node, fx.Call):
        args = []
        for arg in node.args:
            if isinstance(arg, fx.Range):
                args += [fx.Ref(c.name) for c in range_members(columns, arg) if c.is_number]
            else:
                args.append(_expand_ranges(arg, columns))
        return fx.Call(node.name, tuple(args))
    if isinstance(node, fx.Unary):
        return fx.Unary(node.op, _expand_ranges(node.operand, columns))
    if isinstance(node, fx.Binary):
        return fx.Binary(node.op, _expand_ranges(node.left, columns), _expand_ranges(node.right, columns))
    if isinstance(node, fx.Paren):
        return fx.Paren(_expand_ranges(node.inner, columns))
    return node


def _summed(node: fx.Node) -> list[str] | None:
    """Columns of a plain sum like ``{A}+SUM({B},{C})``; ``None`` for anything else."""
    if isinstance(node, fx.Ref):
        return [node.name] if node.mode == "row" else None
    if isinstance(node, fx.Paren):
        return _summed(node.inner)
    if isinstance(node, fx.Binary) and node.op == "+":
        left, right = _summed(node.left), _summed(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, fx.Call) and node.name == "SUM":
        parts = [_summed(a) for a in node.args]
        return None if any(p is None for p in parts) else [n for p in parts for n in p]
    return None


@dataclass
class Migration:
    columns: list[Column]
    groups: list[str]
    tax_rate: float
    notes: list[str] = field(default_factory=list)


def migrate(columns: list[Column], tax_rate: float = DEFAULT_TAX_RATE) -> Migration:
    """Specs for a configuration of plain columns (role, asset group, "net into" and formulas)."""
    cols = sorted(columns, key=lambda c: c.position)
    notes: list[str] = []
    expanded = {c.name: fx.render(_expand_ranges(c.parsed, cols), fx.source_ref)
                for c in cols if c.kind == "formula"}
    specs: dict[str, str] = {}
    for c in cols:
        if c.kind == "text":
            specs[c.name] = "comment" if c.role == "comment" else "note"
        elif c.kind == "input":
            if c.role == "depot_value":
                specs[c.name] = "depot"
            elif c.asset_group:
                specs[c.name] = "asset"
            elif c.net_into:
                specs[c.name] = "liability"
            else:
                specs[c.name] = "value"
    has_depot = "depot" in specs.values()
    for c in cols:
        if c.kind == "input" and c.role == "depot_gain":
            specs[c.name] = "depot_gain" if has_depot else "value"
        if specs.get(c.name) == "liability" and specs.get(c.net_into) not in TOP_ACCOUNTS:
            specs[c.name] = "value"
            notes.append(f"„{c.name}“ wurde mit „{c.net_into}“ verrechnet, das kein Konto ist – jetzt ein sonstiger "
                         "Wert ohne Verrechnung.")

    by_role = {c.role: c for c in cols if c.role}
    gain = by_role["depot_gain"].name if has_depot and "depot_gain" in by_role else DEFAULT_NAMES["depot_gain"]
    depot = next((n for n, s in specs.items() if s == "depot"), None)
    tax = by_role["tax"].name if "tax" in by_role else DEFAULT_NAMES["tax"]
    total = by_role["total"].name if "total" in by_role else DEFAULT_NAMES["total"]
    accounts = [n for n, s in specs.items() if s in IN_TOTAL]
    result: list[Column] = []
    renamed: dict[str, str] = {}

    def keep_as_custom(c: Column, reason: str) -> None:
        name = unique_name(f"{c.name} (alt)", {x.name for x in cols} | set(renamed.values()))
        renamed[c.name] = name
        notes.append(f"{reason} Die bisherige Formel steht jetzt als eigene Formel in „{name}“.")

    for c in cols:
        if c.kind != "formula":
            continue
        text = expanded[c.name]
        node = fx.parse(text)
        if c.role == "total":
            expected = accounts + ([tax] if "tax" in by_role else [])
            if sorted(_summed(node) or []) == sorted(expected):
                specs[c.name] = "total"
            else:
                keep_as_custom(c, f"„{c.name}“ ist jetzt automatisch die Summe aller Konten.")
            continue
        if c.role == "tax":
            match = has_depot and re.fullmatch(
                r"IF\(" + re.escape(_ref(gain)) + r">0,-" + re.escape(_ref(gain)) + r"\*([0-9.]+),0\)", text)
            if match:
                specs[c.name], tax_rate = "tax", float(match.group(1))
            elif has_depot:
                keep_as_custom(c, f"„{c.name}“ wird jetzt automatisch aus dem Depot-Gewinn berechnet.")
            else:
                specs[c.name] = "custom"
            continue
        specs[c.name] = "custom"
        candidates = {"change_prev": f"({_ref(total)}-{_ref(total, 'prev')})/{_ref(total, 'prev')}",
                      "change_prev_abs": f"{_ref(total)}-{_ref(total, 'prev')}",
                      "change_start": f"({_ref(total)}-{_ref(total, 'first')})/{_ref(total, 'first')}",
                      "change_start_abs": f"{_ref(total)}-{_ref(total, 'first')}"}
        if depot and "tax" in by_role:
            candidates |= {"depot_paid_in": f"{_ref(depot)}-{_ref(gain)}",
                           "depot_net_gain": f"{_ref(gain)}+{_ref(tax)}",
                           "depot_gain_share": f"{_ref(gain)}/{_ref(depot)}"}
        numbers = {n for n, s in specs.items() if s in ACCOUNT_SPECS or s == "depot_gain"} | {tax}
        found = next((s for s, f in candidates.items() if fx.parse(f) == node), None)
        if found:
            specs[c.name] = found
        elif (names := _summed(node)) and set(names) <= numbers and len(set(names)) == len(names):
            specs[c.name] = "sum"
        elif (isinstance(node, fx.Binary) and node.op == "/" and node.right == fx.Ref(total)
              and (names := _summed(node.left)) and set(names) <= numbers and len(set(names)) == len(names)):
            specs[c.name] = "share"

    for c in cols:
        formula = expanded.get(c.name, c.formula)
        spec = specs.get(c.name, "custom")
        if c.name in renamed:
            spec = "custom"
        result.append(replace(c, spec=spec, formula=formula))
    for old, new in renamed.items():  # the "(alt)" copy keeps the cells (fixed values from an import)
        result = [replace(c, name=new) if c.name == old else c for c in result]
    for spec, name in (("total", total), ("tax", tax)):
        if spec == "tax" and not has_depot:
            continue
        if not any(c.spec == spec for c in result):
            result.append(Column(name=unique_name(name, {c.name for c in result}), kind="formula", spec=spec,
                                 position=next((c.position for c in result if c.name == renamed.get(name)), 0)))
    if not has_depot and "depot_gain" in by_role:
        notes.append(f"„{by_role['depot_gain'].name}“ gehört zu keinem Depot und ist jetzt ein sonstiger Wert.")

    groups = list(dict.fromkeys(c.asset_group for c in cols if specs.get(c.name) in TOP_ACCOUNTS))
    return Migration(result, groups, tax_rate, notes)


# ---------------------------------------------------------------------------
# Default layouts; "full" = the original NetWorth.xlsx
# ---------------------------------------------------------------------------

def _c(name: str, spec: str, group: str = "", width: float = 0.0, **kw) -> Column:
    return Column(name=name, kind=KIND_OF[spec], spec=spec, asset_group=group, width=width, **kw)


def _members(*names: str) -> str:
    return _plus(list(names))


DEFAULT_COLUMNS = [
    _c("Girokonto", "asset", "Konten", 11.7),
    _c("Kreditkarte", "liability", width=14.9, net_into="Girokonto"),
    _c("Sparkonto", "asset", "Konten", 11.3),
    _c("Urlaubskonto", "asset", "Konten", 13.0),
    _c("Flatex Geld", "asset", "Konten", 11.1),
    _c("Flatex Depot", "depot", "Depot", 12.3),
    _c("Depot Entwicklung", "depot_gain", width=17.7),
    _c("Bausparer", "asset", "Vorsorge", 10.1),
    _c("Bundespensionskasse", "asset", "Vorsorge", 20.6),
    _c("Allianz Vorsorgekasse", "asset", "Vorsorge", 20.4),
    _c("Bundesschatz", "asset", "Anleihen", 13.1),
    _c("Kest", "tax", width=17.7),
    _c("Summe", "total", width=17.6),
    _c("Depot Einzahlung", "depot_paid_in", width=17.7),
    _c("Netto Gewinn", "depot_net_gain", width=12.0),
    _c("Summe Geld", "sum", width=12.4, highlight=">25000:red; <15000:red",
       formula=_members("Girokonto", "Sparkonto", "Urlaubskonto", "Kreditkarte", "Flatex Geld", "Bundesschatz")),
    _c("Cash Rate", "custom", width=9.9, formula="{Summe Geld}/{Summe}", number_format="percent"),
    _c("Liquides Vermögen", "sum", width=18.3,
       formula=_members("Urlaubskonto", "Girokonto", "Sparkonto", "Flatex Geld", "Flatex Depot", "Kreditkarte",
                        "Bundesschatz", "Kest")),
    _c("Liquide Rate", "custom", width=12.0, formula="{Liquides Vermögen}/{Summe}", number_format="percent"),
    _c("Anlagevermögen", "sum", width=16.3,
       formula=_members("Flatex Depot", "Bundesschatz", "Bundespensionskasse", "Allianz Vorsorgekasse", "Kest")),
    _c("Anlage Rate", "custom", width=11.7, formula="{Anlagevermögen}/{Summe}", number_format="percent"),
    _c("Aktien Rate", "share", width=11.7, formula=_members("Flatex Depot", "Kest")),
    _c("Depot Gewinn Anteil", "depot_gain_share", width=19.9),
    _c("Verzinstes Vermögen", "sum", width=20.1,
       formula=_members("Sparkonto", "Flatex Depot", "Bausparer", "Bundespensionskasse", "Bundesschatz",
                        "Allianz Vorsorgekasse", "Urlaubskonto", "Kest")),
    _c("Verzinster Anteil", "custom", width=16.1, formula="{Verzinstes Vermögen}/{Summe}", number_format="percent",
       highlight="<0.85:red; <0.9:yellow"),
    _c("Änderung Anlage", "custom", width=16.4, formula="({Anlagevermögen}-{Anlagevermögen@prev})/{Summe@prev}",
       number_format="percent"),
    _c("Änderung Gesamt", "change_prev", width=17.1),
    _c("Änderung seit Start", "change_start", width=18.3),
    _c("Änderung seit Start (Abs)", "change_start_abs", width=23.4),
    _c("Kommentar", "comment", width=30.0),
]

# Lean layout for new accounts: a few accounts and a depot, the user adds their own
BASIC_COLUMNS = [
    _c("Girokonto", "asset", "Konten"),
    _c("Kreditkarte", "liability", net_into="Girokonto"),
    _c("Sparkonto", "asset", "Konten"),
    _c("Depot", "depot", "Depot"),
    _c("Depot Entwicklung", "depot_gain", width=17.7),
    _c("Kest", "tax"),
    _c("Summe", "total", width=14.0),
    _c("Änderung Gesamt", "change_prev", width=16.0),
    _c("Änderung seit Start (Abs)", "change_start_abs", width=23.4),
    _c("Kommentar", "comment", width=30.0),
]

# name -> (label, columns, groups)
LAYOUTS = {
    "basic": ("Einfach (wenige Konten, Depot, Summe)", BASIC_COLUMNS, ["Konten", "Depot"]),
    "full": ("Vollständig (Layout der ursprünglichen NetWorth.xlsx)", DEFAULT_COLUMNS,
             ["Konten", "Depot", "Vorsorge", "Anleihen"]),
}


def default_columns(layout: str = "full") -> list[Column]:
    _, columns, groups = LAYOUTS[layout]
    return build([replace(c, position=i) for i, c in enumerate(columns, start=1)], groups, DEFAULT_TAX_RATE)[0]
