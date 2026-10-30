"""Interactive net worth dashboard (ported from visualize_net_worth.py).

Builds one HTML page answering:

* Where do I stand?            key figures, progress towards the target
* How did my wealth develop?   stacked net worth per asset group with comments
* Where does the growth come from?  monthly and yearly change split into savings and market gains
* Where is my money?           current distribution and shares over time
* How is the depot doing?      paid-in capital vs. market value
* When do I reach my target?   projection with the current savings rate and different returns

Which column is an account, the total, the depot etc. comes from the column configuration (``Layout``).

Sections: data preparation and analysis, colors and formatting, figures, dashboard.
"""
from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from .columns import DATE_COLUMN, Layout

MONTHS_DE = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

DEFAULT_TARGET_VALUE: float = 1_000_000  # target value in Euro

NET_GAINS = "Net Aktien Gewinne"  # positive depot gains after tax, stacked on top of the depot

# internal columns derived by prepare_data (prefixed, so they can't clash with configured column names)
TOTAL, COMMENT = "_total", "_comment"
DEPOT_VALUE, DEPOT_PAID_IN, MARKET_GAIN, TAX = "_depot_value", "_depot_paid_in", "_market_gain", "_tax"

# Projection scenarios: yearly return on the whole net worth
SCENARIOS = {"Ohne Rendite": 0.0, "4 % Rendite p.a.": 0.04, "7 % Rendite p.a.": 0.07}
MAX_PROJECTION_MONTHS = 50 * 12


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

@dataclass
class Data:
    """Prepared sheet plus what the figures need to know about its columns."""
    df: pd.DataFrame
    groups: dict[str, list[str]]  # plotted categories per asset group, stack order bottom to top
    has_depot: bool
    names: dict[str, str] = field(default_factory=dict)  # role -> configured column name, for explanations
    net_into: dict[str, str] = field(default_factory=dict)  # booked column -> account

    @property
    def stack_order(self) -> list[str]:
        return [col for cols in self.groups.values() for col in cols]

    @property
    def group_of(self) -> dict[str, str]:
        return {col: group for group, cols in self.groups.items() for col in cols}


def prepare_data(sheet: pd.DataFrame, layout: Layout) -> Data:
    """Derive the plotted categories so that they add up to the total.

    * Columns with ``net_into`` (e.g. a negative credit card balance) are booked against their account.
    * Positive depot gains are split off the depot value into "Net Aktien Gewinne"
      (gains minus capital gains tax), so the depot column shows the paid-in capital.
      Losses stay inside the depot value, since a negative stack layer can't be drawn.
    * ``_market_gain`` is the accumulated depot gain after tax (also negative), ``_depot_value``
      and ``_depot_paid_in`` are the depot's market value and paid-in capital.
    """
    df = sheet.dropna(subset=[DATE_COLUMN]).sort_values(DATE_COLUMN, kind="stable").reset_index(drop=True).copy()
    roles = layout.roles

    def numbers(name: str | None) -> pd.Series:
        if name is None or name not in df:
            return pd.Series(0.0, index=df.index)
        return df[name].astype(float).fillna(0)

    groups = {g: list(cols) for g, cols in layout.groups.items()}
    accounts = [c for cols in groups.values() for c in cols]
    for name in accounts:
        df[name] = numbers(name)
    for source, target in layout.net_into.items():
        if target in df:
            df[target] = numbers(target) + numbers(source)

    if "total" in roles:
        df[TOTAL] = numbers(roles["total"])
    else:  # no total configured: sum of the plotted accounts
        df[TOTAL] = df[accounts].sum(axis=1) if accounts else 0.0
    df[COMMENT] = df[roles["comment"]] if "comment" in roles else pd.NA

    depot = roles.get("depot_value")
    has_depot = depot is not None and depot in accounts
    development = numbers(roles.get("depot_gain"))
    df[TAX] = numbers(roles.get("tax"))
    df[DEPOT_VALUE] = numbers(depot)
    df[DEPOT_PAID_IN] = df[DEPOT_VALUE] - development
    df[MARKET_GAIN] = development + df[TAX]

    if has_depot:
        gains = development.clip(lower=0)
        df[NET_GAINS] = gains + df[TAX]
        df[depot] = df[depot] - gains
        group = next(g for g, cols in groups.items() if depot in cols)
        groups[group].insert(groups[group].index(depot) + 1, NET_GAINS)

    return Data(df=df, groups=groups, has_depot=has_depot, names=dict(roles), net_into=dict(layout.net_into))


def load_income(income: Sequence[tuple[pd.Timestamp, float]]) -> pd.Series | None:
    """Net salary per month (index: monthly Period); ``None`` without data."""
    if not income:
        return None
    raw = pd.DataFrame(income, columns=["Datum", "Nettogehalt"])
    return raw.groupby(pd.to_datetime(raw["Datum"]).dt.to_period("M"))["Nettogehalt"].sum()


# ---------------------------------------------------------------------------
# Changes over time
# ---------------------------------------------------------------------------

def value_near(df: pd.DataFrame, date: pd.Timestamp, column: str = TOTAL) -> float:
    """Value of ``column`` in the row whose date is closest to ``date``."""
    idx = (df[DATE_COLUMN] - date).abs().idxmin()
    return float(df.loc[idx, column])


