import pytest

from networth import formula as fx
from networth.columns import Column, default_columns, evaluation_order, validate
from networth.excel import excel_formula


class Values(fx.Context):
    def __init__(self, values, ranges=None):
        self.values, self.ranges = values, ranges or {}

    def value(self, ref):
        return self.values[(ref.name, ref.mode)]

    def range_values(self, rng):
        return self.ranges[(rng.start.name, rng.end.name)]


def test_evaluate_like_excel():
    ctx = Values({("A", "row"): 10.0, ("B", "row"): None, ("A", "prev"): 4.0},
                 {("A", "B"): [10.0, None, 5.0]})
    assert fx.evaluate(fx.parse("{A}-{B}*2"), ctx) == 10.0  # empty = 0
    assert fx.evaluate(fx.parse("({A}-{A@prev})/{A@prev}"), ctx) == 1.5
    assert fx.evaluate(fx.parse("IF({A}>0,-{A}*0.275,0)"), ctx) == pytest.approx(-2.75)
    assert fx.evaluate(fx.parse("SUM({A}:{B})+1"), ctx) == 16.0
    assert fx.evaluate(fx.parse("ROUND(2.5)+ROUND(-2.5)+2^3"), ctx) == 8.0
    with pytest.raises(fx.EvalError):
        fx.evaluate(fx.parse("{A}/{B}"), ctx)


def test_excel_translation_matches_original_sheet():
    letters = {c.name: chr(ord("A") + c.position) if c.position < 26 else "A" + chr(ord("A") + c.position - 26)
               for c in default_columns()}
    by_name = {c.name: c for c in default_columns()}
    assert excel_formula(by_name["Summe"].parsed, letters, 493) == "SUM(B493:G493)+SUM(L493:O493)+J493"
    assert excel_formula(by_name["Kest"].parsed, letters, 493) == "IF(H493>0,-H493*0.275,0)"
    assert excel_formula(by_name["Änderung seit Start"].parsed, letters, 7) == "(P7-$P$2)/$P$2"
    assert excel_formula(by_name["Änderung Gesamt"].parsed, letters, 7) == "(P7-P6)/P6"


def test_parse_errors():
    for bad in ["", "{A", "{A}+", "FOO(1)", "{A}:{B}", "IF(1)", "{A@later}", "1 $ 2"]:
        with pytest.raises(fx.FormulaError):
            fx.parse(bad)


def test_rename_keeps_formula():
    assert fx.rename("SUM({A}:{B})+{A@prev}*2", "A", "Konto Neu") == "SUM({Konto Neu}:{B})+{Konto Neu@prev}*2"


@pytest.mark.parametrize("text, value, expr", [
    ("1234.56", 1234.56, ""), ("1.234,56", 1234.56, ""), ("6604,12+895,2", 7499.32, "6604.12+895.2"),
    ("=-10-5", -15.0, "-10-5"), ("", None, ""), ("€ 12", 12.0, ""),
])
def test_number_input(text, value, expr):
    parsed, parsed_expr = fx.parse_number_input(text)
    assert parsed == pytest.approx(value) if value is not None else parsed is None
    assert parsed_expr == expr


def test_number_input_rejects_text():
    with pytest.raises(fx.FormulaError):
        fx.parse_number_input("abc")


def test_default_columns_are_valid():
    validate(default_columns())
    validate(default_columns("basic"))


def test_cycle_detected():
    cols = [Column("A", "formula", "{B}+1", position=1), Column("B", "formula", "{A}+1", position=2)]
    with pytest.raises(ValueError, match="Zirkelbezug"):
        evaluation_order(cols)


def test_prev_reference_to_itself_is_no_cycle():
    validate([Column("A", position=1), Column("Kumuliert", "formula", "{Kumuliert@prev}+{A}", position=2)])
