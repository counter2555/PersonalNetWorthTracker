"""Achievements for net worth and income, in the style of game achievements.

Every achievement is a threshold on a time series (net worth, best month, months in a row with savings, ...):
it unlocks on the first date the series reaches the goal and stays unlocked, like in a game. Until then the
progress bar shows the best value so far. Shame achievements work the same way, only nobody wants them.

Page layout, themes and formatting come from ``dashboard``.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from .columns import DATE_COLUMN, Layout
from .dashboard import (CSS, DEPOT_PAID_IN, DEPOT_VALUE, MARKET_GAIN, MONTHS_DE, NET_GAINS, TAX, THEME_INIT_JS,
                        THEME_SWITCH_HTML, THEME_SWITCH_JS, TOTAL, Data, Summary, _de, _euro, _kpi, _percent,
                        period_flows, period_salaries, prepare_data, summarize)
from .income_dashboard import monthly_income, yearly_income

TIERS = {"bronze": ("Bronze", 10), "silver": ("Silber", 25), "gold": ("Gold", 50), "platinum": ("Platin", 100)}

WEALTH, MARKET, HABIT, INCOME, FREEDOM, SHAME = ("Vermögen", "Börse & Depot", "Disziplin", "Einkommen",
                                                "Sparen & Freiheit", "Schande")
CATEGORIES = {
    WEALTH: "Meilensteine des Gesamtvermögens.",
    MARKET: "Wenn das Geld für dich arbeitet – und wenn es streikt.",
    HABIT: "Regelmäßig eintragen, regelmäßig sparen.",
    INCOME: "Meilensteine des Nettogehalts.",
    FREEDOM: "Sparquote und finanzielle Unabhängigkeit.",
    SHAME: "Negative Achievements. Freischalten auf eigene Gefahr – sie kosten Punkte.",
}


@dataclass
class Achievement:
    icon: str
    title: str
    description: str
    category: str
    tier: str  # key of TIERS
    value: float  # best value so far
    goal: float
    unit: str = "€"  # "€", "%", "×" or a count unit ("Einträge", "Monate", ...); "" for yes/no achievements
    unlocked: pd.Timestamp | None = None
    shame: bool = False
    secret: bool = False  # title and description stay hidden until unlocked

    @property
    def progress(self) -> float:
        if self.unlocked is not None:
            return 1.0
        return min(max(self.value / self.goal, 0.0), 1.0) if self.goal > 0 else 0.0

    @property
    def points(self) -> int:
        points = TIERS[self.tier][1]
        return -points if self.shame else points

    @property
    def binary(self) -> bool:
        return self.unit == "" and self.goal == 1


def achievement(icon: str, title: str, description: str, category: str, tier: str, series: pd.Series, goal: float,
                unit: str = "€", shame: bool = False, secret: bool = False) -> Achievement:
    """Unlocked on the first date (index of ``series``) on which ``series`` reaches ``goal``."""
    series = series.dropna()
    reached = series[series >= goal]
    return Achievement(icon, title, description, category, tier, float(series.max()) if len(series) else 0.0, goal,
                       unit, pd.Timestamp(reached.index[0]) if len(reached) else None, shame, secret)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

def _month_end(index: pd.PeriodIndex) -> pd.DatetimeIndex:
    return index.to_timestamp(how="end").normalize()


def streaks(flags: pd.Series) -> pd.Series:
    """Length of the run of ``True`` values ending at each position."""
    flags = flags.astype(bool)
    return flags.astype(int).groupby((~flags).cumsum()).cumsum()


def recovered_drawdowns(ratio: pd.Series) -> pd.Series:
    """Largest drawdown (share below the previous peak) that was followed by a new peak, at each position."""
    peak = drawdown = best = 0.0
    out = []
    for value in ratio:
        if value >= peak:
            best, peak, drawdown = max(best, drawdown), value, 0.0
        elif peak > 0:
            drawdown = max(drawdown, 1 - value / peak)
        out.append(best)
    return pd.Series(out, index=ratio.index, dtype=float)


def _monthly_flows(df: pd.DataFrame) -> pd.DataFrame:
    flows = period_flows(df, "M")
    flows.index = _month_end(flows.index)
    return flows


def _yearly(df: pd.DataFrame, income: pd.Series) -> pd.DataFrame:
    """Savings rate and market gains / salary per calendar year with at least 6 months of salary.

    Indexed by the year's last entry; salaries as in the dashboard (from the month after the first entry).
    """
    flows = period_flows(df, "Y")
    salaries = period_salaries(df, income, "Y")
    monthly = period_salaries(df, income, "M")
    months = (monthly > 0).groupby(monthly.index.year).sum()
    years = [p for p in flows.index.intersection(salaries.index) if salaries[p] > 0 and months.get(p.year, 0) >= 6]
    last = df.groupby(df[DATE_COLUMN].dt.to_period("Y"))[DATE_COLUMN].last()
    table = pd.DataFrame({"Sparquote": flows.loc[years, "Sparen"] / salaries.loc[years],
                          "Kursgewinne": flows.loc[years, "Kursgewinne"] / salaries.loc[years]})
    table.index = pd.DatetimeIndex(last.loc[years].to_numpy())
    return table


def _schnapszahl(value: float) -> bool:
    digits = str(abs(round(value)))
    return len(digits) >= 4 and len(set(digits)) == 1


# ---------------------------------------------------------------------------
# Achievements
# ---------------------------------------------------------------------------

def wealth_achievements(data: Data, target: float) -> list[Achievement]:
    df = data.df
    total = df.set_index(DATE_COLUMN)[TOTAL]
    entries = pd.Series(range(1, len(df) + 1), index=total.index, dtype=float)
    flows = _monthly_flows(df)
    first = float(total.iloc[0])
    highs = (total > total.cummax().shift(fill_value=float("-inf"))).cumsum().astype(float) - 1  # first entry isn't a record

    a = [achievement("🐣", "Erste Schritte", "Den ersten Vermögens-Eintrag angelegt.", WEALTH, "bronze", entries, 1, ""),
         achievement("🪙", "Notgroschen", "1.000 € Vermögen.", WEALTH, "bronze", total, 1_000),
         achievement("🖐️", "Fünfstellig", "10.000 € Vermögen – die erste fünfstellige Zahl.", WEALTH, "bronze",
                     total, 10_000),
         achievement("🛋️", "Polster", "25.000 € Vermögen.", WEALTH, "silver", total, 25_000),
         achievement("🏠", "Halbe Miete", "50.000 € Vermögen.", WEALTH, "silver", total, 50_000),
         achievement("💯", "Die schwersten Hunderttausend", "100.000 € Vermögen. Ab hier hilft der Zinseszins mit.",
                     WEALTH, "gold", total, 100_000),
         achievement("🏰", "Viertelmillionär", "250.000 € Vermögen.", WEALTH, "gold", total, 250_000),
         achievement("🛥️", "Halbmillionär", "500.000 € Vermögen.", WEALTH, "platinum", total, 500_000),
         achievement("👑", "Millionär", "1.000.000 € Vermögen. Willkommen im Club.", WEALTH, "platinum",
                     total, 1_000_000)]
    if target > 0 and target != 1_000_000:
        a.append(achievement("🎯", "Mission erfüllt", f"Das eigene Ziel von {_euro(target)} erreicht "
                             "(Einstellungen).", WEALTH, "platinum", total, target))
    if first > 0:
        a += [achievement("✌️", "Verdoppler", "Vermögen verdoppelt gegenüber dem ersten Eintrag.", WEALTH, "silver",
                          total / first, 2, "×"),
              achievement("🚀", "Verfünffacher", "Fünfmal so viel Vermögen wie beim ersten Eintrag.", WEALTH, "gold",
                          total / first, 5, "×")]
    a += [achievement("📈", "Allzeithoch", "10 neue Vermögens-Rekorde aufgestellt.", WEALTH, "bronze", highs, 10,
                      "Rekorde"),
          achievement("🏔️", "Gipfelstürmer", "50 neue Vermögens-Rekorde aufgestellt.", WEALTH, "silver", highs, 50,
                      "Rekorde"),
          achievement("💥", "Rekordmonat", "Vermögen in einem Kalendermonat um 5.000 € gesteigert.", WEALTH, "silver",
                      flows["Gesamt"], 5_000),
          achievement("🌋", "Monstermonat", "Vermögen in einem Kalendermonat um 20.000 € gesteigert.", WEALTH, "gold",
                      flows["Gesamt"], 20_000),
          achievement("🍻", "Schnapszahl", "Ein Vermögensstand mit lauter gleichen Ziffern, z. B. 22.222 €.", WEALTH,
                      "silver", total.map(_schnapszahl).astype(float), 1, "", secret=True),
          achievement("🕶️", "1337", "Ein Vermögensstand, der auf …1.337 € endet. Elite.", WEALTH, "silver",
                      (total.round() % 10_000 == 1337).astype(float), 1, "", secret=True)]
    return a


def market_achievements(data: Data) -> list[Achievement]:
    df = data.df
    dated = df.set_index(DATE_COLUMN)
    gains = dated[MARKET_GAIN]
    flows = _monthly_flows(df)
    a = [achievement("🌱", "Das Geld arbeitet", "1.000 € Kursgewinne nach Steuer angesammelt.", MARKET, "bronze",
                     gains, 1_000),
         achievement("🌳", "Zinseszins-Zauberer", "25.000 € Kursgewinne nach Steuer angesammelt.", MARKET, "silver",
                     gains, 25_000),
         achievement("🏦", "Kapitalist", "100.000 € Kursgewinne nach Steuer angesammelt.", MARKET, "gold",
                     gains, 100_000),
         achievement("🎢", "Bullenritt", "In einem Kalendermonat 3.000 € Kursgewinne.", MARKET, "silver",
                     flows["Kursgewinne"], 3_000)]
    if data.has_depot:
        depot = dated[dated[DEPOT_PAID_IN] > 0]
        returns = (depot[DEPOT_VALUE] + depot[TAX] - depot[DEPOT_PAID_IN]) / depot[DEPOT_PAID_IN]
        ratio = depot[DEPOT_VALUE] / depot[DEPOT_PAID_IN]
        a += [achievement("📊", "Im grünen Bereich", "Depot-Rendite von 10 % nach KESt (gesamt seit Depotstart).",
                          MARKET, "bronze", returns, 0.10, "%"),
              achievement("🔥", "Heißes Händchen", "Depot-Rendite von 50 % nach KESt.", MARKET, "gold", returns, 0.5,
                          "%"),
              achievement("🦄", "Doppelt hält besser", "Depot-Rendite von 100 % nach KESt – eingezahltes Geld "
                          "verdoppelt.", MARKET, "platinum", returns, 1.0, "%"),
              achievement("💎", "Diamond Hands", "Einen Kursrückgang von 10 % im Depot ausgesessen, bis es ein neues "
                          "Hoch gab (Depotwert ÷ eingezahlt).", MARKET, "gold", recovered_drawdowns(ratio), 0.10, "%")]
    return a


def habit_achievements(data: Data) -> list[Achievement]:
    df = data.df
    dates = df[DATE_COLUMN]
    entries = pd.Series(range(1, len(df) + 1), index=dates, dtype=float)
    months = dates.dt.to_period("M")
    calendar = pd.period_range(months.min(), months.max(), freq="M")
    entry_streak = streaks(pd.Series(calendar.isin(months.unique()), index=_month_end(calendar)))
    saving_streak = streaks(_monthly_flows(df)["Sparen"] > 0)
    years = pd.Series(((dates - dates.iloc[0]).dt.days / 365.25).to_numpy(), index=dates)
    accounts = [c for c in data.stack_order if c != NET_GAINS]
    positive = (df[accounts] > 0).sum(axis=1).astype(float) if accounts else pd.Series(0.0, index=df.index)
    positive.index = dates
    return [
        achievement("📒", "Buchhalter", "25 Einträge erfasst.", HABIT, "bronze", entries, 25, "Einträge"),
        achievement("🗄️", "Archivar", "100 Einträge erfasst.", HABIT, "silver", entries, 100, "Einträge"),
        achievement("🧾", "Erbsenzähler", "250 Einträge erfasst.", HABIT, "gold", entries, 250, "Einträge"),
        achievement("📅", "Am Ball geblieben", "12 Monate in Folge mindestens einen Eintrag.", HABIT, "bronze",
                    entry_streak, 12, "Monate"),
        achievement("⏰", "Gewohnheitstier", "36 Monate in Folge mindestens einen Eintrag.", HABIT, "gold",
                    entry_streak, 36, "Monate"),
        achievement("🐷", "Sparschwein", "6 Monate in Folge gespart (Vermögenszuwachs ohne Kursgewinne > 0).", HABIT,
                    "bronze", saving_streak, 6, "Monate"),
        achievement("🤖", "Sparmaschine", "12 Monate in Folge gespart.", HABIT, "silver", saving_streak, 12, "Monate"),
        achievement("🗿", "Unaufhaltsam", "24 Monate in Folge gespart.", HABIT, "gold", saving_streak, 24, "Monate"),
        achievement("🎂", "Jahrestag", "Ein Jahr Vermögen getrackt.", HABIT, "bronze", years, 1, "Jahre"),
        achievement("🏛️", "Veteran", "Fünf Jahre Vermögen getrackt.", HABIT, "gold", years, 5, "Jahre"),
        achievement("🧺", "Viele Körbe", "Gleichzeitig 5 Konten mit positivem Saldo.", HABIT, "bronze", positive, 5,
                    "Konten"),
    ]


def income_achievements(income: pd.Series) -> list[Achievement]:
    monthly = monthly_income(income)["Gesamt"]
    yearly = yearly_income(monthly.to_frame())
    dated = monthly.copy()
    dated.index = _month_end(monthly.index)
    lifetime = dated.cumsum()
    year_to_date = dated.groupby(dated.index.year).cumsum()
    streak = streaks(dated > 0)
    typical = dated.where(dated > 0).rolling(12, min_periods=6).median().shift()
    bonus = dated / typical
    raises = yearly["Veränderung"].copy()
    raises.index = [pd.Timestamp(year, int(yearly.loc[year, "Letzter Monat"]), 1) + pd.offsets.MonthEnd(0)
                    for year in yearly.index]
    return [
        achievement("💶", "Zahltag", "Das erste Nettogehalt eingetragen.", INCOME, "bronze", lifetime, 0.01),
        achievement("💵", "Gutverdiener", "Ein Monat mit 3.000 € netto.", INCOME, "bronze", dated, 3_000),
        achievement("💰", "Spitzenverdiener", "Ein Monat mit 5.000 € netto.", INCOME, "silver", dated, 5_000),
        achievement("🤑", "Fünfstelliger Monat", "Ein Monat mit 10.000 € netto.", INCOME, "gold", dated, 10_000),
        achievement("📆", "Solides Jahr", "50.000 € netto in einem Kalenderjahr.", INCOME, "silver", year_to_date,
                    50_000),
        achievement("🏆", "Sechsstellig netto", "100.000 € netto in einem Kalenderjahr.", INCOME, "platinum",
                    year_to_date, 100_000),
        achievement("🧮", "Lebensverdienst I", "100.000 € netto insgesamt verdient (seit Beginn der Aufzeichnung).",
                    INCOME, "bronze", lifetime, 100_000),
        achievement("⛰️", "Lebensverdienst II", "500.000 € netto insgesamt verdient.", INCOME, "gold", lifetime,
                    500_000),
        achievement("🌌", "Lebens-Million", "1.000.000 € netto insgesamt verdient.", INCOME, "platinum", lifetime,
                    1_000_000),
        achievement("📈", "Gehaltserhöhung", "5 % mehr netto als in denselben Monaten des Vorjahres.", INCOME,
                    "bronze", raises, 0.05, "%"),
        achievement("🪜", "Karrieresprung", "15 % mehr netto als in denselben Monaten des Vorjahres.", INCOME, "gold",
                    raises, 0.15, "%"),
        achievement("🎁", "Bonusmonat", "Ein Monat mit mindestens dem 1,8-fachen des üblichen Gehalts (Median der "
                    "12 Monate davor) – Urlaubs-, Weihnachtsgeld oder Bonus.", INCOME, "silver", bonus, 1.8, "×"),
        achievement("🔁", "Treue Seele", "60 Monate in Folge ein Gehalt erhalten.", INCOME, "gold", streak, 60,
                    "Monate"),
    ]


def freedom_achievements(data: Data, income: pd.Series, summary: Summary) -> list[Achievement]:
    df = data.df
    total = df.set_index(DATE_COLUMN)[TOTAL]
    yearly = _yearly(df, income)
    a = [achievement("🥉", "Zehntel-Sparer", "Ein Kalenderjahr mit einer Sparquote von 10 %.", FREEDOM, "bronze",
                     yearly["Sparquote"], 0.10, "%"),
         achievement("🥈", "Fleißiges Eichhörnchen", "Ein Kalenderjahr mit einer Sparquote von 25 %.", FREEDOM,
                     "silver", yearly["Sparquote"], 0.25, "%"),
         achievement("🥷", "Frugal-Ninja", "Ein Kalenderjahr mit einer Sparquote von 50 %.", FREEDOM, "platinum",
                     yearly["Sparquote"], 0.50, "%"),
         achievement("🦾", "Das Geld arbeitet härter als du", "Ein Kalenderjahr, in dem die Kursgewinne höher waren "
                     "als das Nettogehalt.", FREEDOM, "platinum", yearly["Kursgewinne"], 1.0, "%")]
    expenses = (summary.salary_year or 0) - summary.saved_year
    if summary.salary_year and summary.year_months > 0 and expenses > 0:
        per_year = expenses * 12 / summary.year_months
        covered = total / per_year
        note = f"(Ausgaben der letzten 12 Monate: {_euro(per_year)} pro Jahr)"
        a += [achievement("🏖️", "Ein Jahr Freiheit", f"Vermögen reicht für ein Jahr Ausgaben {note}.", FREEDOM,
                          "silver", covered, 1, "Jahre"),
              achievement("🧳", "Sabbatical-ready", f"Vermögen reicht für fünf Jahre Ausgaben {note}.", FREEDOM,
                          "gold", covered, 5, "Jahre"),
              achievement("🔥", "FIRE", f"Vermögen ≥ 25 Jahresausgaben – nach der 4-%-Regel finanziell frei {note}.",
                          FREEDOM, "platinum", covered, 25, "Jahre")]
    return a


def shame_achievements(data: Data | None, income: pd.Series | None) -> list[Achievement]:
    a = []
    if data is not None:
        df = data.df
        dated = df.set_index(DATE_COLUMN)
        total = dated[TOTAL]
        flows = _monthly_flows(df)
        gaps = pd.Series(df[DATE_COLUMN].diff().dt.days.to_numpy(), index=df[DATE_COLUMN])
        a += [achievement("🤕", "Autsch", "Vermögen ist in einem Kalendermonat um 1.000 € gesunken.", SHAME, "bronze",
                          -flows["Gesamt"], 1_000, shame=True),
              achievement("🪂", "Freier Fall", "Vermögen ist in einem Kalendermonat um 10.000 € gesunken.", SHAME,
                          "gold", -flows["Gesamt"], 10_000, shame=True),
              achievement("🐈‍⬛", "Pechsträhne", "Drei Monate in Folge weniger Vermögen als davor.", SHAME, "silver",
                          streaks(flows["Gesamt"] < 0), 3, "Monate", shame=True),
              achievement("🫠", "Kaufrausch", "Ein Kalendermonat, in dem 2.000 € mehr ausgegeben als gespart wurden "
                          "(Vermögensänderung ohne Kursgewinne).", SHAME, "silver", -flows["Sparen"], 2_000,
                          shame=True),
              achievement("🧊", "Rote Zahlen", "Gesamtvermögen unter 0 €.", SHAME, "gold", (total < 0).astype(float),
                          1, "", shame=True),
              achievement("👻", "Verschollen", "90 Tage lang keinen Eintrag gemacht.", SHAME, "bronze", gaps, 90,
                          "Tage", shame=True),
              achievement("📉", "Crash-Test-Dummy", "In einem Kalendermonat 5.000 € Kursverluste.", SHAME, "silver",
                          -flows["Kursgewinne"], 5_000, shame=True)]
        if data.has_depot:
            depot = dated[dated[DEPOT_PAID_IN] > 0]
            ratio = depot[DEPOT_VALUE] / depot[DEPOT_PAID_IN]
            a.append(achievement("🧻", "Papierhände-Test", "Das Depot lag 20 % unter seinem Höchststand "
                                 "(Depotwert ÷ eingezahlt).", SHAME, "gold", 1 - ratio / ratio.cummax(), 0.20, "%",
                                 shame=True))
        if income is not None:
            yearly = _yearly(df, income)
            a += [achievement("🍾", "Lifestyle-Inflation", "Ein Kalenderjahr mit einer Sparquote unter 10 % "
                              "(90 % des Nettogehalts ausgegeben).", SHAME, "silver", 1 - yearly["Sparquote"], 0.9,
                              "%", shame=True),
                  achievement("💸", "Über den Verhältnissen", "Ein Kalenderjahr, in dem mehr ausgegeben als verdient "
                              "wurde (Sparquote unter 0 %).", SHAME, "gold", 1 - yearly["Sparquote"], 1.0, "%",
                              shame=True)]
    if income is not None:
        monthly = monthly_income(income)["Gesamt"]
        dated = monthly.copy()
        dated.index = _month_end(monthly.index)
        yearly = yearly_income(monthly.to_frame())
        cuts = -yearly["Veränderung"]
        cuts.index = [pd.Timestamp(year, int(yearly.loc[year, "Letzter Monat"]), 1) + pd.offsets.MonthEnd(0)
                      for year in yearly.index]
        a += [achievement("🏜️", "Ebbe in der Kasse", "Ein Monat ganz ohne Gehalt (zwischen zwei Gehältern).", SHAME,
                          "bronze", (dated <= 0).cumsum().astype(float), 1, "", shame=True),
              achievement("✂️", "Gehaltsdelle", "5 % weniger netto als in denselben Monaten des Vorjahres.", SHAME,
                          "silver", cuts, 0.05, "%", shame=True)]
    return a


def collect(data: Data | None, income: pd.Series | None, target: float) -> list[Achievement]:
    items = []
    if data is not None:
        items += wealth_achievements(data, target) + market_achievements(data) + habit_achievements(data)
    if income is not None:
        items += income_achievements(income)
    if data is not None and income is not None:
        items += freedom_achievements(data, income, summarize(data.df, income, target))
    return items + shame_achievements(data, income)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def _value(value: float, unit: str) -> str:
    if unit == "€":
        return _euro(value)
    if unit == "%":
        return _percent(value)
    if unit == "×":
        return f"{_de(value, ',.1f')}×"
    return f"{_de(value, ',.1f' if 0 < value < 10 and value != int(value) else ',.0f')} {unit}".strip()


def _month(date: pd.Timestamp) -> str:
    return f"{MONTHS_DE[date.month - 1]} {date.year}"


def _row(a: Achievement) -> str:
    hidden = a.secret and a.unlocked is None
    tier = TIERS[a.tier][0]
    icon, title = ("❔", "Geheimes Achievement") if hidden else (a.icon, a.title)
    description = "Weiter tracken, um es zu enthüllen." if hidden else a.description
    state = "unlocked" if a.unlocked is not None else "locked"
    if a.unlocked is not None:
        meta = f"<span>Freigeschaltet {_month(a.unlocked)}</span>"
        bar = ""
    elif hidden or a.binary:
        meta, bar = "<span>Noch nicht freigeschaltet</span>", ""
    else:
        meta = (f"<span>{html.escape(_value(max(a.value, 0), a.unit))} / {html.escape(_value(a.goal, a.unit))}</span>"
                f"<span>{a.progress * 100:.0f} %</span>")
        bar = f'<div class="progress"><div style="width: {a.progress * 100:.1f}%"></div></div>'
    classes = f"ach {state} tier-{a.tier}{' shame' if a.shame else ''}"
    return (f'<article class="{classes}" data-state="{state}" data-shame="{int(a.shame)}">'
            f'<div class="ach-icon" aria-hidden="true">{icon}</div><div class="ach-body">'
            f'<div class="ach-head"><span class="ach-title">{html.escape(title)}</span>'
            f'<span class="ach-tier">{tier} · {a.points:+d}</span></div>'
            f'<div class="ach-desc">{html.escape(description)}</div>{bar}'
            f'<div class="ach-meta">{meta}</div></div></article>')


def _sort_key(a: Achievement):
    """Unlocked first (newest first), then the closest to unlocking."""
    if a.unlocked is not None:
        return 0, -a.unlocked.value
    return 1, -a.progress


def _kpis(items: Sequence[Achievement]) -> str:
    good = [a for a in items if not a.shame]
    unlocked = [a for a in good if a.unlocked is not None]
    shame = [a for a in items if a.shame and a.unlocked is not None]
    score = sum(a.points for a in unlocked)
    penalty = sum(a.points for a in shame)
    tiles = [
        _kpi("Achievements freigeschaltet", f"{len(unlocked)} / {len(good)}",
             f"{len(unlocked) / len(good) * 100:.0f} % geschafft" if good else "",
             "Freigeschaltete ÷ alle Achievements ohne Schande-Achievements. Einmal freigeschaltet bleibt "
             "freigeschaltet, auch wenn der Wert später wieder sinkt.", hero=True,
             progress=len(unlocked) / len(good) if good else 0),
        _kpi("Punkte", f"{score + penalty:,}".replace(",", "."),
             f"{score} verdient · {penalty} Schande".replace("-", "−"),
             "Bronze 10, Silber 25, Gold 50, Platin 100 Punkte. Schande-Achievements ziehen ebenso viele ab."),
    ]
    open_ = sorted((a for a in good if a.unlocked is None and not a.secret and not a.binary), key=lambda a: -a.progress)
    if open_:
        a = open_[0]
        tiles.append(_kpi("Fast geschafft", f"{a.icon} {a.title}", f"{a.progress * 100:.0f} % · "
                          f"{_value(max(a.value, 0), a.unit)} von {_value(a.goal, a.unit)}",
                          "Das offene Achievement mit dem größten Fortschritt."))
    recent = sorted(unlocked, key=lambda a: a.unlocked, reverse=True)
    if recent:
        a = recent[0]
        tiles.append(_kpi("Zuletzt freigeschaltet", f"{a.icon} {a.title}", _month(a.unlocked),
                          "Das Achievement mit dem jüngsten Freischaltdatum."))
    return "".join(tiles)


ACHIEVEMENTS_CSS = """
.tier-bronze { --tier: #b87333; } .tier-silver { --tier: #8f979e; } .tier-gold { --tier: #e0a100; }
.tier-platinum { --tier: #8a5cd6; }
:root { --shame: #d6457a; } :root[data-theme="dark"] { --shame: #e05a8a; }
[hidden] { display: none !important; }  /* author display rules (.ach is a grid) would override it */
.filters { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 8px; }
.filters button { font: inherit; font-size: clamp(13px, 0.75vw, 18px); color: var(--ink-secondary); background: var(--surface);
                  border: 1px solid var(--border); border-radius: 999px; padding: 6px 14px; cursor: pointer; }
.filters button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.filters button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ach-section > h2 { display: flex; justify-content: space-between; gap: 8px; margin: 24px 0 2px; }
.ach-section > h2 span { color: var(--ink-muted); font-weight: 400; }
.ach-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 480px), 1fr)); gap: 8px; }
.ach { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 14px; align-items: center; background: var(--surface);
       border: 1px solid var(--border); border-left: 4px solid var(--tier); border-radius: 12px; padding: 12px 14px; }