def period_flows(df: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """Net worth change per calendar period, split into market gains and savings.

    "Kursgewinne" is the change of the accumulated depot gain after tax, "Sparen" is everything
    else (deposits, interest, pension contributions, ...). The first period starts at the first
    snapshot; periods without a snapshot are folded into the next one.
    """
    snapshots = df.set_index(DATE_COLUMN)[[TOTAL, MARKET_GAIN]]
    end = snapshots.groupby(snapshots.index.to_period(freq)).last()
    start = end.shift(1)
    start.iloc[0] = snapshots.iloc[0]

    flows = pd.DataFrame({"Gesamt": end[TOTAL] - start[TOTAL],
                          "Kursgewinne": end[MARKET_GAIN] - start[MARKET_GAIN]})
    flows["Sparen"] = flows["Gesamt"] - flows["Kursgewinne"]
    flows["Stand"] = end[TOTAL]
    return flows


def period_salaries(df: pd.DataFrame, income: pd.Series, freq: str = "M") -> pd.Series:
    """Net salary per period, counting only months fully covered by snapshots (after the first snapshot's month)."""
    first, last = df[DATE_COLUMN].iloc[0].to_period("M"), df[DATE_COLUMN].iloc[-1].to_period("M")
    income = income[(income.index > first) & (income.index <= last)]
    return income.groupby(income.index.asfreq(freq)).sum()


def savings_rates(df: pd.DataFrame, income: pd.Series | None, freq: str = "M") -> pd.Series:
    """Savings / net salary per period (empty without income data), salaries as in ``period_salaries``."""
    if income is None:
        return pd.Series(dtype=float)
    flows = period_flows(df, freq)
    salaries = period_salaries(df, income, freq)
    common = flows.index.intersection(salaries.index)
    common = common[salaries.loc[common].to_numpy() > 0]
    return flows.loc[common, "Sparen"] / salaries.loc[common]


def salary_between(income: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Net salary of the months after ``start``'s month up to and including ``end``'s month."""
    months = pd.period_range(start, end, freq="M")[1:]
    return float(income.reindex(months).sum())


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def project(start: float, monthly_saving: float, annual_return: float, target: float,
            max_months: int = MAX_PROJECTION_MONTHS) -> list[float]:
    """Monthly values from ``start`` until ``target`` is reached (or ``max_months``)."""
    monthly_return = (1 + annual_return) ** (1 / 12) - 1
    values = [start]
    while values[-1] < target and len(values) <= max_months:
        values.append(values[-1] * (1 + monthly_return) + monthly_saving)
    return values


def months_until(start: float, monthly_saving: float, annual_return: float, target: float,
                 max_months: int = MAX_PROJECTION_MONTHS) -> int | None:
    """Months until ``target`` is reached; ``None`` if not within ``max_months``."""
    values = project(start, monthly_saving, annual_return, target, max_months)
    return len(values) - 1 if values[-1] >= target else None


# ---------------------------------------------------------------------------
# Key figures
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    """Key figures for the dashboard header."""
    date: pd.Timestamp
    net_worth: float
    change_month: float
    change_year: float
    saved_year: float
    market_year: float
    year_start: pd.Timestamp  # snapshot closest to one year before ``date``
    year_months: float  # length of the "last year" window in months (shorter if there is less data)
    savings_rate_year: float | None
    salary_year: float | None  # net salary in the same window (None without income data)
    depot_value: float  # market value before capital gains tax
    depot_paid_in: float
    depot_tax: float  # accrued capital gains tax (KESt, <= 0)
    target: float
    months_to_target: dict[str, int | None] = field(default_factory=dict)

    @property
    def monthly_saving(self) -> float:
        return self.saved_year / self.year_months if self.year_months > 0 else 0.0

    @property
    def depot_gain(self) -> float:
        """Gain after capital gains tax, consistent with the market gains in the flows."""
        return self.depot_value + self.depot_tax - self.depot_paid_in

    @property
    def depot_return(self) -> float | None:
        return self.depot_gain / self.depot_paid_in if self.depot_paid_in > 0 else None

    @property
    def target_progress(self) -> float:
        return self.net_worth / self.target if self.target > 0 else 0.0


def summarize(df: pd.DataFrame, income: pd.Series | None = None,
              target: float = DEFAULT_TARGET_VALUE) -> Summary:
    last = df.iloc[-1]
    year_ago_idx = (df[DATE_COLUMN] - (last[DATE_COLUMN] - pd.DateOffset(years=1))).abs().idxmin()
    year_ago = df.loc[year_ago_idx]

    change_year = float(last[TOTAL] - year_ago[TOTAL])
    market_year = float(last[MARKET_GAIN] - year_ago[MARKET_GAIN])
    year_months = (last[DATE_COLUMN] - year_ago[DATE_COLUMN]).days / (365.25 / 12)

    rate = salary = None
    if income is not None:
        salary = salary_between(income, year_ago[DATE_COLUMN], last[DATE_COLUMN])
        rate = (change_year - market_year) / salary if salary > 0 else None

    summary = Summary(
        date=last[DATE_COLUMN],
        net_worth=float(last[TOTAL]),
        change_month=float(last[TOTAL]) - value_near(df, last[DATE_COLUMN] - pd.DateOffset(months=1)),
        change_year=change_year,
        saved_year=change_year - market_year,
        market_year=market_year,
        year_start=year_ago[DATE_COLUMN],
        year_months=year_months,
        savings_rate_year=rate,
        salary_year=salary,
        depot_value=float(last[DEPOT_VALUE]),
        depot_paid_in=float(last[DEPOT_PAID_IN]),
        depot_tax=float(last[TAX]),
        target=target,
    )
    summary.months_to_target = {name: months_until(summary.net_worth, summary.monthly_saving, r, target)
                                for name, r in SCENARIOS.items()}
    return summary


# ---------------------------------------------------------------------------
# Colors and formatting
# ---------------------------------------------------------------------------

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _tint(color: str, amount: float, towards: str = "#ffffff") -> str:
    """Mix ``amount`` (0..1) of ``towards`` into ``color``."""
    rgb, target = (np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], dtype=float) for c in (color, towards))
    mixed = np.round(rgb + (target - rgb) * amount).astype(int)
    return "#" + "".join(f"{c:02x}" for c in mixed)


@dataclass(frozen=True)
class Theme:
    """Chart and page colors. Both themes use the same hues, stepped for their surface (validated palettes)."""
    name: str
    surface: str
    page: str
    ink: str
    ink_secondary: str
    ink_muted: str
    grid: str
    baseline: str
    border: str
    group_palette: tuple[str, ...]  # one hue per asset group, in stacking order
    savings: str  # savings come from the accounts
    market: str  # market gains come from the depot
    scenario_ramp: tuple[str, str, str]  # ordinal one-hue ramp, more return = more contrast to the surface
    tint_steps: tuple[float, ...]  # share of the surface mixed into the group hue for the 1st, 2nd, ... account

    def tint(self, color: str, amount: float) -> str:
        """Lighter (light theme) or darker (dark theme) step of ``color``: mixed towards the surface."""
        return _tint(color, amount, towards=self.surface)

    def tint_step(self, index: int) -> float:
        steps = self.tint_steps
        return steps[index] if index < len(steps) else min(0.85, steps[-1] + 0.05 * (index - len(steps) + 1))

    def group_colors(self, data: Data) -> dict[str, str]:
        return {group: self.group_palette[i % len(self.group_palette)] for i, group in enumerate(data.groups)}

    def category_colors(self, data: Data) -> dict[str, str]:
        groups = self.group_colors(data)
        return {col: self.tint(groups[group], self.tint_step(i))
                for group, cols in data.groups.items() for i, col in enumerate(cols)}

    @property
    def scenario_colors(self) -> dict[str, str]:
        return dict(zip(SCENARIOS, self.scenario_ramp))

    @property
    def annotation_background(self) -> str:
        r, g, b = (int(self.surface[i:i + 2], 16) for i in (1, 3, 5))
        return f"rgba({r},{g},{b},0.85)"


LIGHT = Theme(
    name="light", surface="#fcfcfb", page="#f9f9f7", ink="#0b0b0b", ink_secondary="#52514e", ink_muted="#898781",
    grid="#e1e0d9", baseline="#c3c2b7", border="rgba(11,11,11,0.08)",
    group_palette=("#1baf7a", "#eda100", "#2a78d6", "#eb6834", "#8a5cd6", "#d6457a", "#13a3b5", "#7a8a2a"),
    savings="#2a78d6", market="#eb6834",
    scenario_ramp=("#86b6ef", "#2a78d6", "#104281"),
    tint_steps=(0.0, 0.35, 0.55, 0.7),
)
DARK = Theme(
    name="dark", surface="#1a1a19", page="#0d0d0d", ink="#f0efec", ink_secondary="#c3c2b7", ink_muted="#898781",
    grid="#2c2c2a", baseline="#383835", border="rgba(255,255,255,0.10)",
    group_palette=("#199e70", "#c98500", "#3987e5", "#d95926", "#9a6fe0", "#e05a8a", "#1fb3c4", "#8c9c35"),
    savings="#3987e5", market="#d95926",
    scenario_ramp=("#1c5cab", "#3987e5", "#9ec5f4"),
    tint_steps=(0.0, 0.25, 0.4, 0.5),  # mixing towards a dark surface loses contrast faster
)
THEMES = {theme.name: theme for theme in (LIGHT, DARK)}

BASE_FONT_SIZE = 12  # px at <= 1700px window width, scaled up by the dashboard script

# The mode bar would cover the range buttons in the top right corner; drag to zoom and double-click to reset still work.
PLOTLY_CONFIG = {"displaylogo": False, "responsive": True, "displayModeBar": False}


def _de(value: float, fmt: str = ",.0f") -> str:
    """Format a number with German separators (1.234,5)."""
    return format(value, fmt).replace(",", "_").replace(".", ",").replace("_", ".")


def _euro(value: float, sign: bool = False) -> str:
    return f"{_de(value, '+,.0f' if sign else ',.0f')} €"


def _percent(share: float, sign: bool = False) -> str:
    if not sign and 0 < share < 0.01:
        return "<1 %"
    return f"{_de(share * 100, '+.0f' if sign else '.0f')} %"


def _reach_date(summary: Summary, months: int | None) -> str:
    if months is None:
        return "nicht in 50 Jahren"
    if months == 0:
        return "erreicht"
    date = summary.date + pd.DateOffset(months=months)
    return f"{MONTHS_DE[date.month - 1]} {date.year}"


def group_totals(data: Data) -> pd.DataFrame:
    return pd.DataFrame({group: data.df[cols].clip(lower=0).sum(axis=1) for group, cols in data.groups.items()},
                        index=data.df.index)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

def _style(fig: go.Figure, t: Theme, legend: dict | None = None) -> go.Figure:
    # no fixed size: the figure fills its card, which is sized by the page CSS
    fig.update_layout(
        template="none",  # all colors come from the theme; plotly's default template would ship its own
        autosize=True,
        paper_bgcolor=t.surface,
        plot_bgcolor=t.surface,
        font=dict(family=FONT, size=BASE_FONT_SIZE, color=t.ink_secondary),
        separators=",.",  # German decimal and thousands separators
        margin=dict(l=8, r=8, t=8, b=8),
        hoverlabel=dict(bgcolor=t.surface, bordercolor=t.grid, font=dict(family=FONT, color=t.ink)),
        legend={**dict(orientation="h", x=0, y=1.02, xanchor="left", yanchor="bottom"), **(legend or {})},
    )
    # automargin is on in plotly's default template only; without it tick labels get clipped
    fig.update_xaxes(automargin=True, showgrid=False, showline=True, linecolor=t.baseline, ticks="",
                     tickfont=dict(color=t.ink_muted))
    fig.update_yaxes(automargin=True, gridcolor=t.grid, gridwidth=1, zeroline=False, ticks="",
                     tickfont=dict(color=t.ink_muted), tickformat=",.0f", ticksuffix=" €", rangemode="tozero")
    return fig


# narrow cards: the legend goes below the plot, so it doesn't collide with the range buttons above
LEGEND_BELOW = dict(y=-0.12, yanchor="top")


def _range_buttons(fig: go.Figure, t: Theme, months: Sequence[int] = (6, 12, 24)) -> None:
    """Range buttons right-aligned above the plot (the legend sits on the left)."""
    buttons = [dict(count=m, label=f"{m // 12}J" if m % 12 == 0 else f"{m}M", step="month", stepmode="backward")
               for m in months]
    fig.update_xaxes(rangeselector=dict(
        buttons=[*buttons, dict(step="all", label="Alle")], bgcolor=t.page, activecolor=t.grid,
        font=dict(color=t.ink_secondary), x=1, xanchor="right", y=1.02, yanchor="bottom"))


def _stack_trace(x, y, name: str, color: str, stack: str, t: Theme,
                 hovertemplate: str = "%{y:,.0f} €") -> go.Scatter:
    return go.Scatter(x=x, y=y, name=name, stackgroup=stack, mode="lines", line=dict(width=0.6, color=t.surface),
                      fillcolor=color, hovertemplate=hovertemplate)


def plot_wealth(data: Data, t: Theme = LIGHT) -> go.Figure:
    """Net worth stacked per asset group, total as a line, comments as annotations."""
    df = data.df
    colors = t.group_colors(data)
    fig = go.Figure()
    for group, values in group_totals(data).items():
        fig.add_trace(_stack_trace(df[DATE_COLUMN], values, group, colors[group], "wealth", t))
    fig.add_trace(go.Scatter(x=df[DATE_COLUMN], y=df[TOTAL], name="Gesamt", mode="lines",
                             line=dict(width=2, color=t.ink), hovertemplate="<b>%{y:,.0f} €</b>"))

    comments = df[df[COMMENT].notna() & (df[COMMENT].astype(str).str.strip() != "")]
    for level, (_, row) in enumerate(comments.iterrows()):
        fig.add_annotation(x=row[DATE_COLUMN], y=row[TOTAL], text=html.escape(str(row[COMMENT]).strip()),
                           ax=0, ay=-28 - 16 * (level % 3), showarrow=True, arrowhead=0, arrowwidth=1,
                           arrowcolor=t.ink_muted, font=dict(color=t.ink_secondary), bgcolor=t.annotation_background)

    _style(fig, t, legend=dict(traceorder="reversed"))  # legend order = stack order from top
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(hoverformat="%d.%m.%Y")
    _range_buttons(fig, t)
    return fig


def _signed_euros(values) -> list[str]:
    """Pre-formatted hover values: plotly.js ignores signed formats like "%{y:+,.0f}" and prints the raw float."""
    return [_euro(value, sign=True) for value in values]


def plot_monthly_flows(data: Data, t: Theme = LIGHT) -> go.Figure:
    """Change per month, split into savings and market gains; the diamond marks the total."""
    flows = period_flows(data.df, "M")
    x = flows.index.to_timestamp()
    fig = go.Figure([
        go.Bar(x=x, y=flows["Sparen"], name="Gespart", marker_color=t.savings,
               customdata=_signed_euros(flows["Sparen"]), hovertemplate="Gespart: %{customdata}<extra></extra>"),
        go.Bar(x=x, y=flows["Kursgewinne"], name="Kursgewinne", marker_color=t.market,
               customdata=_signed_euros(flows["Kursgewinne"]),
               hovertemplate="Kursgewinne: %{customdata}<extra></extra>"),
        go.Scatter(x=x, y=flows["Gesamt"], name="Gesamt", mode="markers",
                   marker=dict(symbol="diamond", size=8, color=t.ink, line=dict(width=1.5, color=t.surface)),
                   customdata=_signed_euros(flows["Gesamt"]),
                   hovertemplate="<b>Gesamt: %{customdata}</b><extra></extra>"),
    ])
    _style(fig, t)
    fig.update_layout(barmode="relative", bargap=0.25, hovermode="x unified")
    fig.update_xaxes(hoverformat="%B %Y", dtick="M3" if len(flows) > 12 else "M1", tickformat="%b %Y")
    fig.update_yaxes(rangemode="normal", zeroline=True, zerolinecolor=t.baseline, tickprefix="", ticksuffix=" €")
    if len(flows) > 24:  # show the last two years, older months are reachable via "Alle" / panning
        fig.update_xaxes(range=[x[-24] - pd.Timedelta(days=16), x[-1] + pd.Timedelta(days=16)])
    _range_buttons(fig, t, months=(12, 24))
    return fig


def plot_yearly_flows(data: Data, income: pd.Series | None, t: Theme = LIGHT) -> go.Figure:
    """Change per calendar year (savings + market gains) with the total and the savings rate on top."""
    df = data.df
    flows = period_flows(df, "Y")
    rates = savings_rates(df, income, "Y")
    last = df[DATE_COLUMN].iloc[-1]
    labels = [str(p.year) for p in flows.index]
    if last.month < 12:
        labels[-1] = f"{last.year} (bis {MONTHS_DE[last.month - 1]})"

    fig = go.Figure([
        go.Bar(x=labels, y=flows["Sparen"], name="Gespart", marker_color=t.savings, width=0.55,
               customdata=_signed_euros(flows["Sparen"]), hovertemplate="Gespart: %{customdata}<extra></extra>"),
        go.Bar(x=labels, y=flows["Kursgewinne"], name="Kursgewinne", marker_color=t.market, width=0.55,
               customdata=_signed_euros(flows["Kursgewinne"]),
               hovertemplate="Kursgewinne: %{customdata}<extra></extra>"),
    ])
    tops = flows["Sparen"].clip(lower=0) + flows["Kursgewinne"].clip(lower=0)
    for i, (period, top) in enumerate(zip(flows.index, tops)):
        text = f"<b>{_euro(flows.loc[period, 'Gesamt'], sign=True)}</b>"
        if period in rates.index:
            text += f"<br>Sparquote {_percent(rates[period])}"
        # x as category index: a label like "2024" would be read as position 2024 on the category axis
        fig.add_annotation(x=i, y=top, text=text, showarrow=False, yanchor="bottom", yshift=4,
                           font=dict(color=t.ink))
    _style(fig, t)
    fig.update_layout(barmode="relative", hovermode="x unified")
    fig.update_xaxes(type="category", range=[-0.5, len(labels) - 0.5], tickfont=dict(color=t.ink_secondary))
    low = min(0.0, float((flows["Sparen"].clip(upper=0) + flows["Kursgewinne"].clip(upper=0)).min()))
    high = float(tops.max()) if len(tops) else 1.0
    pad = (high - low) * 0.25 or 1
    fig.update_yaxes(range=[low - (pad / 3 if low < 0 else 0), high + pad], zeroline=True, zerolinecolor=t.baseline)
    return fig


def plot_distribution(data: Data, t: Theme = LIGHT) -> go.Figure:
    now = data.df.iloc[-1][data.stack_order].astype(float)
    now = now[now > 0].sort_values()
    total = now.sum()
    colors, group_of = t.category_colors(data), data.group_of

    fig = go.Figure(go.Bar(
        x=now.values, y=now.index, orientation="h", marker=dict(color=[colors[c] for c in now.index]),
        text=[f"{_euro(v)} · {_percent(v / total)}" for v in now.values], textposition="outside", cliponaxis=False,
        textfont=dict(color=t.ink_secondary), width=0.6,
        customdata=[group_of[c] for c in now.index], hovertemplate="%{y} (%{customdata})<br>%{x:,.2f} €<extra></extra>",
    ))
    _style(fig, t)
    fig.update_xaxes(visible=False, range=[0, now.max() * 1.8] if len(now) else None)
    fig.update_yaxes(showgrid=False, ticksuffix="  ", tickformat="", tickfont=dict(color=t.ink_secondary))
    fig.update_layout(showlegend=False)
    return fig


def plot_shares(data: Data, t: Theme = LIGHT) -> go.Figure:
    """Share of each asset group in percent over time."""
    df = data.df
    groups = group_totals(data)
    colors = t.group_colors(data)
    shares = groups.div(groups.sum(axis=1).replace(0, np.nan), axis=0).fillna(0) * 100
    fig = go.Figure([_stack_trace(df[DATE_COLUMN], shares[group], group, colors[group], "shares", t,
                                  hovertemplate="%{y:.0f} %") for group in shares])
    _style(fig, t, legend=dict(traceorder="reversed", **LEGEND_BELOW))
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(hoverformat="%d.%m.%Y")
    fig.update_yaxes(range=[0, 100], tickformat=".0f", ticksuffix=" %", dtick=25)
    _range_buttons(fig, t)
    return fig


def plot_depot(data: Data, t: Theme = LIGHT) -> go.Figure:
    """Paid-in capital as area, market value as line; the gap between them is the gain."""
    df = data.df
    depot = df[(df[DEPOT_VALUE] > 0) | (df[DEPOT_PAID_IN] > 0)]
    gain = depot[DEPOT_VALUE] - depot[DEPOT_PAID_IN]
    ratio = gain / depot[DEPOT_PAID_IN].where(depot[DEPOT_PAID_IN] > 0)
    gain_text = [f"{euro} ({_de(share * 100, '+.1f')} %)" for euro, share in zip(_signed_euros(gain), ratio.fillna(0))]

    fig = go.Figure([
        go.Scatter(x=depot[DATE_COLUMN], y=depot[DEPOT_PAID_IN], name="Eingezahlt", mode="lines",
                   line=dict(width=1, color=t.category_colors(data)[NET_GAINS]), fill="tozeroy",
                   fillcolor=t.tint(t.market, 0.75), hovertemplate="Eingezahlt: %{y:,.0f} €<extra></extra>"),
        go.Scatter(x=depot[DATE_COLUMN], y=depot[DEPOT_VALUE], name="Depotwert", mode="lines",
                   line=dict(width=2, color=t.market), customdata=gain_text,
                   hovertemplate="<b>Depotwert: %{y:,.0f} €</b><br>Gewinn: %{customdata}<extra></extra>"),
    ])
    _style(fig, t, legend=LEGEND_BELOW)
    fig.update_layout(hovermode="x unified")
    fig.update_xaxes(hoverformat="%d.%m.%Y")
    _range_buttons(fig, t)
    return fig


def plot_projection(data: Data, summary: Summary, t: Theme = LIGHT) -> go.Figure:
    """History plus projections with the current monthly savings and different returns."""
    df = data.df
    fig = go.Figure(go.Scatter(x=df[DATE_COLUMN], y=df[TOTAL], name="Bisher", mode="lines",
                               line=dict(width=2, color=t.ink), hovertemplate="%{y:,.0f} €<extra>Bisher</extra>"))
    end = df[DATE_COLUMN].iloc[-1]
    reached = []
    for name, rate in SCENARIOS.items():
        values = project(summary.net_worth, summary.monthly_saving, rate, summary.target)
        dates = [end + pd.DateOffset(months=i) for i in range(len(values))]
        hit = values[-1] >= summary.target
        if hit:
            reached.append(dates[-1])
        # the reach date sits in the legend; a dot marks where the line crosses the target
        fig.add_trace(go.Scatter(x=dates, y=values, name=f"{name}: {_reach_date(summary, len(values) - 1 if hit else None)}",
                                 mode="lines", line=dict(width=2, color=t.scenario_colors[name], dash="dash"),
                                 hovertemplate=f"%{{y:,.0f}} €<extra>{name}</extra>"))
        if hit:
            fig.add_trace(go.Scatter(x=[dates[-1]], y=[values[-1]], mode="markers", showlegend=False,
                                     marker=dict(size=9, color=t.scenario_colors[name], line=dict(width=2, color=t.surface)),
                                     hovertemplate=f"%{{x|%b %Y}}<extra>{name}</extra>"))

    fig.add_hline(y=summary.target, line=dict(color=t.ink_muted, width=1),
                  annotation=dict(text=f"Ziel {_euro(summary.target)}", xanchor="left", x=0, yanchor="bottom",
                                  font=dict(color=t.ink_secondary)))
    _style(fig, t)
    fig.update_layout(hovermode="closest")
    # end the axis a bit after the latest reached scenario, so a never-reaching line doesn't stretch it to 50 years
    horizon = max(reached) + pd.DateOffset(years=3) if reached else end + pd.DateOffset(years=30)
    fig.update_xaxes(hoverformat="%b %Y", range=[df[DATE_COLUMN].iloc[0], horizon])
    fig.update_yaxes(range=[0, summary.target * 1.12])
    return fig


def create_figures(data: Data, summary: Summary, income: pd.Series | None = None,
                   t: Theme = LIGHT) -> dict[str, go.Figure]:
    figures = {
        "wealth": plot_wealth(data, t),
        "monthly": plot_monthly_flows(data, t),
        "distribution": plot_distribution(data, t),
        "projection": plot_projection(data, summary, t),
        "yearly": plot_yearly_flows(data, income, t),
        "shares": plot_shares(data, t),
    }
    if data.has_depot:
        figures["depot"] = plot_depot(data, t)
    return figures


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _css_variables(t: Theme) -> str:
    return (f"--surface: {t.surface}; --page: {t.page}; --ink: {t.ink}; --ink-secondary: {t.ink_secondary}; "
            f"--ink-muted: {t.ink_muted}; --grid: {t.grid}; --border: {t.border}; --accent: {t.savings}; "
            f"--accent-track: {t.tint(t.savings, 0.8)};")


THEME_CSS = f"""
:root {{ color-scheme: light; {_css_variables(LIGHT)} }}
:root[data-theme="dark"] {{ color-scheme: dark; {_css_variables(DARK)} }}
"""

# Top bar of every page (dashboards and static/style.css pages alike): fixed px sizes and full width, so it looks the
# same everywhere and doesn't jump between pages.
TOOLBAR_CSS = f"""
.toolbar {{ position: sticky; top: 0; z-index: 10; background: var(--surface); border-bottom: 1px solid var(--border);
            font: 500 14px/20px {FONT}; color: var(--ink-secondary); }}
.toolbar-inner {{ display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px 12px;
                  padding: 8px clamp(16px, 2vw, 48px); }}
.toolbar nav {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 2px; flex: 1 1 0; min-width: 0; }}
.toolbar .nav-sep {{ flex: none; width: 1px; height: 20px; margin: 0 6px; background: var(--grid); }}
.toolbar a, .toolbar .account button {{ flex: none; white-space: nowrap; font: inherit; color: var(--ink-secondary);
    text-decoration: none; background: none; border: 0; border-radius: 8px; padding: 6px 9px; cursor: pointer; }}
.toolbar a:hover, .toolbar .account button:hover {{ background: var(--grid); color: var(--ink); }}
.toolbar a.active {{ background: var(--accent-track); color: var(--ink); }}
.toolbar a.cta {{ background: var(--accent); color: #fff; }}
.toolbar a.cta:hover {{ filter: brightness(1.1); }}
.toolbar a:focus-visible, .toolbar button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
.toolbar .account {{ display: flex; align-items: center; gap: 2px; padding-left: 10px;
                     border-left: 1px solid var(--grid); }}
.toolbar .account form {{ display: flex; margin: 0; }}
.theme-switch {{ display: inline-flex; flex: none; border: 1px solid var(--grid); border-radius: 8px;
                 overflow: hidden; }}
.theme-switch button {{ font: inherit; color: var(--ink-secondary); background: var(--surface); border: 0;
                        padding: 5px 10px; line-height: 20px; cursor: pointer; }}
.theme-switch button:hover {{ color: var(--ink); }}
.theme-switch button[aria-pressed="true"] {{ background: var(--grid); color: var(--ink); }}
.theme-switch button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
/* too narrow for one row: the menu gets its own row(s) below "new entry", account and theme switch */
@media (max-width: 1360px) {{
  .toolbar nav {{ order: 1; flex-basis: 100%; }}
  .toolbar a.cta {{ margin-right: auto; }}
}}
/* phones: the wrapped bar would take too much of the screen when it stays on top */
@media (max-width: 760px) {{ .toolbar {{ position: static; }} .toolbar .nav-sep {{ display: none; }} }}
"""

CSS = THEME_CSS + f"""
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink); font-family: {FONT}; }}
main {{ max-width: 3200px; margin: 0 auto; padding: 16px clamp(16px, 2vw, 48px) 48px; }}
{TOOLBAR_CSS}
header {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px; }}
.kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }}
.kpi.hero {{ grid-column: span 2; }}
.kpi-label {{ color: var(--ink-secondary); font-size: clamp(13px, 0.75vw, 20px); }}
.kpi-value {{ font-size: clamp(22px, 1.5vw, 38px); font-weight: 600; margin: 4px 0; }}
.hero .kpi-value {{ font-size: clamp(36px, 2.8vw, 76px); line-height: 1.1; }}
.kpi-note {{ color: var(--ink-muted); font-size: clamp(12px, 0.7vw, 18px); }}
.kpi-how {{ color: var(--ink-muted); font-size: clamp(11px, 0.65vw, 16px); margin-top: 8px; padding-top: 8px;
            border-top: 1px solid var(--grid); }}
