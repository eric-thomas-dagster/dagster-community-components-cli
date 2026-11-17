import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import convert_ir as C  # noqa: E402

MOD = str(HERE / "fixtures_ir" / "mod.py")


def fn(name, params=()):
    import ast, inspect, textwrap
    src = Path(MOD).read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return {"name": name, "file": MOD, "params": list(params) or [a.arg for a in node.args.args],
            "source": textwrap.dedent(ast.get_source_segment(src, node, padded=False)) if not node.decorator_list else
            "\n".join(src.splitlines()[node.decorator_list[0].lineno - 1:node.end_lineno])}


def task(tid, ty="_PythonDecoratedOperator", up=(), **kw):
    t = {"task_id": tid, "type": ty, "group": None, "upstream": list(up), "downstream": [], "retries": 0, "retry_delay_s": 300.0,
         "pool": None, "trigger_rule": "all_success", "owner": "airflow", "queue": None, "execution_timeout_s": None,
         "is_mapped": False, "is_setup": False, "is_teardown": False, "outlets": [], "inlets": [], "template_fields": {}}
    t.update(kw)
    return t


def dag(tasks, kind="cron_trigger", **tt):
    return {"dag_id": "d1", "file": "d1.py", "description": None, "tags": [], "catchup": False, "max_active_runs": 16,
            "has_callbacks": False, "tasks": tasks,
            "timetable": {"class": "X", "kind": kind, "cron": "0 6 * * *", "timezone": "America/Chicago", **tt}}


def convert(d):
    return C.convert_dag(d, "note", {})


def test_callable_support_code_is_exact_and_free_of_airflow():
    code, sup = C.resolve_callable(fn("double"))
    assert "@task" not in code and code.startswith("def double")
    assert sup == ["SCALE = 3", "def helper(x):\n    return x * SCALE"]
    assert not any("airflow" in s for s in sup)


@pytest.mark.parametrize("name,why", [("uses_context", "context parameters"), ("uses_airflow_api", "Airflow API")])
def test_context_and_airflow_api_callables_are_deferred(name, why):
    with pytest.raises(C.Defer, match=why):
        C.resolve_callable(fn(name))


def test_python_chain_passes_values_and_rewires_empty_operators():
    d = dag([task("start", "EmptyOperator"), task("numbers", "PythonOperator", up=["start"], callable=fn("numbers"), op_args=[], op_kwargs={}),
             task("double", up=["numbers"], callable=fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={})])
    code, s = convert(d)
    compile(code, "x", "exec")
    assert s["assets"] == 2 and s["elided"] == 1
    assert "ins={'in_0': dg.AssetIn(key=['d1', 'numbers'])}" in code
    assert "return _af_numbers()" in code and "\n    _af_double(in_0)" in code      # only a consumed result is returned (stored)
    assert "deps=" not in code.split("def t_numbers")[0].split("@dg.asset")[-1]          # start was elided, no dangling dep
    assert "cron_schedule='0 6 * * *', execution_timezone='America/Chicago'" in code


def test_bash_and_decorated_bash():
    d = dag([task("b", "BashOperator", template_fields={"bash_command": "echo hi", "env": {"A": "1"}}),
             task("bd", "_BashDecoratedOperator", up=["b"], callable=fn("returns_command"), op_args=["x"], op_kwargs={})], kind="manual")
    code, s = convert(d)
    compile(code, "x", "exec")
    assert "run_shell(context, 'echo hi', env={'A': '1'}" in code and "cmd = _af_bd('x')" in code and "ScheduleDefinition" not in code


def test_asset_outlet_becomes_the_key_and_single_asset_trigger_is_eager():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"}, outlets=["s3://bkt/out/file"], inlets=["s3://bkt/in/raw"])],
            kind="asset_triggered", assets=["s3://bkt/in/raw"], asset_logic="single")
    code, s = convert(d)
    assert "key=['s3', 'bkt', 'out', 'file']" in code and "AutomationCondition.eager()" in code and "ScheduleDefinition" not in code
    assert s["external"] == ["s3/bkt/in/raw"]


