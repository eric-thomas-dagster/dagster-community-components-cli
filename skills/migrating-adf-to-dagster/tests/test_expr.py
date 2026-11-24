import pytest

from adf_to_dagster.expr import Untranslatable, translate
from adf_to_dagster.runtime import Ctx, fx


def ev(value, ctx=None, item=None, **ns):
    ctx = ctx or Ctx("P", {"x": "X", "n": 3}, {}, {"v": "V"})
    return eval(translate(value), {"fx": fx, "ctx": ctx, "item": item, **ns})


def E(s):
    return {"value": s, "type": "Expression"}


def test_literals_pass_through():
    assert ev("plain text") == "plain text"
    assert ev(5) == 5 and ev(True) is True and ev(["a", 1]) == ["a", 1]


def test_whole_expression_functions():
    assert ev(E("@concat('a', 'b', string(1))")) == "ab1"
    assert ev(E("@CONCAT('a','b')")) == "ab"          # function names are case-insensitive
    assert ev(E("@if(equals(1, 1), 'y', 'n')")) == "y"
    assert ev(E("@split('a,b', ',')")) == ["a", "b"]
    assert ev(E("@length(createArray(1, 2))")) == 2
    assert ev(E("@or(false, not(false))")) is True
    assert ev(E("@'it''s'".replace("@", "@concat(") + ")")) == "it's"


def test_parameters_variables_item_and_outputs():
    ctx = Ctx("P", {"x": "X"}, {}, {"v": "V"})
    ctx.outputs["A"] = {"firstRow": {"id": 7}, "value": [1, 2]}
    assert ev(E("@pipeline().parameters.x"), ctx) == "X"
    assert ev(E("@variables('v')"), ctx) == "V"
    assert ev(E("@item().name"), ctx, item={"name": "n1"}) == "n1"
    assert ev(E("@activity('A').output.firstRow.id"), ctx) == 7
    assert ev(E("@activity('A').output.value[1]"), ctx) == 2
    assert ev(E("@pipeline().Pipeline"), ctx) == "P"


def test_interpolation_and_escapes():
    assert ev(E("id=@{pipeline().parameters.x}&n=@{pipeline().parameters.n}")) == "id=X&n=3"
    assert ev(E("a @@ b")) == "a @ b"
    assert ev(E("@{pipeline().parameters.x}")) == "X"


def test_optional_access_returns_none_and_plain_access_errors():
    ctx = Ctx("P", {}, {}, {})
    ctx.outputs["A"] = {"a": {}}
    assert ev(E("@activity('A').output.a?.b"), ctx) is None
    with pytest.raises(Exception):
        ev(E("@activity('A').output.a.b"), ctx)


def test_datetime_functions():
    assert ev(E("@formatDateTime('2025-03-04T05:06:07Z', 'yyyy-MM-dd')")) == "2025-03-04"
    assert ev(E("@formatDateTime('2025-03-04T05:06:07Z', 'yyyy-MM-ddTHH:mm:ss')")) == "2025-03-04T05:06:07"
    assert ev(E("@addDays('2025-03-04T00:00:00Z', 1, 'yyyyMMdd')")) == "20250305"
    assert ev(E("@startOfMonth('2025-03-14T10:00:00Z', 'yyyy-MM-dd')")) == "2025-03-01"
    assert ev(E("@utcnow('yyyy')")).isdigit()


def test_trigger_properties():
    ctx = Ctx("P", {}, {}, {}, trigger={"windowStartTime": "2025-01-01T00:00:00.0000000Z"})
    assert ev(E("@trigger().outputs.windowStartTime"), ctx).startswith("2025-01-01")


@pytest.mark.parametrize("src", [
    "@xpath('<a/>', '/a')", "@pipeline().Unknown", "@dataset().x", "@activity('A')", "@linkedService().x", "@bare", "@concat('a'",
])
def test_unsupported_is_refused_not_guessed(src):
    with pytest.raises(Untranslatable):
        translate(E(src))


def test_unresolved_arm_value_is_refused():
    with pytest.raises(Untranslatable):
        translate("[parameters('Conn')]")


def test_dataset_parameters_inside_a_dataset():
    code = translate(E("@concat('t_', dataset().Table)"), ds="ds")
    assert eval(code, {"fx": fx, "ds": {"Table": "x"}}) == "t_x"