.kpi-how b {{ font-weight: 600; color: var(--ink-secondary); }}
.progress {{ height: 8px; border-radius: 4px; background: var(--accent-track); margin: 8px 0 6px; overflow: hidden; }}
.progress > div {{ height: 100%; border-radius: 4px; background: var(--accent); }}
.grid {{ display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px; }}
.card {{ display: flex; flex-direction: column; background: var(--surface); border: 1px solid var(--border);
         border-radius: 12px; padding: 16px; }}
.card.wide {{ grid-column: 1 / -1; }}
h2 {{ font-size: clamp(15px, 0.9vw, 24px); font-weight: 600; margin: 0; }}
.sub {{ color: var(--ink-secondary); font-size: clamp(13px, 0.75vw, 19px); margin: 4px 0 8px; max-width: 80ch; }}
/* chart heights follow the viewport, so the dashboard fills the screen on large monitors */
.plot {{ flex: 1 0 auto; }}
.plot > div {{ height: 100%; }}
.plot.tall {{ height: clamp(380px, 46vh, 1100px); }}
.plot.mid {{ height: clamp(320px, 36vh, 900px); }}
details {{ margin-top: 16px; }}
summary {{ cursor: pointer; color: var(--ink-secondary); font-size: clamp(14px, 0.8vw, 20px); }}
.table-wrap {{ max-height: 480px; overflow: auto; margin-top: 12px; }}
table {{ border-collapse: collapse; font-size: clamp(12px, 0.7vw, 18px); font-variant-numeric: tabular-nums; width: 100%; }}
th, td {{ padding: 4px 10px; text-align: right; border-bottom: 1px solid var(--grid); white-space: nowrap; }}
th {{ position: sticky; top: 0; background: var(--surface); color: var(--ink-secondary); font-weight: 600; }}
th:first-child, td:first-child {{ text-align: left; }}
@media (max-width: 900px) {{ .grid {{ grid-template-columns: minmax(0, 1fr); }} .kpi.hero {{ grid-column: 1 / -1; }} }}
"""

# Runs in <head> before anything is painted: the stored choice, else the system setting.
THEME_INIT_JS = """
(function () {
  var theme = null;
  try { theme = localStorage.getItem("net-worth-theme"); } catch (e) {}
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = theme;
})();
"""

THEME_SWITCH_JS = """
function syncThemeButtons() {
  document.querySelectorAll("[data-theme-choice]").forEach(button => {
    const active = button.dataset.themeChoice === document.documentElement.dataset.theme;
    button.setAttribute("aria-pressed", String(active));
  });
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("net-worth-theme", theme); } catch (e) {}
  syncThemeButtons();
  if (typeof renderCharts === "function") renderCharts();
}