def test_s3_operators_map_to_runtime_helpers():
    f = lambda **k: {"aws_conn_id": "aws_default", **k}
    d = dag([task("ls", "S3ListOperator", template_fields=f(bucket="b", prefix="p/", delimiter=None)),
             task("rm", "S3DeleteObjectsOperator", up=["ls"], template_fields=f(bucket="b", keys={"xcom_from": "ls", "key": "return_value"}))], kind="manual")
    code, s = convert(d)
    compile(code, "x", "exec")
    assert "s3_list_keys(client, 'b', 'p/', None)" in code and "s3_delete_keys(client, 'b', in_0)" in code
    assert s["resources"] == ["aws_default"]


@pytest.mark.parametrize("mut,why", [
    (lambda d: d["tasks"][0].update(trigger_rule="all_done"), "trigger_rule all_done"),
    (lambda d: d["tasks"][0].update(is_mapped=True), "dynamic task mapping"),
    (lambda d: d["tasks"][0].update(template_fields={"bash_command": "echo {{ ds }}"}), "Jinja"),
    (lambda d: d["tasks"][0].update(type="KubernetesPodOperator"), "operator KubernetesPodOperator"),
    (lambda d: d["timetable"].update(kind="asset_triggered", assets=["a", "b"], asset_logic="mixed"), "mixes AND and OR"),
    (lambda d: d["timetable"].update(kind="other"), "schedule type"),
    (lambda d: d.update(catchup=True), "catchup"),
    (lambda d: d["tasks"][0].update(depends_on_past=True), "depends_on_past"),
    (lambda d: d["tasks"][0].update(pool_slots=2), "pool_slots=2"),
    (lambda d: d["tasks"][0].update(callbacks=["on_failure_callback"]), "task callbacks"),
    (lambda d: d["tasks"][0].update(retry_exponential_backoff=True), "retry_exponential_backoff"),
    (lambda d: d["tasks"][0].update(max_active_tis_per_dag=1), "max_active_tis_per_dag"),
    (lambda d: d["tasks"][0].update(type="PythonOperator", execution_timeout_s=30.0), "execution_timeout on a non-shell task"),
])
def test_unsupported_constructs_defer_the_whole_dag_with_a_reason(mut, why):
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})])
    mut(d)
    with pytest.raises(C.Defer, match=why):
        convert(d)


def test_one_producer_per_asset_across_dags():
    produced = {"s3/b/x": "other"}
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"}, outlets=["s3://b/x"])])
    with pytest.raises(C.Defer, match="already produced"):
        C.convert_dag(d, "n", produced)


def test_generated_project_loads_in_dagster(tmp_path):
    pytest.importorskip("dagster")
    ir = {"airflow_version": "x", "dags": [dag([task("a", "BashOperator", template_fields={"bash_command": "echo a"}),
                                                  task("b", "PythonOperator", up=["a"], callable=fn("numbers"), op_args=[], op_kwargs={})])]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    defs = tmp_path / "pkg" / "defs"
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(defs),
                    "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    import dagster as dg
    mod = importlib.import_module("pkg.defs.d1")
    d = dg.Definitions(assets=[mod.t_a, mod.t_b], jobs=[mod.d1_job], schedules=[mod.d1_schedule])
    assert {a.key.to_user_string() for a in d.assets} == {"d1/a", "d1/b"}
    assert d.get_schedule_def("d1_schedule").cron_schedule == "0 6 * * *"


def test_all_of_several_assets_uses_a_per_asset_condition_on_roots_only():
    d = dag([task("r", "BashOperator", template_fields={"bash_command": "true"}),
             task("c", "BashOperator", up=["r"], template_fields={"bash_command": "true"})],
            kind="asset_triggered", assets=["a1", "a2"], asset_logic="all")
    code, s = convert(d)
    root, child = code.split("def t_r")[0], code.split("def t_c")[0]
    assert code.count("asset_matches(") == 2 and "since_last_handled()" in code and "any_deps_missing()" in code
    assert "AutomationCondition.eager()" in child.split("key=['d1', 'c']")[1]       # downstream tasks follow the root
    assert "deps=[dg.AssetKey(['a1']), dg.AssetKey(['a2'])]" in root and s["external"] == ["a1", "a2"]


def test_any_of_several_assets_is_eager_and_once_has_no_schedule():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})], kind="asset_triggered", assets=["a", "b"], asset_logic="any")
    assert "AutomationCondition.eager()" in convert(d)[0]
    code, _ = convert(dag([task("t", "BashOperator", template_fields={"bash_command": "true"})], kind="once"))
    assert "ScheduleDefinition" not in code


