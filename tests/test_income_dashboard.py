import pandas as pd
import pytest

from networth.income_dashboard import monthly_income, summarize_income, yearly_income


def salaries(start: str, amounts: list[float]) -> pd.Series:
    return pd.Series(amounts, index=pd.period_range(start, periods=len(amounts), freq="M"), dtype=float)


def test_special_payments_are_split_off():
    # 14 salaries: June and November double
    income = salaries("2024-01", [2000, 2000, 2000, 2000, 2000, 4000, 2000, 2000, 2000, 2000, 4000, 2000])
    monthly = monthly_income(income)
    assert monthly["Sonderzahlungen"].sum() == 4000
    assert monthly.loc[pd.Period("2024-06", "M"), "Gehalt"] == 2000
    assert (monthly["Gehalt"] + monthly["Sonderzahlungen"] == monthly["Gesamt"]).all()


def test_missing_months_count_as_zero():
    income = salaries("2024-01", [2000, 2000]).drop(pd.Period("2024-02", "M"))
    income[pd.Period("2024-04", "M")] = 2000
    monthly = monthly_income(income)
    assert len(monthly) == 4 and monthly["Gesamt"].tolist() == [2000, 0, 0, 2000]


def test_yearly_change_compares_the_same_months():
    income = salaries("2023-01", [2000] * 12 + [2200] * 10)  # 2024 only until October
    yearly = yearly_income(monthly_income(income))
    assert yearly.loc[2024, "Monate"] == 10
    assert yearly.loc[2024, "Veränderung"] == pytest.approx(0.1)
    assert pd.isna(yearly.loc[2023, "Veränderung"])


def test_summary():
    income = salaries("2023-03", [2000] * 10 + [2200] * 12)
    monthly = monthly_income(income)
    summary = summarize_income(monthly, yearly_income(monthly))
    assert summary.window_total == 12 * 2200 and summary.monthly_average == 2200
    assert summary.previous_total is None  # only 22 months of data
    assert summary.regular == 2200
    assert summary.year == 2024 and summary.year_total == 2200 * 12
    assert summary.year_change is None  # 2023 starts in March, so January and February are missing