document.querySelectorAll("[data-theme-choice]").forEach(button => {
  button.addEventListener("click", () => setTheme(button.dataset.themeChoice));
});
syncThemeButtons();
"""

THEME_SWITCH_HTML = """<div class="theme-switch" role="group" aria-label="Darstellung">
    <button type="button" data-theme-choice="light" aria-pressed="false">Hell</button>
    <button type="button" data-theme-choice="dark" aria-pressed="false">Dunkel</button>
  </div>"""



def toolbar_html(nav: str = "") -> str:
    """Top bar with the navigation ``nav`` (``app._nav_html``) and the theme switch."""
    return f'<div class="toolbar"><div class="toolbar-inner">{nav}{THEME_SWITCH_HTML}</div></div>'


# Every chart is embedded once per theme (FIGURES[id][theme]); switching re-renders it with the other spec.
DASHBOARD_JS = f"""
const CONFIG = {json.dumps(PLOTLY_CONFIG)};

// Plotly sizes text in px; scale it with the window width so wide screens don't get tiny labels.
function fontSize() {{
  return Math.round(Math.min(20, Math.max({BASE_FONT_SIZE}, window.innerWidth / 140)));
}}

function renderCharts() {{
  const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  for (const [id, specs] of Object.entries(FIGURES)) {{
    const gd = document.getElementById("chart-" + id);
    const spec = specs[theme];
    const layout = JSON.parse(JSON.stringify(spec.layout));
    layout.font.size = fontSize();
    // keep a range the viewer picked (range buttons, zoom) when switching the theme
    const current = gd.layout && gd.layout.xaxis;
    if (current && current.autorange === false && current.range) {{
      layout.xaxis.range = current.range.slice();
      layout.xaxis.autorange = false;
    }}
    Plotly.react(gd, spec.data, layout, CONFIG);
  }}
}}
{THEME_SWITCH_JS}
renderCharts();