def test_snowflake_copy_and_sql_map_to_snowflake_run():
    copy = task("cp", "CopyFromExternalStageToSnowflakeOperator", args={
        "snowflake_conn_id": "snowflake_default", "stage": "ST", "table": "T", "schema": "RAW", "file_format": "F", "database": "DB", "warehouse": "WH",
        "prefix": "a/", "files": None, "pattern": None, "copy_options": "ON_ERROR = CONTINUE", "validation_mode": None})
    sql = task("q", "SQLExecuteQueryOperator", up=["cp"], template_fields={"sql": "post.sql", "conn_id": "snowflake_default"},
               template_files={"sql": "select 1; select 2;"}, args={"database": "DB"})
    code, s = convert(dag([copy, sql], kind="manual"))
    compile(code, "x", "exec")
    assert "snowflake_run(context, 'snowflake_default', {'warehouse': 'WH', 'database': 'DB', 'schema': 'RAW'}, [copy_into_sql(" in code
    assert "['select 1; select 2;'], split=True)" in code and s["snowflake_resources"] == ["snowflake_default"]


def test_sql_on_a_non_snowflake_connection_or_with_jinja_is_deferred():
    q = task("q", "SQLExecuteQueryOperator", template_fields={"sql": "select 1", "conn_id": "postgres_default"})
    with pytest.raises(C.Defer, match="only Snowflake connections"):
        convert(dag([q], kind="manual"))
    q = task("q", "SQLExecuteQueryOperator", template_fields={"sql": "select '{{ ds }}'", "conn_id": "snowflake_default"})
    with pytest.raises(C.Defer, match="not a plain string"):
        convert(dag([q], kind="manual"))


def test_runtime_copy_sql_matches_airflows_template():
    ns = {}
    exec(C.RUNTIME.split("def snowflake_run")[0].replace("from __future__ import annotations", ""), ns)
    sql = ns["copy_into_sql"](table="T", stage="ST", file_format="F", schema="RAW", prefix="a/", files=["x'y.csv"], copy_options="ON_ERROR = CONTINUE")
    assert " ".join(sql.split()) == "COPY INTO RAW.T FROM @ST/a/ FILES=('x''y.csv') FILE_FORMAT=F ON_ERROR = CONTINUE"


def test_self_dependency_is_dropped_with_a_note_and_third_party_imports_are_reported():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"}, outlets=["s3://b/x"], inlets=["s3://b/x", "s3://b/y"])], kind="manual")
    code, s = convert(d)
    assert "dg.AssetKey(['s3', 'b', 'x'])" not in code.split("def t_t")[1] and "dg.AssetKey(['s3', 'b', 'y'])" in code
    assert "NOTE: t: reads and writes s3/b/x" in code and s["notes"]
    assert C.third_party({"import pandas as pd": 0, "import os": 1, "from numpy import array": 2}) == {"pandas", "numpy"}


def test_chained_writers_of_one_asset_make_the_last_the_producer():
    t = lambda i, up=(): task(i, "BashOperator", up=up, template_fields={"bash_command": "true"}, outlets=["s3://b/x"])
    code, s = convert(dag([t("a"), t("b", ["a"]), t("c", ["b"])], kind="manual"))
    compile(code, "x", "exec")
    assert code.count("key=['s3', 'b', 'x']") == 1 and "key=['d1', 'a']" in code and "key=['d1', 'b']" in code
    assert any("a -> b -> c all update s3/b/x; only c produces" in n for n in s["notes"]) and "NOTE:" in code


