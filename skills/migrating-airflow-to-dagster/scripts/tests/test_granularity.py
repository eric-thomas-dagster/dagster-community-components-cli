"""--granularity product: steps folded into the asset they feed. Compared against the faithful task granularity on the same IR."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from test_convert_ir import _py, dag, fn, task  # noqa: E402
from test_generated_tests import pytest_run  # noqa: E402

dg = pytest.importorskip("dagster")


def conv(tmp_path, tasks, name, *extra):
    ir = {"airflow_version": "2.10.5", "dags": [dag(tasks, kind="once")]}
    p = tmp_path / name
    p.mkdir()
    (p / "ir.json").write_text(json.dumps(ir))
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(p / "ir.json"), "--out-dir", str(p / "pk" / "defs"), "--report", str(p / "r.json"), "--return-all", *extra],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (p / "pk" / "__init__.py").write_text("")
    return p, json.loads((p / "r.json").read_text())["dags"]["d1"]


def chain(**first):
    return [_py("numbers", "numbers", **first),
            task("double", up=["numbers"], callable=fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={})]


def run_assets(p, modname):
    import importlib
    sys.path.insert(0, str(p))
    try:
        mod = importlib.import_module(f"pk.defs.{modname}")
        assets = [v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]
        res = dg.materialize(assets)
        assert res.success
        return {k.to_user_string(): res.asset_value(k) for a in assets for k in a.keys if True}, mod
    finally:
        sys.path.remove(str(p))
        for m in [m for m in sys.modules if m == "pk" or m.startswith("pk.")]:
            del sys.modules[m]


def test_a_step_feeding_one_task_is_folded_and_the_leaf_value_is_unchanged(tmp_path):
    pt, rt = conv(tmp_path, chain(), "t", "--granularity", "task")
    pp, rp = conv(tmp_path, chain(), "p")
    assert sorted(rt["task_keys"]) == ["double", "numbers"] and len(rt["keys"]) == 2
    assert rp["keys"] == [["d1", "double"]] and rp["folded"] == {"numbers": "double"}
    assert rp["task_keys"] == {"double": "d1/double", "numbers": "d1/double"}            # every Airflow task still maps to something
    dec = rp["granularity"]["decisions"]
    assert dec["numbers"]["role"] == "step" and dec["numbers"]["into"] == "double" and "plain Python" in dec["numbers"]["reason"]
    assert dec["double"]["role"] == "product" and "no downstream task consumes it" in dec["double"]["reason"]
    vt, _ = run_assets(pt, "d1")
    vp, mod = run_assets(pp, "d1")
    assert vp["d1/double"] == vt["d1/double"]                                            # same outcome for the data product
    assert "_v_numbers = _af_numbers()" in (pp / "pk" / "defs" / "d1.py").read_text()


def test_a_step_with_retries_keeps_its_own_restart_point(tmp_path):
    p, r = conv(tmp_path, chain(retries=2), "p", "--granularity", "product")
    assert r["folded"] == {} and len(r["keys"]) == 2
    assert "own restart point" in r["granularity"]["decisions"]["numbers"]["reason"]


def test_fan_out_and_persisted_outputs_are_products(tmp_path):
    tasks = [_py("numbers", "numbers"), task("a", up=["numbers"], callable=fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={}),
             task("b", up=["numbers"], callable=fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={})]
    p, r = conv(tmp_path, tasks, "p", "--granularity", "product")
    assert r["folded"] == {} and "2 downstream tasks" in r["granularity"]["decisions"]["numbers"]["reason"]
    out = _py("numbers", "numbers", outlets=["s3://b/x"])
    p2, r2 = conv(tmp_path, [out, task("double", up=["numbers"], callable=fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={})], "q", "--granularity", "product")
    assert r2["folded"] == {} and "persisted" in r2["granularity"]["decisions"]["numbers"]["reason"]


def test_glue_empty_operators_are_reported_as_glue(tmp_path):
    p, r = conv(tmp_path, [task("start", "EmptyOperator"), _py("numbers", "numbers", up=["start"])], "p", "--granularity", "product")
    assert r["granularity"]["decisions"]["start"]["role"] == "glue"


def test_generated_tests_pass_for_the_product_form(tmp_path):
    p, r = conv(tmp_path, chain(), "p", "--granularity", "product")
    assert r["generated_tests"]["file"] == "tests/test_d1.py"
    res = pytest_run(p)
    assert res.returncode == 0 and "5 passed" in res.stdout, res.stdout + res.stderr


def test_the_default_is_product_and_task_is_the_opt_in_fallback(tmp_path):
    p, r = conv(tmp_path, chain(), "p")
    assert r["granularity"]["mode"] == "product" and r["folded"] == {"numbers": "double"} and len(r["keys"]) == 1
    p2, r2 = conv(tmp_path, chain(), "q", "--granularity", "task")
    assert r2["granularity"] is None and r2["folded"] == {} and len(r2["keys"]) == 2


def test_manifest_targets_and_parity_follow_the_folded_steps(tmp_path):
    import apply_conversion
    sys.path.insert(0, str(HERE.parent / "parity"))
    import compare
    p, r = conv(tmp_path, chain(), "p", "--granularity", "product")
    ir = {"dags": [dag(chain(), kind="once")]}
    man = {"units": [{"id": "A1", "kind": "dag", "dag_id": "d1", "status": "pending", "classification": "MECH"},
                     {"id": "A2", "kind": "task", "dag_id": "d1", "task_id": "numbers", "status": "pending", "classification": "MECH"},
                     {"id": "A3", "kind": "task", "dag_id": "d1", "task_id": "double", "status": "pending", "classification": "MECH"}]}
    apply_conversion.apply(man, ir, {"dags": {"d1": {"status": "converted", **r}}})
    by = {u["id"]: u["target"] for u in man["units"]}
    assert by["A2"] == {"assets": ["d1/double"], "folded_into": "double"}
    assert by["A3"] == {"assets": ["d1/double"], "deps": {"d1/double": []}}              # the consumer's own dependencies, expanded through the folded step
    t = lambda s, v="None": {"state": s, "value": v}          # noqa: E731
    a = {"state": "success", "tasks": {"numbers": t("success", "[1, 2, 3]"), "double": t("success", "[3, 6, 9]")}}
    d = {"state": "success", "tasks": {"numbers": t("success", "[3, 6, 9]"), "double": t("success", "[3, 6, 9]")}}
    assert compare.dag_issues(a, d, {"task_keys": r["task_keys"], "folded": r["folded"]}) == []            # outcomes match; the folded step has no value of its own to compare
    assert compare.dag_issues(a, d, {"task_keys": r["task_keys"]}) != []


def test_an_empty_operator_with_outlets_is_an_asset_not_glue(tmp_path):
    from test_convert_ir import task as _task
    tasks = [_task("producer", "EmptyOperator", outlets=["s3://dataset/example.csv"]), _task("wait", "BashOperator", template_fields={"bash_command": "sleep 1"})]
    p, r = conv(tmp_path, tasks, "p")
    assert sorted(r["task_keys"]) == ["producer", "wait"] and r["glue"] == []
    assert r["granularity"]["decisions"]["producer"]["role"] == "product"
    p2, r2 = conv(tmp_path, [_task("start", "EmptyOperator"), tasks[1]], "q")
    assert r2["glue"] == ["start"] and "start" not in r2["task_keys"]