let resizeTimer;
window.addEventListener("resize", () => {{
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {{
    const size = fontSize();
    document.querySelectorAll(".chart").forEach(gd => {{
      if (gd.layout && gd.layout.font.size !== size) Plotly.relayout(gd, {{"font.size": size}});
    }});
  }}, 150);
}});
"""


def _kpi(label: str, value: str, note: str, how: str, hero: bool = False, progress: float | None = None) -> str:
    """Key figure tile; ``how`` explains in one or two sentences how the value is calculated."""
    bar = ""
    if progress is not None:
        bar = f'<div class="progress"><div style="width: {min(max(progress, 0), 1) * 100:.1f}%"></div></div>'
    return (f'<div class="kpi{" hero" if hero else ""}"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}</div>{bar}'
            f'<div class="kpi-note">{html.escape(note)}</div>'
            f'<div class="kpi-how"><b>Berechnung:</b> {html.escape(how)}</div></div>')


def _market_gain_text(data: Data) -> str:
    gain, tax = data.names.get("depot_gain"), data.names.get("tax")
    if gain is None:
        return "0 (keine Spalte mit der Rolle Depot-Entwicklung)"
    return f"Änderung von {gain} + {tax}" if tax else f"Änderung von {gain}"


def _kpis(data: Data, summary: Summary) -> str:
    year = "12 Monate" if summary.year_months > 11 else f"{summary.year_months:.0f} Monate"
    rate = summary.savings_rate_year
    depot_return = summary.depot_return
    scenario = "4 % Rendite p.a."
    reach = _reach_date(summary, summary.months_to_target[scenario])
    start = f"{summary.year_start:%d.%m.%Y}"
    months = _de(summary.year_months, ".1f")
    rate_how = ""
    if rate is not None:
        rate_how = " Sparquote = gespart ÷ Nettogehälter derselben Monate (Seite „Einkommen“)."
    total = data.names.get("total", "Summe")
    tiles = [
        _kpi(f"Vermögen am {summary.date:%d.%m.%Y}", _euro(summary.net_worth),
             f"{_euro(summary.change_month, sign=True)} im letzten Monat",
             f"Spalte „{total}“ im letzten Eintrag. Vormonat: Vergleich mit dem Eintrag, "
             "der am nächsten an einem Monat davor liegt.", hero=True),
        _kpi(f"Zuwachs letzte {year}", _euro(summary.change_year, sign=True),
             f"{_euro(summary.saved_year)} gespart · {_euro(summary.market_year, sign=True)} Kursgewinne",
             f"Letzter Eintrag minus Eintrag vom {start}. Kursgewinne = {_market_gain_text(data)}, "
             f"der Rest zählt als gespart."),
        _kpi("Sparleistung", f"{_euro(summary.monthly_saving)} / Monat",
             f"Sparquote {_percent(rate)} vom Nettogehalt" if rate is not None else f"Ø der letzten {year}",
             f"Nur die letzten 12 Monate: gespart seit {start} ÷ {months} Monate.{rate_how}"),
    ]
    if data.has_depot:
        tiles.append(_kpi("Depot-Rendite", _percent(depot_return, sign=True) if depot_return is not None else "–",
                          f"{_euro(summary.depot_gain, sign=True)} nach KESt auf {_euro(summary.depot_paid_in)} "
                          "eingezahlt",
                          f"Gesamt seit Depotstart, nicht pro Jahr: ({_market_gain_text(data).removeprefix('Änderung von ')})"
                          " ÷ eingezahltes Kapital im letzten Eintrag."))
    tiles.append(_kpi(f"Ziel {_euro(summary.target)}", _percent(summary.target_progress) + " erreicht",
                      f"bei 4 % Rendite: {reach}",
                      "Vermögen ÷ Ziel. Datum: Hochrechnung mit der Sparleistung pro Monat und 4 % Rendite p.a. auf "
                      "das gesamte Vermögen.", progress=summary.target_progress))
    return "".join(tiles)


def _card(title: str, subtitle: str, chart_id: str, size: str = "mid", wide: bool = False) -> str:
    return (f'<section class="card{" wide" if wide else ""}"><h2>{html.escape(title)}</h2>'
            f'<p class="sub">{subtitle}</p>'
            f'<div class="plot {size}"><div id="chart-{chart_id}" class="chart"></div></div></section>')


def _table(data: Data) -> str:
    table = data.df[[DATE_COLUMN, *data.stack_order, TOTAL]].iloc[::-1].copy()
    table[DATE_COLUMN] = table[DATE_COLUMN].dt.strftime("%d.%m.%Y")
    table = table.rename(columns={TOTAL: data.names.get("total", "Summe")})
    numbers = [c for c in table.columns if c != DATE_COLUMN]
    return table.to_html(index=False, border=0, formatters={col: _de for col in numbers})


def _figures_json(figures: dict[str, dict[str, go.Figure]]) -> str:
    """{chart id: {theme: figure spec}} as JSON that is safe inside a <script> element."""
    specs = {chart_id: {theme: json.loads(fig.to_json()) for theme, fig in themed.items()}
             for chart_id, themed in figures.items()}
    return json.dumps(specs, separators=(",", ":")).replace("</", "<\\/")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def build_dashboard(data: Data, summary: Summary, income: pd.Series | None = None,
                    show_distribution: bool = True, plotly_src: str | None = None, nav: str = "") -> str:
    """Render the complete dashboard as one HTML page (``plotly_src`` and ``nav`` as in ``page_html``)."""
    themed = {name: create_figures(data, summary, income, theme) for name, theme in THEMES.items()}
    chart_ids = [chart_id for chart_id in themed["light"] if show_distribution or chart_id != "distribution"]
    figures = {chart_id: {name: themed[name][chart_id] for name in THEMES} for chart_id in chart_ids}

    saving = _euro(summary.monthly_saving)
    explain_split = (f"Kursgewinne = {_escape(_market_gain_text(data))} (also nach Steuer); "
                     "„gespart“ ist der Rest "
                     "der Vermögensänderung – Einzahlungen, Zinsen und Vorsorgebeiträge.")
    netted = ", ".join(f"{_escape(s)} ist mit {_escape(t)}" for s, t in data.net_into.items())

    cards = [
        _card("Vermögensentwicklung", "Gesamtvermögen je Eintrag nach Anlagegruppe, die Linie obenauf ist die Summe. "
              + (f"{netted} verrechnet. " if netted else "")
              + "Klick auf eine Gruppe in der Legende blendet sie aus.", "wealth", "tall", wide=True),
        _card("Woher kommt der Zuwachs?", "Veränderung pro Kalendermonat: letzter Eintrag des Monats minus letzter "
              f"Eintrag davor. {explain_split}", "monthly", wide=not show_distribution),
    ]
    if show_distribution:
        cards.append(_card("Wo liegt mein Geld?", "Stand im letzten Eintrag je Konto und Anteil am Gesamtvermögen "
                           "(Konten mit Saldo ≤ 0 sind ausgeblendet).", "distribution"))
    cards.append(_card(f"Weg zum Ziel von {_euro(summary.target)}",
                       f"Hochrechnung ab dem letzten Eintrag: Jeden Monat kommen {saving} dazu (Sparleistung der "
                       f"letzten 12 Monate) und das gesamte Vermögen wächst mit 0, 4 bzw. 7 % pro Jahr. Keine "
                       f"Prognose, nur eine Orientierung.", "projection"))
    if data.has_depot:
        gain = _escape(data.names.get("depot_gain", "Depot-Entwicklung"))
        cards.append(_card("Depot: eingezahlt vs. Wert", f"Fläche = eingezahltes Kapital (Depotwert minus {gain}), "
                           "Linie = Depotwert vor KESt. Liegt die Linie darüber, bist du im Plus. Die Depot-Rendite "
                           "oben ist nach KESt.", "depot"))
    cards += [
        _card("Jahresübersicht", "Zuwachs pro Kalenderjahr: letzter Eintrag des Jahres minus letzter Eintrag davor. "
              f"{explain_split} Sparquote = gespart ÷ Nettogehälter des Jahres (ohne den angebrochenen ersten "
              "Monat).", "yearly"),
        _card("Aufteilung über die Zeit", "Summe je Anlagegruppe ÷ Gesamtvermögen, für jeden Eintrag; negative "
              "Salden (z. B. Girokonto) zählen als 0.", "shares"),
    ]

    table = (f'<details><summary>Datentabelle ({len(data.df)} Einträge)</summary>'
             f'<div class="table-wrap">{_table(data)}</div></details>')
    return page_html("Net Worth Dashboard", _kpis(data, summary), cards, figures, plotly_src, nav, table)


def page_html(title: str, kpis: str, cards: Sequence[str], figures: dict[str, dict[str, go.Figure]],
              plotly_src: str | None = None, nav: str = "", footer: str = "") -> str:
    """Dashboard page: key figure tiles, a grid of chart cards and ``footer`` (e.g. a data table).

    ``figures``: {chart id: {theme: figure}}, rendered into the cards' ``chart-<id>`` elements.
    ``plotly_src``: URL of plotly.js; ``None`` inlines it, so the page works offline as a single file.
    ``nav``: HTML for the navigation links in the toolbar.
    """
    plotly = f'<script src="{html.escape(plotly_src)}"></script>' if plotly_src else f"<script>{get_plotlyjs()}</script>"
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<script>{THEME_INIT_JS}</script>
<style>{CSS}</style>
{plotly}
</head>
<body>
{toolbar_html(nav)}
<main>
<header>{kpis}</header>
<div class="grid">{"".join(cards)}</div>
{footer}
</main>
<script>const FIGURES = {_figures_json(figures)};</script>
<script>{DASHBOARD_JS}</script>
</body>
</html>
"""


def render(sheet: pd.DataFrame, layout: Layout, income: pd.Series | None, target: float,
           show_distribution: bool = True, plotly_src: str | None = None, nav: str = "") -> str:
    data = prepare_data(sheet, layout)
    summary = summarize(data.df, income, target)
    return build_dashboard(data, summary, income, show_distribution, plotly_src, nav)
