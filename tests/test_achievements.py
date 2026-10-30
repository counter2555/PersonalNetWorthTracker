import pandas as pd
import pytest

from networth.achievements import (_palindrome, _salary_multiples, achievement, build_page, collect,
                                   recovered_drawdowns, streaks)


def test_unlocked_on_first_date_and_stays_unlocked():
    series = pd.Series([500.0, 1200.0, 800.0], index=pd.to_datetime(["2024-01-31", "2024-02-29", "2024-03-31"]))
    a = achievement("🪙", "Notgroschen", "", "Vermögen", "bronze", series, 1000)
    assert a.unlocked == pd.Timestamp("2024-02-29") and a.progress == 1.0
    locked = achievement("🪙", "Fünfstellig", "", "Vermögen", "bronze", series, 10_000)
    assert locked.unlocked is None and locked.progress == pytest.approx(0.12)


def test_shame_costs_points():
    series = pd.Series([1.0], index=pd.to_datetime(["2024-01-31"]))
    assert achievement("🤕", "Autsch", "", "Schande", "gold", series, 1, "", shame=True).points == -50


def test_streaks():
    assert streaks(pd.Series([True, True, False, True, True, True])).tolist() == [1, 2, 0, 1, 2, 3]


def test_recovered_drawdowns():
    # drops 20 % below the peak, but only recovers to a new peak at the end
    assert recovered_drawdowns(pd.Series([1.0, 0.8, 0.9, 1.1])).tolist() == pytest.approx([0, 0, 0, 0.2])


def test_palindrome():
    assert _palindrome(12_321) and _palindrome(-45_654.4)
    assert not _palindrome(1_221) and not _palindrome(22_222) and not _palindrome(12_345)


def test_salary_multiples_use_the_last_twelve_months():
    income = pd.Series([2000.0] * 12 + [3000.0], index=pd.period_range("2024-01", periods=13, freq="M"))
    total = pd.Series([10_000.0, 24_000.0, 36_000.0],
                      index=pd.to_datetime(["2024-11-15", "2024-12-31", "2025-01-31"]))
    multiples = _salary_multiples(total, income)
    assert pd.isna(multiples.iloc[0])  # less than 12 months of salary
    assert multiples.iloc[1:].tolist() == pytest.approx([1.0, 36_000 / 25_000])


def test_collect_without_net_worth_entries():
    income = pd.Series([3000.0] * 14, index=pd.period_range("2024-01", periods=14, freq="M"))
    items = collect(None, income, 1_000_000)
    by_title = {a.title: a for a in items}
    assert by_title["Gutverdiener"].unlocked == pd.Timestamp("2024-01-31")
    assert by_title["Treue Seele"].value == 14
    assert "Notgroschen" not in by_title  # needs net worth entries


def test_filters_can_hide_rows():
    # .ach is a grid, which would override the browser's [hidden] rule and keep filtered rows visible
    page = build_page(collect(None, None, 1_000_000))
    assert "[hidden] { display: none !important; }" in page