.ach-icon { width: 64px; height: 64px; border-radius: 10px; display: grid; place-items: center; font-size: 34px;
            background: color-mix(in srgb, var(--tier) 16%, var(--surface)); box-shadow: inset 0 0 0 2px var(--tier); }
.ach.locked { border-left-color: var(--grid); }
.ach.locked .ach-icon { filter: grayscale(1); opacity: 0.45; background: var(--page); box-shadow: inset 0 0 0 2px var(--grid); }
.ach.locked .ach-title { color: var(--ink-secondary); }
.ach.unlocked.shame { background: color-mix(in srgb, var(--shame) 7%, var(--surface)); border-left-color: var(--shame); }
.ach.unlocked.shame .ach-icon { box-shadow: inset 0 0 0 2px var(--shame);
                                background: color-mix(in srgb, var(--shame) 16%, var(--surface)); }
.ach.shame { --accent: var(--shame); --accent-track: color-mix(in srgb, var(--shame) 18%, var(--surface)); }
.ach-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.ach-title { font-weight: 600; font-size: clamp(14px, 0.85vw, 20px); }
.ach-tier { color: var(--tier); font-size: clamp(11px, 0.6vw, 15px); font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.04em; white-space: nowrap; }
.ach.locked .ach-tier { color: var(--ink-muted); }
.ach-desc { color: var(--ink-secondary); font-size: clamp(13px, 0.75vw, 18px); margin: 2px 0 4px; }
.ach .progress { margin: 6px 0 4px; }
.ach-meta { display: flex; justify-content: space-between; gap: 8px; color: var(--ink-muted);
            font-size: clamp(12px, 0.65vw, 16px); font-variant-numeric: tabular-nums; }
