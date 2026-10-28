"""Generate a fictional NetWorth.xlsx with the same layout as the real one.

All values are random numbers; use the file for tests and demos only:

    python generate_test_data.py example_NetWorth.xlsx
    python visualize_net_worth.py example_NetWorth.xlsx
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _walk(rng: np.random.Generator, n: int, start: float, drift: float, noise: float) -> np.ndarray:
    return np.maximum(start + np.cumsum(rng.normal(drift, noise, n)), 0).round(2)


def generate(n_rows: int = 150, start: str = "2023-01-06", depot_start_row: int = 40, seed: int = 42) -> pd.DataFrame:
    """Create ``n_rows`` weekly snapshots of a fictional net worth.

    The depot columns ("Depot Entwicklung", "Depot Einzahlung", "Kest") are empty before
    ``depot_start_row``, mimicking a depot that was opened later.
    """
    rng = np.random.default_rng(seed)
    dates = pd.Timestamp(start) + pd.to_timedelta(np.arange(n_rows) * 7 + rng.integers(0, 3, n_rows), unit="D")

    df = pd.DataFrame({"Datum": dates})
    df["Girokonto"] = _walk(rng, n_rows, 1800, 5, 250)
    df["Sparkonto"] = _walk(rng, n_rows, 5000, 40, 30)
    df["Urlaubskonto"] = _walk(rng, n_rows, 600, 8, 60)
    df["Kreditkarte"] = -rng.uniform(0, 800, n_rows).round(2)
    df["Flatex Geld"] = _walk(rng, n_rows, 300, 2, 40)

    # depot: regular deposits plus a noisy market development
    has_depot = np.arange(n_rows) >= depot_start_row
    deposits = np.where(has_depot, 250.0, 0.0).cumsum()
    returns = np.where(has_depot, rng.normal(0.0015, 0.015, n_rows), 0.0)
    development = np.zeros(n_rows)
    for i in range(1, n_rows):
        development[i] = (deposits[i - 1] + development[i - 1]) * returns[i] + development[i - 1]
    development = development.round(2)
    kest = np.where(development > 0, -0.275 * development, 0).round(2)

    df["Flatex Depot"] = (deposits + development).round(2)
    df["Depot Entwicklung"] = np.where(has_depot, development, np.nan)
    df["Depot Einzahlung"] = np.where(has_depot, deposits, np.nan)
    df["Kest"] = np.where(has_depot, kest, np.nan)
    df["Netto Gewinn"] = df["Depot Entwicklung"].fillna(0) + df["Kest"].fillna(0)

    df["Bausparer"] = (1200 + np.arange(n_rows) * 25.0).round(2)
    df["Bundespensionskasse"] = (900 + np.arange(n_rows) * 6.5).round(2)
    df["Allianz Vorsorgekasse"] = (1500 + np.arange(n_rows) * 9.0).round(2)
    df["Bundesschatz"] = np.where(np.arange(n_rows) >= n_rows // 3, 3000.0, 0.0)

    # same formula as in the real sheet: =SUM(B:G) + SUM(L:O) + J
    df["Summe"] = (df[["Girokonto", "Sparkonto", "Urlaubskonto", "Kreditkarte", "Flatex Geld", "Flatex Depot",
                       "Bausparer", "Bundespensionskasse", "Allianz Vorsorgekasse", "Bundesschatz"]].sum(axis=1)
                   + df["Kest"].fillna(0)).round(2)

    df["Kommentar"] = pd.Series([None] * n_rows, dtype=object)
    for row, text in {5: "Neuer Job", depot_start_row: "Depot eröffnet", n_rows // 3: "Bundesschatz gekauft",
                      n_rows - 10: "Urlaub"}.items():
        if row < n_rows:
            df.loc[row, "Kommentar"] = text
    return df


def generate_income(dates: pd.Series, seed: int = 42) -> pd.DataFrame:
    """Monthly net salary for the months covered by ``dates``, with 14 salaries a year (June and November double)."""
    rng = np.random.default_rng(seed + 1)
    months = pd.period_range(dates.min(), dates.max(), freq="M")
    raises = 1.03 ** (np.arange(len(months)) // 12)  # +3 % every year
    salary = (2600 * raises + rng.normal(0, 40, len(months))).round(2)
    salary[[m.month in (6, 11) for m in months]] *= 2
    return pd.DataFrame({"Datum": months.to_timestamp(how="end").normalize(), "Nettogehalt": salary})


def write_excel(path: str | Path, with_income: bool = True, **kwargs) -> pd.DataFrame:
    df = generate(**kwargs)
    with pd.ExcelWriter(path) as writer:
        df.to_excel(writer, sheet_name="Net Worth", index=False)
        if with_income:
            generate_income(df["Datum"], seed=kwargs.get("seed", 42)).to_excel(writer, sheet_name="Income", index=False)
    return df


def main() -> None:
    parser = argparse.ArgumentParser("generate_test_data", description=__doc__.splitlines()[0])
    parser.add_argument("output", nargs="?", default="example_NetWorth.xlsx", type=Path)
    parser.add_argument("-n", "--rows", default=150, type=int)
    parser.add_argument("--seed", default=42, type=int)
    args = parser.parse_args()
    write_excel(args.output, n_rows=args.rows, seed=args.seed)
    print(f"Wrote {args.rows} fictional rows to {args.output}")


if __name__ == "__main__":
    main()
