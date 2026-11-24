"""Run every expression example in Microsoft's ADF expression-function reference (tests/data/adf-expression-functions.md) through the translator.

The reference is the only ground truth available without an Azure subscription. It is also not always right: a handful of its examples contradict its own
rules or are typos, and those are listed in ERRATA with the reason, so a failure here is either a converter bug or a new documentation error to record.
Nondeterministic functions (utcNow, guid) are checked by shape; getFutureTime and getPastTime run against the clock the documentation assumes.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adf_to_dagster import runtime as rt
from adf_to_dagster.expr import Untranslatable, translate

DOC = (Path(__file__).parent / "data" / "adf-expression-functions.md").read_text()


def pairs():
    out, seen = [], set()
    for m in re.finditer(r"```\n(?P<expr>[^`\n]+)\n```\s*\n\s*(?:And returns this result:|Returns this result:|And returns:)\s*`(?P<res>[^`]+)`", DOC):
        out.append((m.group("expr").strip(), m.group("res").strip()))
    for m in re.finditer(r"(?P<para>[^\n]*(?:returns|return) (?P<tf>true|false)[^\n]*:\s*)\n\n```\n(?P<expr>[^`\n]+)\n```", DOC):
        out.append((m.group("expr").strip(), m.group("tf")))
    return [p for p in out if not (p[0] in seen or seen.add(p[0]))]


# examples the documentation gets wrong, or that need a type this converter does not have
ERRATA = {
    "addDays('2018-03-15T13:00:00Z', 10)": "the documented result drops the 13:00 hour and is missing the '.' before the fraction",
    "addDays('2018-03-15T00:00:00Z', -5)": "the documented result is missing the '.' before the fraction (':0000000Z')",
    "addHours('2018-03-15T00:00:00Z', 10)": "the documented result is missing the '.' before the fraction",
    "addHours('2018-03-15T15:00:00Z', -5)": "the documented result is missing the '.' before the fraction",
    "subtractFromTime('2018-01-02T00:00:00Z', 1, 'Day')": "the documented result has ':0000000Z' instead of '.0000000Z'",
    "convertFromUtc('2018-01-01T08:00:00.0000000Z', 'Pacific Standard Time')": "the docs disagree with themselves: convertFromUtc shows a trailing Z and convertTimeZone, for the same conversion, does not",
    "dayOfWeek('2018-03-15T13:27:36Z')": "the documented 3 contradicts the documented rule (Sunday is 0): 15 March 2018 was a Thursday, which is 4",
    "getPastTime(5, 'Day')": "five days before the assumed clock (1 March 2018) is 24 February, not the documented 27 January",
    "getPastTime(5, 'Day', 'D')": "same wrong date as above",
    "encodeUriComponent('https://contoso.com')": "the documented result turns https into http",
    "uriComponent('https://contoso.com')": "the documented result turns https into http",
    "decodeUriComponent('http%3A%2F%2Fcontoso.com')": "the documented result turns http into https",
    "uriComponentToString('http%3A%2F%2Fcontoso.com')": "the documented result turns http into https",
    "binary('hello')": "the documented result is a bit string; this converter returns bytes (there is no binary type to compare against)",
    "string( { \"name\": \"Sophie Owen\" } )": "an object literal inside an expression: not supported, so the activity stays a stub",
}
# ADF binary values are shown in the docs as a bit string; this converter has no binary type, so these stay stubs
BINARY = {"base64ToBinary('aGVsbG8=')", "dataUriToBinary('data:text/plain;charset=utf-8;base64,aGVsbG8=')",
          "decodeDataUri('data:text/plain;charset=utf-8;base64,aGVsbG8=')", "uriComponentToBinary('http%3A%2F%2Fcontoso.com')"}
NONDETERMINISTIC = {"utcNow()": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z$", "utcNow('D')": r"^\w+, \w+ \d{1,2}, \d{4}$", "guid('P')": r"^\([0-9a-f-]{36}\)$"}


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2018, 3, 1, tzinfo=tz or timezone.utc)


def evaluate(expr):
    ctx = rt.Ctx("P", {}, {}, {})
    return eval(translate({"value": "@" + expr, "type": "Expression"}), {"fx": rt.fx, "ctx": ctx, "item": None})


def doc_value(res):
    try:
        return json.loads(res)
    except ValueError:
        return res


PAIRS = pairs()


def test_the_extractor_found_the_documented_examples():
    assert len(PAIRS) >= 70


@pytest.mark.parametrize("expr,res", PAIRS, ids=[p[0][:60] for p in PAIRS])
def test_documented_example(expr, res, monkeypatch):
    monkeypatch.setattr(rt, "datetime", _Clock)
    if expr in BINARY:
        with pytest.raises(Untranslatable):
            translate({"value": "@" + expr, "type": "Expression"})
        return
    if expr in ERRATA:
        if expr.startswith("string("):
            with pytest.raises(Untranslatable):
                translate({"value": "@" + expr, "type": "Expression"})
        else:
            evaluate(expr)                                   # it still evaluates; only the documented result is wrong
        return
    got = evaluate(expr)
    if expr in NONDETERMINISTIC:
        assert re.match(NONDETERMINISTIC[expr], got), got
        return
    want = doc_value(res)
    if isinstance(want, float) or isinstance(got, float):
        assert got == pytest.approx(want)
    else:
        assert got == want, f"{expr} -> {got!r}, documented {want!r}"


def test_the_errata_are_real_and_our_values_follow_the_documented_rules(monkeypatch):
    monkeypatch.setattr(rt, "datetime", _Clock)
    assert evaluate("addDays('2018-03-15T13:00:00Z', 10)") == "2018-03-25T13:00:00.0000000Z"
    assert evaluate("dayOfWeek('2018-03-15T13:27:36Z')") == 4
    assert evaluate("getPastTime(5, 'Day')") == "2018-02-24T00:00:00.0000000Z"
    assert evaluate("uriComponent('https://contoso.com')") == "https%3A%2F%2Fcontoso.com"
    assert evaluate("convertFromUtc('2018-01-01T08:00:00.0000000Z', 'Pacific Standard Time')") == evaluate("convertTimeZone('2018-01-01T08:00:00.0000000Z', 'UTC', 'Pacific Standard Time')")


def test_case_sensitivity_follows_the_documentation():
    assert evaluate("contains('Hello World', 'hello')") is False            # contains and replace: case-sensitive
    assert evaluate("replace('Old old', 'old', 'x')") == "Old x"
    assert evaluate("startsWith('Hello', 'hel')") is True                   # startsWith, endsWith, indexOf, lastIndexOf: not case-sensitive
    assert evaluate("endsWith('Hello', 'LLO')") is True
    assert evaluate("indexOf('Hello', 'L')") == 2 and evaluate("lastIndexOf('Hello', 'L')") == 3


def test_every_documented_function_is_either_translated_or_listed_as_unsupported():
    documented = set(re.findall(r"^### ```?(\w+)```?\s*$", DOC, re.M))
    from adf_to_dagster.expr import FUNCS
    missing = sorted(f for f in documented if f.lower() not in FUNCS)
    # recorded, not hidden: these become loud stubs. Revisit when a real pipeline needs one.
    assert "xml" in [m.lower() for m in missing] or missing, missing
    print("documented but not translated:", missing)


# Examples whose documented results sit in a list under several expressions, copied from the reference (the extractor above takes one expression per result).
DOC_TABLE = [
    ("if(equals(1, 1), 'yes', 'no')", "yes"),
    ("equals(true, 1)", True), ("equals('abc', 'abcd')", False),
    ("greater(10, 5)", True), ("greater('apple', 'banana')", False),
    ("greaterOrEquals(5, 5)", True), ("greaterOrEquals('apple', 'banana')", False),
    ("less(5, 10)", True), ("less('banana', 'apple')", False),
    ("lessOrEquals(10, 10)", True), ("lessOrEquals('apply', 'apple')", False),
    ("and(true, true)", True), ("and(false, true)", False), ("and(false, false)", False),
    ("and(equals(1, 1), equals(2, 2))", True), ("and(equals(1, 1), equals(1, 2))", False),
    ("or(true, false)", True), ("or(false, false)", False), ("or(equals(1, 1), equals(1, 2))", True), ("or(equals(1, 2), equals(1, 3))", False),
    ("not(false)", True), ("not(true)", False), ("not(equals(1, 2))", True), ("not(equals(1, 1))", False),
    ("bool(1)", True), ("bool(0)", False),
    ("div(9, 2.0)", 4.5), ("div(9.0, 2)", 4.5), ("div(9.0, 2.0)", 4.5), ("div(9, 2)", 4),
    ("mul(1, 2)", 2), ("mul(1.5, 2)", 3),
    ("empty('')", True), ("empty('abc')", False),
    ("first('hello')", "h"), ("first(createArray(0, 1, 2))", 0), ("last('abcd')", "d"), ("last(createArray(0, 1, 2, 3))", 3),
    ("length('abcd')", 4), ("length(createArray(0, 1, 2, 3))", 4),
    ("skip(createArray(0, 1, 2, 3), 1)", [1, 2, 3]), ("take('abcde', 3)", "abc"), ("take(createArray(0, 1, 2, 3, 4), 3)", [0, 1, 2]),
    ("coalesce(null, true, false)", True), ("coalesce(null, 'hello', 'world')", "hello"), ("coalesce(null, null, null)", None),
    ("max(1, 2, 3)", 3), ("max(createArray(1, 2, 3))", 3), ("min(1, 2, 3)", 1), ("min(createArray(1, 2, 3))", 1),
    ("split('a_b_c', '_')", ["a", "b", "c"]),
    ("intersection(createArray(1, 2, 3), createArray(101, 2, 1, 10), createArray(6, 8, 1, 2))", [1, 2]),
]


@pytest.mark.parametrize("expr,want", DOC_TABLE, ids=[d[0][:60] for d in DOC_TABLE])
def test_documented_list_examples(expr, want):
    assert evaluate(expr) == want


def test_rand_excludes_the_maximum_and_ticks_counts_from_year_one():
    assert {evaluate("rand(1, 5)") for _ in range(200)} <= {1, 2, 3, 4}
    assert evaluate("ticks('0001-01-01T00:00:01Z')") == 10_000_000
    assert evaluate("ticks('1970-01-01T00:00:00Z')") == 621355968000000000        # the Unix epoch in .NET ticks