.hint { color: var(--ink-muted); font-size: clamp(13px, 0.75vw, 18px); }
.empty { display: none; color: var(--ink-muted); }
@media (max-width: 600px) { .ach { grid-template-columns: 48px minmax(0, 1fr); gap: 10px; }
  .ach-icon { width: 48px; height: 48px; font-size: 26px; } }
"""

FILTER_JS = """
document.querySelectorAll("[data-filter]").forEach(button => {
  button.addEventListener("click", () => {
    const filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach(b => b.setAttribute("aria-pressed", String(b === button)));
    document.querySelectorAll(".ach").forEach(row => {
      const shame = row.dataset.shame === "1";
      const show = filter === "all" || (filter === "shame" ? shame : !shame && row.dataset.state === filter);
      row.hidden = !show;
    });
    document.querySelectorAll(".ach-section").forEach(section => {
      section.hidden = !section.querySelector(".ach:not([hidden])");
    });
  });
});
"""


def build_page(items: Sequence[Achievement], nav: str = "", hints: Sequence[str] = ()) -> str:
    sections = []
    for category, subtitle in CATEGORIES.items():
        rows = sorted((a for a in items if a.category == category), key=_sort_key)
        if not rows:
            continue
        done = sum(a.unlocked is not None for a in rows)
        sections.append(f'<section class="ach-section"><h2>{html.escape(category)} <span>{done} / {len(rows)}</span>'
                        f'</h2><p class="sub">{html.escape(subtitle)}</p>'
                        f'<div class="ach-list">{"".join(_row(a) for a in rows)}</div></section>')
    filters = "".join(f'<button type="button" data-filter="{key}" aria-pressed="{str(key == "all").lower()}">'
                      f"{label}</button>" for key, label in (("all", "Alle"), ("unlocked", "Freigeschaltet"),
                                                             ("locked", "Offen"), ("shame", "Schande")))
    notes = "".join(f'<p class="hint">{html.escape(hint)}</p>' for hint in hints)
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Erfolge · Net Worth</title>
<script>{THEME_INIT_JS}</script>
<style>{CSS}{ACHIEVEMENTS_CSS}</style>
</head>
<body>
<main>
<div class="toolbar">{nav}Darstellung
  {THEME_SWITCH_HTML}
</div>
<header>{_kpis(items)}</header>
<div class="filters" role="group" aria-label="Filter">{filters}</div>
{notes}
{"".join(sections)}
</main>
<script>{THEME_SWITCH_JS}{FILTER_JS}</script>
</body>
</html>
"""


def render(sheet: pd.DataFrame | None, layout: Layout | None, income: pd.Series | None, target: float,
           nav: str = "") -> str:
    """``sheet``/``layout``: net worth entries, ``income``: monthly net salary; either may be missing."""
    data = prepare_data(sheet, layout) if sheet is not None and layout is not None and len(sheet) else None
    hints = []
    if data is None:
        hints.append("Lege Vermögens-Einträge an, um die Achievements zu Vermögen, Börse, Disziplin und Sparen "
                     "freizuschalten.")
    if income is None:
        hints.append("Trage dein Nettogehalt unter „Einkommen“ ein, um die Einkommens- und Spar-Achievements "
                     "freizuschalten.")
    return build_page(collect(data, income, target), nav, hints)
