import pytest

from streamsets_to_dagster.el import Untranslatable, translate
from streamsets_to_dagster.runtime import Record, fx

C = {"TABLE_PREFIX": "aws_", "N": "3"}


def ev(s, rec=None, f=None):
    return eval(translate(s), {"fx": fx, "C": C, "rec": rec or Record(), "f": f})


def test_literals_pass_through():
    assert translate("plain") == "'plain'"
    assert translate(5) == "5"


def test_interpolation_and_constants():
    assert ev("jdbc://${TABLE_PREFIX}x/${str:toUpper('db')}") == "jdbc://aws_x/DB"


def test_records_and_comparisons():
    r = Record({"n": "7", "a": {"b": 2}}, {"table": "aws_t"}, "finished-file")
    assert ev("${record:attribute('table') == str:concat(TABLE_PREFIX, 't')}", r) is True
    assert ev("${record:value('/n') > 5 && record:eventType() == 'finished-file'}", r) is True
    assert ev("${record:value('/a/b') + 1}", r) == 3
    assert ev("${record:exists('/zz')}", r) is False
    assert ev("${record:value('/zz') == null ? 'none' : 'some'}", r) == "none"


def test_time_units_and_word_operators():
    assert ev("${10 * SECONDS}") == 10 and ev("${2 * MINUTES}") == 120
    assert ev("${1 eq 1 and not (2 gt 3)}") is True


def test_field_mapper_context():
    f = {"name": "Line Item #", "value": "x", "path": "/Line Item #"}
    assert ev("${str:replaceAll(str:replace(str:toLower(f:name()), ' ', '_'), '#', 'number')}", f=f) == "line_item_number"


def test_escapes_in_regex_strings():
    assert ev("${str:replaceAll('a.b-c', \"[\\\\.\\\\-]\", '_')}") == "a_b_c"


@pytest.mark.parametrize("bad", ["${foo:bar(1)}", "${record:value('/a'}", "${vault:read('x')}"])
def test_unknown_functions_are_refused_not_guessed(bad):
    with pytest.raises(Untranslatable):
        translate(bad)
