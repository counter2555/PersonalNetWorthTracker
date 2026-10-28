"""Interactive income dashboard (net salary from the "Einkommen" page).

Builds one HTML page answering:

* What do I earn?              net salary of the last 12 months, monthly average, current year vs. last year
* How does my salary develop?  salary per month and per calendar year
* Am I ahead this year?        cumulative salary per year, month by month
* Where does it go?            salary per year split into saved and spent (needs net worth entries)

Page layout, themes and formatting come from ``dashboard``.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from .columns import Layout
from .dashboard import (LEGEND_BELOW, LIGHT, MONTHS_DE, THEMES, Data, Summary, Theme, _card, _de, _euro, _kpi,
                        _percent, _range_buttons, _signed_euros, _style, page_html, period_flows, period_salaries,
                        prepare_data, summarize)

RECENT_YEARS = 5  # years shown in the cumulative chart


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def monthly_income(income: pd.Series) -> pd.DataFrame:
    """Salary per month from the first to the last month with income (missing months count as 0)."""
    months = pd.period_range(income.index.min(), income.index.max(), freq="M")
    return pd.DataFrame({"Gesamt": income.reindex(months, fill_value=0.0).astype(float)})


def yearly_income(monthly: pd.DataFrame) -> pd.DataFrame:
    """Salary per calendar year with the number of covered months and the change against the previous year.

    The change compares the same calendar months, so a year that is still running (or started mid-year)
    is compared with the matching months of the year before; ``NaN`` if those aren't all covered.
    """
    years = monthly.groupby(monthly.index.year)
    table = years[["Gesamt"]].sum()
    table["Monate"] = years.size()
    table["Erster Monat"] = years.apply(lambda g: g.index.min().month)
    table["Letzter Monat"] = years.apply(lambda g: g.index.max().month)
    changes = []
    for year in table.index:
        months = monthly.index[monthly.index.year == year].month
        before = monthly[(monthly.index.year == year - 1) & monthly.index.month.isin(months)]
        base = before["Gesamt"].sum()
        changes.append(table.loc[year, "Gesamt"] / base - 1 if len(before) == len(months) and base > 0 else float("nan"))
    table["Veränderung"] = changes
    return table


def yearly_spending(df: pd.DataFrame, income: pd.Series) -> pd.DataFrame:
    """Salary per calendar year split into saved and spent; only years covered by net worth entries.

    "Gespart" is the savings part of the net worth change (as in the main dashboard), "Ausgegeben" the rest of
    the salary. Salaries count from the month after the first entry, like the savings rate.
    """
    flows = period_flows(df, "Y")
    salaries = period_salaries(df, income, "Y")
    common = flows.index.intersection(salaries.index)
    common = common[salaries.loc[common].to_numpy() > 0]
    saved = flows.loc[common, "Sparen"]
    table = pd.DataFrame({"Nettogehalt": salaries.loc[common], "Gespart": saved,
                          "Ausgegeben": salaries.loc[common] - saved})
    table["Sparquote"] = table["Gespart"] / table["Nettogehalt"]
    table.index = table.index.year
    return table


@dataclass
class IncomeSummary:
    """Key figures for the dashboard header."""
    last_month: pd.Period
    window_start: pd.Period  # first month of the "last 12 months" window
    window_months: int  # months in that window (fewer if there is less data)
    window_total: float
    previous_total: float | None  # the 12 months before, if fully covered
    year: int
    year_total: float  # current calendar year up to the last month
    year_change: float | None  # against the same months of the year before
    savings: Summary | None = None  # net worth key figures, if there are entries

    @property
    def monthly_average(self) -> float:
        return self.window_total / self.window_months

    @property
    def window_change(self) -> float | None:
        if self.previous_total is None or self.previous_total <= 0:
            return None
        return self.window_total / self.previous_total - 1


def summarize_income(monthly: pd.DataFrame, yearly: pd.DataFrame, savings: Summary | None = None) -> IncomeSummary:
    last = monthly.index[-1]
    window = monthly["Gesamt"].iloc[-12:]
    previous = monthly["Gesamt"].iloc[-24:-12]
    change = yearly.loc[last.year, "Veränderung"]
    return IncomeSummary(
        last_month=last,
        window_start=window.index[0],
        window_months=len(window),
        window_total=float(window.sum()),
        previous_total=float(previous.sum()) if len(previous) == 12 else None,
        year=last.year,
        year_total=float(yearly.loc[last.year, "Gesamt"]),
        year_change=None if pd.isna(change) else float(change),
        savings=savings,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _month(period: pd.Period) -> str:
    return f"{MONTHS_DE[period.month - 1]} {period.year}"


def _year_label(yearly: pd.DataFrame, year: int) -> str:
    """"2024", or with the covered months for an incomplete year: "2026 (bis Okt)", "2019 (ab Mär)"."""
    first, last = int(yearly.loc[year, "Erster Monat"]), int(yearly.loc[year, "Letzter Monat"])
    if first > 1 and last < 12:
        return f"{year} ({MONTHS_DE[first - 1]}–{MONTHS_DE[last - 1]})"
    if first > 1:
        return f"{year} (ab {MONTHS_DE[first - 1]})"
    if last < 12:
        return f"{year} (bis {MONTHS_DE[last - 1]})"
    return str(year)


def _change_text(change: float | None, compared: str) -> str:
    return f"{_percent(change, sign=True)} {compared}" if change is not None else ""


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def plot_monthly(monthly: pd.DataFrame, t: Theme = LIGHT) -> go.Figure:
    """Salary per month as bars, the 12-month average as a line."""
    x = monthly.index.to_timestamp()
    average = monthly["Gesamt"].rolling(12).mean()
    fig = go.Figure([
        go.Bar(x=x, y=monthly["Gesamt"], name="Nettogehalt", marker_color=t.group_palette[0],
               hovertemplate="Nettogehalt: %{y:,.0f} €<extra></extra>"),
        go.Scatter(x=x, y=average, name="Ø 12 Monate", mode="lines", line=dict(width=2, color=t.ink),
                   hovertemplate="<b>Ø 12 Monate: %{y:,.0f} €</b><extra></extra>"),
    ])
    _style(fig, t, legend=dict(traceorder="normal"))
    fig.update_layout(bargap=0.25, hovermode="x unified")
    fig.update_xaxes(hoverformat="%B %Y", dtick="M3" if len(monthly) > 12 else "M1", tickformat="%b %Y")
    if len(monthly) > 24:  # show the last two years, older months are reachable via "Alle" / panning
        fig.update_xaxes(range=[x[-24] - pd.Timedelta(days=16), x[-1] + pd.Timedelta(days=16)])
    _range_buttons(fig, t, months=(12, 24))
    return fig


def plot_yearly(yearly: pd.DataFrame, t: Theme = LIGHT) -> go.Figure:
    """Salary per calendar year with the total and the change against the previous year on top."""
    labels = [_year_label(yearly, year) for year in yearly.index]
    fig = go.Figure(go.Bar(x=labels, y=yearly["Gesamt"], name="Nettogehalt", marker_color=t.group_palette[0],
                           width=0.55, hovertemplate="Nettogehalt: %{y:,.0f} €<extra></extra>"))
    for i, (year, row) in enumerate(yearly.iterrows()):
        text = f"<b>{_euro(row['Gesamt'])}</b>"
        if not pd.isna(row["Veränderung"]):
            text += f"<br>{_percent(row['Veränderung'], sign=True)}"
        # x as category index: a label like "2024" would be read as position 2024 on the category axis
        fig.add_annotation(x=i, y=row["Gesamt"], text=text, showarrow=False, yanchor="bottom", yshift=4,
                           font=dict(color=t.ink))
    _style(fig, t)
    fig.update_layout(showlegend=False, hovermode="x unified")
    fig.update_xaxes(type="category", range=[-0.5, len(labels) - 0.5], tickfont=dict(color=t.ink_secondary))
    high = float(yearly["Gesamt"].max()) if len(yearly) else 1.0
    fig.update_yaxes(range=[0, high * 1.25 or 1])
    return fig


def plot_cumulative(monthly: pd.DataFrame, t: Theme = LIGHT) -> go.Figure:
    """Cumulative salary per calendar year over the months; the current year is drawn on top in the ink color."""
    years = sorted(set(monthly.index.year))[-RECENT_YEARS:]
    base = t.group_palette[0]
    fig = go.Figure()
    for age, year in enumerate(reversed(years)):  # newest first, so it gets the strongest color
        values = monthly.loc[monthly.index.year == year, "Gesamt"]
        months = [MONTHS_DE[p.month - 1] for p in values.index]
        newest = age == 0
        color = t.ink if newest else t.tint(base, t.tint_step(age - 1))
        fig.add_trace(go.Scatter(x=months, y=values.cumsum(), name=str(year), mode="lines+markers" if newest else "lines",
                                 line=dict(width=2.5 if newest else 1.5, color=color), marker=dict(size=5),
                                 hovertemplate=f"{year}: %{{y:,.0f}} €<extra></extra>"))
    _style(fig, t, legend=dict(traceorder="normal", **LEGEND_BELOW))
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=MONTHS_DE, range=[-0.3, 11.3])
    return fig


def plot_spending(spending: pd.DataFrame, yearly: pd.DataFrame, t: Theme = LIGHT) -> go.Figure:
    """Salary per year split into saved and spent, with the savings rate on top."""
    labels = [_year_label(yearly, year) if year in yearly.index else str(year) for year in spending.index]
    fig = go.Figure([
        go.Bar(x=labels, y=spending["Gespart"], name="Gespart", marker_color=t.savings, width=0.55,
               customdata=_signed_euros(spending["Gespart"]), hovertemplate="Gespart: %{customdata}<extra></extra>"),
        go.Bar(x=labels, y=spending["Ausgegeben"], name="Ausgegeben", marker_color=t.ink_muted, width=0.55,
               customdata=_signed_euros(spending["Ausgegeben"]),
               hovertemplate="Ausgegeben: %{customdata}<extra></extra>"),
    ])
    tops = spending["Gespart"].clip(lower=0) + spending["Ausgegeben"].clip(lower=0)
    for i, (year, top) in enumerate(zip(spending.index, tops)):
        text = f"<b>Sparquote {_percent(spending.loc[year, 'Sparquote'])}</b>"
        fig.add_annotation(x=i, y=top, text=text, showarrow=False, yanchor="bottom", yshift=4, font=dict(color=t.ink))
    _style(fig, t, legend=dict(traceorder="normal", **LEGEND_BELOW))
    fig.update_layout(barmode="relative", hovermode="x unified")
    fig.update_xaxes(type="category", range=[-0.5, len(labels) - 0.5], tickfont=dict(color=t.ink_secondary))
    low = min(0.0, float((spending["Gespart"].clip(upper=0) + spending["Ausgegeben"].clip(upper=0)).min()))
    high = float(tops.max()) if len(tops) else 1.0
    pad = (high - low) * 0.2 or 1
    fig.update_yaxes(range=[low - (pad / 3 if low < 0 else 0), high + pad], zeroline=True, zerolinecolor=t.baseline)
    return fig


def create_figures(monthly: pd.DataFrame, yearly: pd.DataFrame, spending: pd.DataFrame | None,
                   t: Theme = LIGHT) -> dict[str, go.Figure]:
    figures = {
        "income-monthly": plot_monthly(monthly, t),
        "income-yearly": plot_yearly(yearly, t),
        "income-cumulative": plot_cumulative(monthly, t),
    }
    if spending is not None and len(spending):
        figures["income-spending"] = plot_spending(spending, yearly, t)
    return figures


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _kpis(summary: IncomeSummary) -> str:
    window = f"{_month(summary.window_start)} – {_month(summary.last_month)}"
    months = "12 Monate" if summary.window_months == 12 else f"{summary.window_months} Monate"
    tiles = [
        _kpi(f"Nettogehalt letzte {months}", _euro(summary.window_total),
             _change_text(summary.window_change, "ggü. den 12 Monaten davor") or window,
             f"Summe der Einträge auf der Seite „Einkommen“ von {window}.", hero=True),
        _kpi("Ø pro Monat", _euro(summary.monthly_average), f"Ø der letzten {months}",
             f"Nettogehalt der letzten {months} ÷ {summary.window_months}."),
        _kpi(f"Jahr {summary.year} bis {MONTHS_DE[summary.last_month.month - 1]}", _euro(summary.year_total),
             _change_text(summary.year_change, "ggü. denselben Monaten im Vorjahr") or "kein Vorjahr zum Vergleich",
             "Summe seit Jahresbeginn bis zum letzten Monat mit Gehalt."),
    ]
    savings = summary.savings
    if savings is not None and savings.savings_rate_year is not None and savings.salary_year:
        spent = savings.salary_year - savings.saved_year
        tiles.append(_kpi("Sparquote", _percent(savings.savings_rate_year),
                          f"{_euro(savings.saved_year)} gespart · {_euro(spent)} ausgegeben",
                          f"Wie im Vermögens-Dashboard: gespart seit {savings.year_start:%d.%m.%Y} ÷ Nettogehälter "
                          "derselben Monate. Ausgegeben = Nettogehalt minus gespart."))
    return "".join(tiles)


def _table(yearly: pd.DataFrame, spending: pd.DataFrame | None) -> str:
    rows = []
    for year, row in yearly.iloc[::-1].iterrows():
        cells = [_year_label(yearly, year), _de(row["Gesamt"]),
                 _de(row["Gesamt"] / row["Monate"]),
                 "" if pd.isna(row["Veränderung"]) else _percent(row["Veränderung"], sign=True)]
        if spending is not None:
            saved = spending.loc[year] if year in spending.index else None
            cells += ["", ""] if saved is None else [_de(saved["Gespart"]), _percent(saved["Sparquote"])]
        rows.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>")
    head = ["Jahr", "Nettogehalt", "Ø pro Monat", "ggü. Vorjahr"]
    if spending is not None:
        head += ["Gespart", "Sparquote"]
    return (f"<table><thead><tr>{''.join(f'<th>{h}</th>' for h in head)}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def build_dashboard(income: pd.Series, data: Data | None = None, savings: Summary | None = None,
                    plotly_src: str | None = None, nav: str = "") -> str:
    """Render the income dashboard as one HTML page (``plotly_src`` and ``nav`` as in ``dashboard.page_html``).

    ``data`` and ``savings`` (prepared net worth entries and their key figures) add the savings rate and the
    split of the salary into saved and spent.
    """
    monthly = monthly_income(income)
    yearly = yearly_income(monthly)
    spending = yearly_spending(data.df, income) if data is not None else None
    summary = summarize_income(monthly, yearly, savings)

    themed = {name: create_figures(monthly, yearly, spending, theme) for name, theme in THEMES.items()}
    figures = {chart_id: {name: themed[name][chart_id] for name in THEMES} for chart_id in themed["light"]}

    cards = [
        _card("Nettogehalt pro Monat", "Summe der Einträge je Kalendermonat; Monate ohne Eintrag zählen als 0. "
              "Die Linie ist der Durchschnitt der letzten 12 Monate.", "income-monthly", wide=True),
        _card("Jahreseinkommen", "Nettogehalt pro Kalenderjahr. Die Veränderung vergleicht dieselben Monate des "
              "Vorjahres, ein laufendes Jahr also nur bis zum aktuellen Monat.", "income-yearly"),
        _card("Jahresverlauf", f"Aufsummiertes Nettogehalt seit Jahresbeginn, Monat für Monat, für die letzten "
              f"{RECENT_YEARS} Jahre. Liegt das aktuelle Jahr (hervorgehobene Linie) darüber, verdienst du mehr als zuvor.",
              "income-cumulative"),
    ]
    if "income-spending" in figures:
        cards.append(_card("Wohin geht das Einkommen?", "Nettogehalt pro Kalenderjahr, aufgeteilt in gespart und "
                           "ausgegeben. Gespart = Vermögenszuwachs ohne Kursgewinne wie im Vermögens-Dashboard (enthält "
                           "auch Zinsen und Vorsorgebeiträge), ausgegeben = der Rest des Nettogehalts. Gehälter zählen "
                           "erst ab dem Monat nach dem ersten Vermögens-Eintrag.", "income-spending", wide=True))

    table = (f'<details><summary>Jahrestabelle ({len(yearly)} Jahre)</summary>'
             f'<div class="table-wrap">{_table(yearly, spending)}</div></details>')
    return page_html("Einkommens-Dashboard", _kpis(summary), cards, figures, plotly_src, nav, table)


def render(income: pd.Series, sheet: pd.DataFrame | None = None, layout: Layout | None = None,
           plotly_src: str | None = None, nav: str = "") -> str:
    """``sheet`` and ``layout``: net worth entries for the savings figures; leave out if there are none."""
    data = savings = None
    if sheet is not None and layout is not None and len(sheet):
        data = prepare_data(sheet, layout)
        savings = summarize(data.df, income)
    return build_dashboard(income, data, savings, plotly_src, nav)
