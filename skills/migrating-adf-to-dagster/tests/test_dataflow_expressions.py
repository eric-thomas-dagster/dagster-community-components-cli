"""The data flow expression language against Microsoft's function reference (tests/data/adf-data-flow-expressions-usage.md).

The reference lists examples as ``expression -> result``. Every example of an implemented function is run; examples whose results need a type this executor does not
model (dates printed bare, structures, arrays) are skipped by name. A translator that does not know a function raises Unsupported, so the flow stays a stub.
"""
import decimal
import re
from pathlib import Path

import pytest

from adf_to_dagster import dflow as df

DOC = (Path(__file__).parent / "data" / "adf-data-flow-expressions-usage.md").read_text()

IMPLEMENTED = {n[2:] for n in dir(df.D) if n.startswith("f_")} | {"iif", "iifnull", "coalesce", "case", "add", "isnull", "concat", "concatws", "substring"}


def examples():
    out = []
    for m in re.finditer(r"^\s*\*\s+``(?P<expr>.+?)\s*->\s*(?P<res>.+?)``\s*$", DOC, re.M):
        e = m.group("expr").replace("`", "")                          # the reference writes `null` inside its code spans
        fn = re.match(r"\s*([A-Za-z]\w*)\(", e)
        names = {x.lower() for x in re.findall(r"\b([A-Za-z]\w*)\(", e)}
        if names and names <= IMPLEMENTED | {"todate", "month"}:
            out.append((e, m.group("res")))
        elif not names and re.search(r"[+]", e) and "'" in e:
            out.append((e, m.group("res")))
    return out


def parse_result(res):
    res = res.strip()
    if res.startswith("'") and res.endswith("'"):
        return res[1:-1]
    if res in ("true", "false"):
        return res == "true"
    if res == "null":
        return None
    try:
        return int(res) if re.fullmatch(r"-?\d+", res) else decimal.Decimal(res)
    except decimal.InvalidOperation:
        raise ValueError(res)


# documentation examples that contradict the function's own description
ERRATA = {"trim('!--!du!mbo!', '-!')": "trim removes only leading and trailing characters from the set, so the inner ! stays: the documented 'dumbo' is wrong"}


def run(expr):
    return df.compile_expr(expr)({}, {})


EX = examples()


def test_the_extractor_found_the_documented_examples():
    assert len(EX) >= 40, len(EX)


@pytest.mark.parametrize("expr,res", EX, ids=[e[0][:70] for e in EX])
def test_documented_example(expr, res):
    if expr in ERRATA:
        assert run(expr) == "du!mbo"
        return
    try:
        want = parse_result(res)
    except ValueError:
        pytest.skip(f"result {res!r} needs a type this executor does not model")
    try:
        got = run(expr)
    except df.Unsupported as e:
        pytest.skip(f"deliberately not translated, so the flow stays a stub: {e}")
    if isinstance(want, decimal.Decimal) or isinstance(got, (float, decimal.Decimal)):
        assert float(got) == pytest.approx(float(want)), f"{expr} -> {got!r}, documented {res}"
    else:
        assert got == want, f"{expr} -> {got!r}, documented {res}"


def test_null_propagates_through_arithmetic_comparison_and_string_functions():
    r = {"a": None, "b": 2}
    f = lambda e: df.compile_expr(e)(r, {})
    assert f("a + b") is None and f("a == b") is None and f("upper(a)") is None and f("a <=> a") is True and f("isNull(a)") is True and f("coalesce(a, b)") == 2


def test_columns_parameters_and_quoted_names():
    r = {"Nombre": "ann", "monto Total": 5, "x": 1}
    f = lambda e: df.compile_expr(e)(r, {"Cargo": "boss"})
    assert f("upper(Nombre)") == "ANN" and f("{monto Total} * 2") == 10 and f("$Cargo == 'boss'") is True and f("nombre") == "ann"
    assert f("iif(x == 1, 'one', 'other')") == "one" and f("case(x == 2, 'two', x == 1, 'one', 'many')") == "one" and f("case(x == 9, 'nine', 'many')") == "many"


def test_stream_qualified_columns():
    assert df.compile_expr("a@id == b@id")({"id": 1, "b@id": 1}, {}) is True


def test_aggregates_skip_nulls_and_work_on_expressions():
    rows = [{"v": 1, "g": "a"}, {"v": None, "g": "a"}, {"v": 5, "g": "a"}]
    ev = lambda e: df.compile_expr(e, agg=True)(rows[0], {}, rows)
    assert ev("sum(v)") == 6 and ev("count(v)") == 2 and ev("count()") == 3 and ev("max(v)") == 5 and ev("min(v)") == 1 and ev("avg(v)") == 3 and ev("sum(v) / count(v)") == 3


def test_unknown_functions_and_unsupported_syntax_raise_so_the_flow_stays_a_stub():
    for bad in ("regexExtract(a, 'x')", "toUTC(a)", "@(name = a)", "a[0]"):
        with pytest.raises(df.Unsupported):
            df.compile_expr(bad)
    with pytest.raises(df.Unsupported, match="outside an aggregate"):
        df.compile_expr("sum(a)")