def test_unordered_writers_of_one_asset_defer():
    t = lambda i: task(i, "BashOperator", template_fields={"bash_command": "true"}, outlets=["s3://b/x"])
    with pytest.raises(C.Defer, match="not ordered"):
        convert(dag([t("a"), t("b")], kind="manual"))


def test_apply_conversion_plans_converted_dags_and_defers_the_rest():
    import apply_conversion as A
    good = dag([task("start", "EmptyOperator"), task("a", "BashOperator", up=["start"], template_fields={"bash_command": "true"}),
                task("b", "BashOperator", up=["a"], template_fields={"bash_command": "true"})])
    good["dag_id"] = "good"
    bad = dag([task("x", "BashOperator", template_fields={"bash_command": "true"}, trigger_rule="all_done")])
    bad["dag_id"] = "bad"
    ir = {"dags": [good, bad]}
    report = {"dags": {}}
    for dg_ in ir["dags"]:
        try:
            _, summary = C.convert_dag(dg_, "n", {})
            report["dags"][dg_["dag_id"]] = {"status": "converted", **summary}
        except C.Defer as e:
            report["dags"][dg_["dag_id"]] = {"status": "deferred", "reason": str(e)}
    mk = lambda i, kind, dag_id, task_id=None: {"id": i, "kind": kind, "dag_id": dag_id, "task_id": task_id, "status": "pending", "evidence": "", "reason": "", "target": {}}
    m = {"units": [mk("A1", "dag", "good"), mk("A2", "task", "good", "start"), mk("A3", "task", "good", "a"), mk("A4", "task", "good", "b"),
                   mk("B1", "dag", "bad"), mk("B2", "task", "bad", "x")]}
    A.apply(m, ir, report)
    u = {x["id"]: x for x in m["units"]}
    assert u["A1"]["target"] == {"jobs": ["good"], "schedules": [{"name": "good_schedule", "cron": "0 6 * * *"}]}
    assert "EmptyOperator elided" in u["A2"]["target"]["none"]
    assert u["A3"]["target"] == {"assets": ["good/a"], "deps": {"good/a": []}}          # the elided start is not a dependency
    assert u["A4"]["target"] == {"assets": ["good/b"], "deps": {"good/b": ["good/a"]}}
    assert u["B1"]["status"] == u["B2"]["status"] == "deferred" and "trigger_rule all_done" in u["B2"]["reason"]


def test_report_ranks_operators_with_integration_hints(tmp_path):
    ir = {"airflow_version": "x", "dags": [dag([task("a", "BashOperator", template_fields={"bash_command": "true"}),
                                                  task("b", "KubernetesPodOperator")], kind="manual")]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg" / "defs"),
                    "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    ops = json.loads((tmp_path / "r.json").read_text())["operators"]
    assert ops["BashOperator"]["mapped_by_converter"] and ops["BashOperator"]["registry"] == ["shell_command_asset"]
    assert not ops["KubernetesPodOperator"]["mapped_by_converter"] and "k8s_job_asset" in ops["KubernetesPodOperator"]["registry"]


def test_bash_execution_timeout_is_enforced_as_a_failure():
    code, _ = convert(dag([task("b", "BashOperator", template_fields={"bash_command": "sleep 5"}, execution_timeout_s=1.0)], kind="manual"))
    assert "timeout=1.0" in code
    ns = {}
    exec(C.RUNTIME.split("def copy_into_sql")[0].replace("from __future__ import annotations", ""), ns)
    class Ctx:
        class log:
            info = staticmethod(lambda *_: None)
    import dagster as dg
    with pytest.raises(dg.Failure, match="execution_timeout of 1.0 s exceeded"):
        ns["run_shell"](Ctx, "sleep 5", timeout=1.0)


def test_default_pre_post_execute_methods_are_not_callbacks():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"}, callbacks=["pre_execute", "post_execute"])], kind="manual")
    convert(d)       # must not defer
