import json
import os
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


@pytest.fixture(autouse=True)
def strict_by_default(monkeypatch):
    """In-process conversions defer unsupported tasks (the strict mode); the caveat tests run the CLI, whose default is a marked stub."""
    monkeypatch.setattr(C, "CAVEATS", "off")


def convert(d):
    return C.convert_dag(d, "note", {})


def test_callable_support_code_is_exact_and_free_of_airflow():
    code, sup = C.resolve_callable(fn("double"))
    assert "@task" not in code and code.startswith("def double")
    assert sup == ["SCALE = 3", "def helper(x):\n    return x * SCALE"]
    assert not any("airflow" in s for s in sup)


@pytest.mark.parametrize("name,why", [("uses_airflow_api", "Airflow API"), ("uses_prev_ds", "does not rebuild")])
def test_unsupported_context_and_direct_airflow_api_callables_are_deferred(name, why):
    with pytest.raises(C.Defer, match=why):
        C.resolve_callable(fn(name))


def test_supported_context_names_are_recognised():
    meta = {}
    C.resolve_callable(fn("wants_ds"), None, meta)
    assert meta["ctx_named"] == ["ds"] and meta["needs_ctx"] and not meta["needs_ti"]
    meta = {}
    C.resolve_callable(fn("pulls"), None, meta)
    assert meta["needs_ti"] and meta["needs_ctx"]


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
    (lambda d: d["tasks"][0].update(trigger_rule="custom_rule"), "trigger_rule custom_rule"),
    (lambda d: d["tasks"][0].update(is_mapped=True), "dynamic task mapping"),
    (lambda d: d["tasks"][0].update(template_fields={"bash_command": "echo {{ var.value.x }}"}), "does not provide"),
    (lambda d: d["tasks"][0].update(type="KubernetesPodOperator"), "operator KubernetesPodOperator"),
    (lambda d: d["timetable"].update(kind="asset_triggered", assets=["a", "b"], asset_logic="mixed"), "mixes AND and OR"),
    (lambda d: d["timetable"].update(kind="other"), "schedule type"),
    (lambda d: d.update(catchup=True), "catchup"),
    (lambda d: d["tasks"][0].update(depends_on_past=True), "depends_on_past"),
    (lambda d: d["tasks"][0].update(pool_slots=2), "pool_slots=2"),
    (lambda d: d["tasks"][0].update(callbacks=["on_failure_callback"]), "task callbacks"),
    (lambda d: d["tasks"][0].update(callbacks=["pre_execute"]), "task callbacks or hooks"),
    (lambda d: d.update(dag_settings={"fail_stop": True}), "fail_stop=True"),
    (lambda d: d.update(dag_settings={"max_active_runs": True}), "max_active_runs=True"),
    (lambda d: d.update(dag_settings={"max_active_tasks": 0.5}), "max_active_tasks"),
    (lambda d: d.update(dag_settings={"dagrun_timeout": {"repr": "weird"}}), "dagrun_timeout"),
    (lambda d: d["tasks"][0].update(retry_exponential_backoff=True), "retry_exponential_backoff"),
    (lambda d: d["tasks"][0].update(max_active_tis_per_dag=1, pool="p"), "together with a named pool"),
    (lambda d: d["tasks"][0].update(max_active_tis_per_dagrun=1), "max_active_tis_per_dagrun"),
    (lambda d: d["tasks"][0].update(type="PythonOperator", template_fields={"templates_dict": {"a": "{{ ds }}"}}), "templates_dict"),
    (lambda d: d["tasks"][0].update(template_fields={"bash_command": "echo {{ macros.ds_format_locale(ds, 'a', 'b', 'c') }}"}), "macros"),
    (lambda d: d["tasks"][0].update(type="PythonOperator", execution_timeout_s=30.0), "execution_timeout on a non-shell task"),
])
def test_unsupported_constructs_defer_the_whole_dag_with_a_reason(mut, why):
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})])
    mut(d)
    with pytest.raises(C.Defer, match=why):
        convert(d)


def test_max_active_runs_is_a_job_tag_and_reported_for_the_deployment_limit():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})])
    d["dag_settings"] = {"max_active_runs": 1}
    code, summary = convert(d)
    assert "tags={'airflow/dag_id': 'd1'}" in code and summary["max_active_runs"] == 1


def test_task_and_dag_concurrency_map_to_a_pool_and_an_executor_limit():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"}, max_active_tis_per_dag=2)])
    d["dag_settings"] = {"max_active_tasks": 3}
    code, summary = convert(d)
    assert "pool='d1__t'" in code and summary["pools"] == {"d1__t": 2}
    assert "multiprocess_executor.configured({'max_concurrent': 3})" in code and summary["max_active_tasks"] == 3


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


def test_sql_on_a_non_snowflake_connection_or_with_unsupported_jinja_is_deferred():
    q = task("q", "SQLExecuteQueryOperator", template_fields={"sql": "select 1", "conn_id": "postgres_default"})
    with pytest.raises(C.Defer, match="only Snowflake connections"):
        convert(dag([q], kind="manual"))
    q = task("q", "SQLExecuteQueryOperator", template_fields={"sql": "select '{{ var.value.x }}'", "conn_id": "snowflake_default"})
    with pytest.raises(C.Defer, match="does not provide"):
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
    bad = dag([task("x", "BashOperator", template_fields={"bash_command": "true"}, trigger_rule="custom_rule")])
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
    assert u["B1"]["status"] == u["B2"]["status"] == "deferred" and "trigger_rule custom_rule" in u["B2"]["reason"]


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


def test_default_dag_settings_do_not_defer():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})], kind="manual")
    d["dag_settings"] = {"max_active_tasks": 16, "max_active_runs": 16, "fail_stop": False}
    convert(d)       # defaults are not behaviour changes


# ---- task context, Jinja, Variable -------------------------------------------------------------------------------------------------------

def _runtime():
    pytest.importorskip("dagster")
    ns: dict = {}
    exec(C.RUNTIME.replace("from __future__ import annotations", ""), ns)
    return ns


def _fake_ctx(tags=None, run_id="run1", retry=0):
    import types
    return types.SimpleNamespace(run=types.SimpleNamespace(tags=tags or {}, run_id=run_id), retry_number=retry)


def test_context_is_rebuilt_from_run_tags_and_ds_is_the_utc_date():
    rt = _runtime()
    ctx = rt["af_context"](_fake_ctx({"airflow/logical_date": "2026-07-02T05:00:00+00:00", "airflow/data_interval_end": "2026-07-03T05:00:00+00:00"}),
                           "d", "t", {"p": 1}, {})
    assert ctx["ds"] == "2026-07-02" and ctx["ds_nodash"] == "20260702" and ctx["ts_nodash"] == "20260702T050000"
    assert ctx["data_interval_end"].isoformat() == "2026-07-03T05:00:00+00:00" and ctx["params"] == {"p": 1}
    assert ctx["ti"].try_number == 1 and ctx["dag_run"].dag_id == "d" and ctx["task"].task_id == "t"
    late = rt["af_context"](_fake_ctx({"airflow/logical_date": "2026-07-01T23:00:00-05:00"}), "d", "t", {}, {})
    assert late["ds"] == "2026-07-02"                        # 23:00 in Chicago is already the next UTC date: Airflow's ds is the UTC date
    assert rt["af_context"](_fake_ctx(), "d", "t", {}, {})["ds"]                      # a manual run falls back to now


def test_call_with_context_follows_pythonoperators_binding_rules():
    rt = _runtime()
    call = rt["call_with_context"]
    ctx = {"ds": "D", "run_id": "R", "params": {}}
    assert call(lambda x, ds: (x, ds), [1], {}, ctx) == (1, "D")                      # context fills parameters op_args did not
    assert call(lambda ds, x=0: (ds, x), [], {"x": 5}, ctx) == ("D", 5)
    assert call(lambda ds: ds, [], {"ds": "OVERRIDE"}, ctx) == "OVERRIDE"             # op_kwargs win over context
    assert call(lambda **kw: sorted(kw), [], {"extra": 1}, ctx) == ["ds", "extra", "params", "run_id"]    # **kwargs gets everything
    assert call(lambda x: x, [7], {}, ctx) == 7                                       # a name the context lacks is left alone
    got = call(lambda: rt["get_current_context"]()["ds"], [], {}, ctx)
    assert got == "D"
    with pytest.raises(RuntimeError):
        rt["get_current_context"]()


def test_xcom_pull_reads_upstream_values_and_refuses_what_it_cannot_mirror():
    import dagster as dg
    rt = _runtime()
    ctx = rt["af_context"](_fake_ctx(), "d", "t", {}, {"a": 41, "b": "x"})
    assert ctx["ti"].xcom_pull(task_ids="a") == 41 and ctx["ti"].xcom_pull(task_ids=["a", "b"]) == [41, "x"]
    assert ctx["ti"].xcom_pull(task_ids="zzz") is None and ctx["ti"].xcom_pull(task_ids="zzz", default="d") == "d"      # Airflow: an unknown id gives None
    assert ctx["ti"].xcom_pull(task_ids="a", key="other") is None
    with pytest.raises(NotImplementedError):
        ctx["ti"].xcom_push("return_value", 1)


def test_render_supports_filters_and_macros_and_is_strict(monkeypatch):
    jinja2 = pytest.importorskip("jinja2")
    rt = _runtime()
    ctx = rt["af_context"](_fake_ctx({"airflow/logical_date": "2026-07-02T05:00:00+00:00"}), "d", "t", {"n": 3}, {})
    r = rt["render"]
    assert r(ctx, "{{ ds }}/{{ ds_nodash }}/{{ params.n }}") == "2026-07-02/20260702/3"
    assert r(ctx, "{{ data_interval_start | ds }} {{ macros.ds_add(ds, -1) }} {{ macros.ds_format(ds, '%Y-%m-%d', '%d.%m') }}") == "2026-07-02 2026-07-01 02.07"
    with pytest.raises(jinja2.UndefinedError):
        r(ctx, "{{ nope }}")


def test_variable_reads_the_environment_like_airflows_env_backend(monkeypatch):
    rt = _runtime()
    V = rt["Variable"]
    monkeypatch.setenv("AIRFLOW_VAR_MY_KEY", "v1")
    monkeypatch.setenv("AIRFLOW_VAR_CFG", '{"a": 1}')
    assert V.get("my_key") == "v1" and V.get("cfg", deserialize_json=True) == {"a": 1}
    assert V.get("absent", default_var="d") == "d" and V.get("absent", default="d3") == "d3"
    with pytest.raises(KeyError, match="does not exist"):
        V.get("absent")
    with pytest.raises(NotImplementedError):
        V.set("a", "b")


def test_airflow_schedule_tags_runs_with_the_logical_date_for_both_semantics():
    import datetime as dt
    import zoneinfo
    dg = pytest.importorskip("dagster")
    pytest.importorskip("croniter")
    rt = _runtime()

    @dg.asset
    def x():
        return 1

    job = dg.define_asset_job("j", selection=[x])
    tick = dt.datetime(2026, 7, 2, 0, 0, tzinfo=zoneinfo.ZoneInfo("America/Chicago"))
    for sem, logical, end in (("trigger", "2026-07-02T05:00:00+00:00", "2026-07-02T05:00:00+00:00"),
                              ("data_interval", "2026-07-01T05:00:00+00:00", "2026-07-02T05:00:00+00:00")):
        sched = rt["airflow_schedule"]("s", job, "0 0 * * *", "America/Chicago", sem)
        repo = dg.Definitions(assets=[x], jobs=[job], schedules=[sched]).get_repository_def()
        rr = sched.evaluate_tick(dg.build_schedule_context(scheduled_execution_time=tick, repository_def=repo)).run_requests[0]
        assert rr.tags["airflow/logical_date"] == logical and rr.tags["airflow/data_interval_end"] == end, sem


def test_dates_make_the_dag_use_a_tagged_schedule_and_render_literal_templates_only():
    d = dag([task("b", "BashOperator", template_fields={"bash_command": "echo {{ ds }} {{ params.k }}", "env": {"D": "{{ ds_nodash }}"}})],
            kind="cron_data_interval")
    d["params"] = {"k": "v"}
    code, s = convert(d)
    compile(code, "x", "exec")
    assert s["uses_context"] and "ctx = af_context(context, 'd1', 'b', {'k': 'v'}, {})" in code
    assert "render(ctx, 'echo {{ ds }} {{ params.k }}')" in code and "{'D': render(ctx, '{{ ds_nodash }}')}" in code
    assert "airflow_schedule('d1_schedule', d1_job, '0 6 * * *', 'America/Chicago', 'data_interval')" in code
    code2, s2 = convert(dag([task("b", "BashOperator", template_fields={"bash_command": "echo {{ ds }}"})], kind="cron_trigger"))
    assert "'trigger')" in code2


def test_callable_that_pulls_xcom_gets_its_ancestors_as_inputs_and_they_return_values():
    d = dag([task("a", callable=fn("numbers"), op_args=[], op_kwargs={}), task("m", "EmptyOperator", up=["a"]),
             task("b", up=["m"], callable=fn("pulls"), op_args=[], op_kwargs={})], kind="manual")
    code, s = convert(d)
    compile(code, "x", "exec")
    assert "ins={'in_0': dg.AssetIn(key=['d1', 'a'])}" in code                       # a is an ancestor of b through the elided task
    assert "return _af_a()" in code and "call_with_context(_af_b, [], {}, ctx)" in code
    assert "ctx = af_context(context, 'd1', 'b', {}, {'a': in_0}, keys={'a': ['d1', 'a']})" in code


def test_bash_script_files_are_read_from_the_template_path_or_defer():
    d = dag([task("s", "BashOperator", template_fields={"bash_command": "run.sh "}, template_files={"bash_command": "echo from file"})], kind="manual")
    assert "run_shell(context, 'echo from file'" in convert(d)[0]
    d = dag([task("s", "BashOperator", template_fields={"bash_command": "run.sh "})], kind="manual")
    with pytest.raises(C.Defer, match="names a script file"):
        convert(d)


def test_generated_context_dag_loads_and_runs_in_dagster(tmp_path):
    pytest.importorskip("dagster")
    d = dag([task("a", callable=fn("numbers"), op_args=[], op_kwargs={}),
             task("b", up=["a"], callable=fn("pulls"), op_args=[], op_kwargs={}),
             task("c", callable=fn("wants_ds"), op_args=[], op_kwargs={})], kind="manual")
    d["dag_id"] = "dctx"
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": [d]}))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_ctx" / "defs"),
                    "--report", str(tmp_path / "r.json"), "--return-all"], check=True, capture_output=True)
    (tmp_path / "pkg_ctx" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    import dagster as dg
    mod = importlib.import_module("pkg_ctx.defs.dctx")
    res = dg.materialize([mod.t_c], raise_on_error=False)
    assert res.success and res.asset_value(mod.t_c.key).endswith("x") and res.asset_value(mod.t_c.key)[:2] == "20"      # ds + "x"
    res = dg.materialize([mod.t_a, mod.t_b], raise_on_error=False)
    assert not res.success                              # numbers() returns a list, so pulls() cannot add 1 to it: the failure is loud


def _mapped_ir(source):
    p = task("p", "PythonOperator", callable=fn("fruit_list"), op_args=[], op_kwargs={})
    m = task("m", "PythonOperator", up=["p"] if source["kind"] == "xcom" else [], is_mapped=True,
             callable={**fn("show"), "params": "['item', 'greeting']"}, op_args=[], op_kwargs={},
             expand={"form": "expand", "args": {"item": source}, "fixed": {"greeting": "hi"}, "fixed_unserializable": [], "op_args": False})
    return dag([p, m] if source["kind"] == "xcom" else [m], kind="once")


def test_mapping_over_a_producers_list_runs_one_partition_per_item(tmp_path):
    dg = pytest.importorskip("dagster")
    ir = {"airflow_version": "x", "dags": [_mapped_ir({"kind": "xcom", "wrapper": "PlainXComArg", "task_id": "p", "key": "return_value"})]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    defs = tmp_path / "pkg_map" / "defs"
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(defs),
                    "--report", str(tmp_path / "r.json"), "--mapping", "partitions"], check=True, capture_output=True)
    assert json.loads((tmp_path / "r.json").read_text())["dags"]["d1"]["status"] == "converted"
    (tmp_path / "pkg_map" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    mod = importlib.import_module("pkg_map.defs.d1")
    (tmp_path / "inst").mkdir()
    with dg.DagsterInstance.local_temp(str(tmp_path / "inst")) as instance:
        assert dg.materialize([mod.t_p], instance=instance).success
        repo = dg.Definitions(assets=[mod.t_p, mod.t_m], jobs=[mod.d1_job, mod.d1_items_job], sensors=[mod.d1_fanout]).get_repository_def()
        from dagster._core.definitions.partitions.context import partition_loading_context
        with partition_loading_context(dynamic_partitions_store=instance):
            result = mod.d1_fanout.evaluate_tick(dg.build_sensor_context(instance=instance, repository_def=repo))
        keys = [r.partition_key for r in result.run_requests]
        assert len(keys) == 3 and keys[1] == "plain" and keys[0].startswith("json:") and keys[2].startswith("sha1:")
        assert set(instance.get_dynamic_partitions("d1_items")) == set(keys)
        for k in keys:
            assert dg.materialize([mod.t_m, mod.t_p], selection=[mod.t_m], partition_key=k, instance=instance).success
        assert [g for g, _ in mod.SEEN] == ["hi"] * 3 and {"name": "apple", "n": 1} in [i for _, i in mod.SEEN] and "plain" in [i for _, i in mod.SEEN]
        # a second tick for the same materialization adds nothing new
        with partition_loading_context(dynamic_partitions_store=instance):
            again = mod.d1_fanout.evaluate_tick(dg.build_sensor_context(instance=instance, cursor=result.cursor, repository_def=repo))
        assert not again.run_requests


def test_literal_mapping_runs_each_static_partition(tmp_path):
    dg = pytest.importorskip("dagster")
    ir = {"airflow_version": "x", "dags": [_mapped_ir({"kind": "literal", "value": ["a", 2, {"k": 1}]})]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_lit" / "defs"),
                    "--report", str(tmp_path / "r.json"), "--mapping", "partitions"], check=True, capture_output=True)
    (tmp_path / "pkg_lit" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    mod = importlib.import_module("pkg_lit.defs.d1")
    keys = mod.d1_items.get_partition_keys()
    assert keys == ["a", "json:2", 'json:{"k": 1}']
    for k in keys:
        assert dg.materialize([mod.t_m], partition_key=k).success
    assert [i for _, i in mod.SEEN] == ["a", 2, {"k": 1}]


@pytest.fixture
def partitions_mode(monkeypatch):
    monkeypatch.setattr(C, "MAP_MODE", "partitions")


def test_mapping_over_a_literal_list_is_a_static_partition_set(partitions_mode):
    code, s = convert(_mapped_ir({"kind": "literal", "value": ["a", 2, {"k": 1}]}))
    assert "StaticPartitionsDefinition(['a', 'json:2', 'json:{\"k\": 1}'])" in code and s["mapping"]["kind"] == "literal"
    assert "decode_item(context.partition_key, d1_literal_items)" in code


@pytest.mark.parametrize("mut,why", [
    (lambda ir: ir["tasks"][0].update(upstream=["x"]), "root Python task"),
    (lambda ir: ir["tasks"][-1]["expand"]["args"]["item"].update(wrapper="MapXComArg"), "plain return value"),
    (lambda ir: ir["tasks"][-1].update(type="BashOperator"), "mapping of a BashOperator"),
])
def test_unsupported_mapping_shapes_defer_with_a_reason(mut, why, partitions_mode):
    d = _mapped_ir({"kind": "xcom", "wrapper": "PlainXComArg", "task_id": "p", "key": "return_value"})
    mut(d)
    with pytest.raises(C.Defer, match=why):
        convert(d)


def _convert_and_load(tmp_path, ir, pkg, *extra):
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"),
                        "--report", str(tmp_path / "r.json"), *extra], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["d1"]
    (tmp_path / pkg / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    assert rep["status"] == "converted", rep
    return rep, importlib.import_module(f"{pkg}.defs.d1")


def _xcom_src(tid):
    return {"kind": "xcom", "wrapper": "PlainXComArg", "task_id": tid, "key": "return_value"}


def test_graph_mapping_maps_chains_and_collects_for_a_reduce_step(tmp_path):
    dg = pytest.importorskip("dagster")
    pl = task("p", "PythonOperator", callable=fn("fruit_list"), op_args=[], op_kwargs={})
    show = task("m", "PythonOperator", up=["p"], is_mapped=True, callable={**fn("show"), "params": "['item', 'greeting']"}, op_args=[], op_kwargs={},
                expand={"form": "expand", "args": {"item": _xcom_src("p")}, "fixed": {"greeting": "hi"}, "fixed_unserializable": [], "op_args": False})
    red = task("r", "PythonOperator", up=["m"], callable=fn("total"), op_args=[{"xcom_from": "m", "key": "return_value"}], op_kwargs={})
    nums = task("n", "PythonOperator", is_mapped=True, callable={**fn("double_item"), "params": "['x']"}, op_args=[], op_kwargs={},
                expand={"form": "expand", "args": {"x": {"kind": "literal", "value": [1, 2, 3]}}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    nums2 = task("n2", "PythonOperator", up=["n"], is_mapped=True, callable={**fn("double_item"), "params": "['x']"}, op_args=[], op_kwargs={},
                 expand={"form": "expand", "args": {"x": _xcom_src("n")}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([pl, show, red, nums, nums2], kind="once")]}, "pkg_graph")
    assert rep["status"] == "converted" and rep["mapping"]["kind"] == "graph"
    res = mod.d1_graph.node_def and dg.materialize([mod.d1_graph])
    assert res.success
    got = {k.to_user_string(): res.asset_value(k) for k in [dg.AssetKey(["d1", t]) for t in ("p", "m", "r", "n", "n2")]}
    assert got["d1/r"] == 1 + 1 + 1 and got["d1/n"] == [2, 4, 6] and got["d1/n2"] == [4, 8, 12]
    assert [g for g, _ in mod.SEEN] == ["hi"] * 3 and len(got["d1/m"]) == 3


def test_graph_mapping_runs_a_shell_command_per_expanded_value(tmp_path):
    dg = pytest.importorskip("dagster")
    out = tmp_path / "out.txt"
    cmds = [f"echo one >> {out}", f"echo two >> {out}"]
    b = task("b", "BashOperator", is_mapped=True, expand={"form": "expand", "args": {"bash_command": {"kind": "literal", "value": cmds}},
                                                         "fixed": {}, "fixed_unserializable": [], "partial": {}, "op_args": False})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([b], kind="once")]}, "pkg_graph_sh")
    assert rep["status"] == "converted"
    assert dg.materialize([mod.d1_graph]).success
    assert sorted(out.read_text().split()) == ["one", "two"]


def test_graph_mapping_supports_op_args_and_expand_kwargs(tmp_path):
    dg = pytest.importorskip("dagster")
    a = task("a", "PythonOperator", is_mapped=True, callable={**fn("double_item"), "params": "['x']"}, op_args=[], op_kwargs={},
             expand={"form": "expand", "args": {"op_args": {"kind": "literal", "value": [[1], [2]]}}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    k = task("k", "PythonOperator", is_mapped=True, callable={**fn("double_item"), "params": "['x']"}, op_args=[], op_kwargs={},
             expand={"form": "expand_kwargs", "args": {"*": {"kind": "literal", "value": [{"x": 5}, {"x": 6}]}}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([a, k], kind="once")]}, "pkg_graph_args")
    assert rep["status"] == "converted"
    res = dg.materialize([mod.d1_graph])
    assert res.success and res.asset_value(dg.AssetKey(["d1", "a"])) == [2, 4] and res.asset_value(dg.AssetKey(["d1", "k"])) == [10, 12]


def test_graph_mapping_over_map_concat_and_zip_sources(tmp_path):
    dg = pytest.importorskip("dagster")
    a = task("a", "PythonOperator", callable=fn("three"), op_args=[], op_kwargs={})
    b = task("b", "PythonOperator", callable=fn("two"), op_args=[], op_kwargs={})
    dbl = {**fn("double_item"), "params": ["x"]}
    ex = lambda src: {"form": "expand", "args": {"x": src}, "fixed": {}, "fixed_unserializable": [], "op_args": False}
    m = task("m", "PythonOperator", up=["a"], is_mapped=True, callable=dict(dbl), op_args=[], op_kwargs={},
             expand=ex({"kind": "xcom", "wrapper": "MapXComArg", "inner": _xcom_src("a"), "callables": [dbl, dbl]}))
    c = task("c", "PythonOperator", up=["a", "b"], is_mapped=True, callable=dict(dbl), op_args=[], op_kwargs={},
             expand=ex({"kind": "xcom", "wrapper": "ConcatXComArg", "parts": [_xcom_src("a"), _xcom_src("b")]}))
    z = task("z", "PythonOperator", up=["a", "b"], is_mapped=True, callable=dict(dbl), op_args=[], op_kwargs={},
             expand=ex({"kind": "xcom", "wrapper": "ZipXComArg", "parts": [_xcom_src("a"), _xcom_src("b")], "fillvalue": None}))
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([a, b, m, c, z], kind="once")]}, "pkg_graph_shapes")
    assert rep["status"] == "converted", rep
    res = dg.materialize([mod.d1_graph])
    assert res.success
    val = lambda t: res.asset_value(dg.AssetKey(["d1", t]))
    assert val("m") == [8, 16, 24]          # two .map doublings, then the task's own
    assert val("c") == [2, 4, 6, 20, 40] and val("z") == [(1, 10, 1, 10), (2, 20, 2, 20)]


def test_untranslatable_task_becomes_a_marked_stub_and_the_rest_of_the_dag_converts(tmp_path):
    dg = pytest.importorskip("dagster")
    a = task("a", "PythonOperator", callable=fn("three"), op_args=[], op_kwargs={})
    b = task("b", "PythonOperator", up=["a"], callable=fn("uses_airflow_api"), op_args=[], op_kwargs={})
    c = task("c", "PythonOperator", up=["a"], callable=fn("three"), op_args=[], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([a, b, c], kind="once")]}, "pkg_caveat")
    assert rep["status"] == "converted" and [x["task"] for x in rep["caveats"]] == ["b"] and "Airflow API" in rep["caveats"][0]["reason"]
    res = dg.materialize([mod.t_a, mod.t_b, mod.t_c], raise_on_error=False)
    assert not res.success
    failed = [e for e in res.get_step_failure_events()]
    assert len(failed) == 1 and "AIRFLOW-CAVEAT" in failed[0].event_specific_data.error.message
    ok = dg.materialize([mod.t_a, mod.t_c])                   # the rest of the DAG is intact
    assert ok.success


def test_unknown_operator_and_unsupported_template_become_stubs_in_a_dag_that_otherwise_converts(tmp_path):
    dg = pytest.importorskip("dagster")
    a = task("a", "PythonOperator", callable=fn("three"), op_args=[], op_kwargs={})
    k = task("k", "SageMakerTransformOperator", up=["a"])
    c = task("c", "PythonOperator", up=["k"], trigger_rule="all_done", callable=fn("three"), op_args=[], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([a, k, c], kind="once")]}, "pkg_caveat2")
    assert [x["task"] for x in rep["caveats"]] == ["k"] and "SageMakerTransformOperator" in rep["caveats"][0]["reason"]
    assert "AIRFLOW-CAVEAT" in (tmp_path / "pkg_caveat2" / "defs" / "d1.py").read_text()


def test_xcom_push_becomes_materialization_metadata_and_a_keyed_pull_reads_it(tmp_path):
    dg = pytest.importorskip("dagster")
    a = task("a", "PythonOperator", callable=fn("pusher"), op_args=[], op_kwargs={})
    b = task("b", "PythonOperator", up=["a"], callable=fn("puller"), op_args=[], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([a, b], kind="once")]}, "pkg_xpush", "--return-all")
    assert rep["status"] == "converted"
    (tmp_path / "inst").mkdir()
    with dg.DagsterInstance.local_temp(str(tmp_path / "inst")) as instance:
        res = dg.materialize([mod.t_a, mod.t_b], instance=instance)
        assert res.success
        assert res.asset_value(dg.AssetKey(["d1", "b"])) == (3, {"a": [1, 2]}, "d")
        md = res.get_asset_materialization_events()[0].materialization.metadata
        assert md["count"].value == 3 and md["detail"].value == {"a": [1, 2]}


def test_a_shell_tasks_last_stdout_line_is_its_value_for_downstream_tasks(tmp_path):
    dg = pytest.importorskip("dagster")
    b = task("b", "BashOperator", template_fields={"bash_command": "echo first; echo last"})
    c = task("c", "PythonOperator", up=["b"], callable=fn("passthrough"), op_args=[{"xcom_from": "b", "key": "return_value"}], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([b, c], kind="once")]}, "pkg_bashout", "--return-all")
    assert rep["status"] == "converted"
    res = dg.materialize([mod.t_b, mod.t_c])
    assert res.success and res.asset_value(dg.AssetKey(["d1", "c"])) == "last"


def _py(tid, fname, up=(), **kw):
    return task(tid, "PythonOperator", up=list(up), callable=fn(fname), op_args=[], op_kwargs={}, **kw)


def _run_gated(tmp_path, tasks, pkg):
    dg = pytest.importorskip("dagster")
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag(tasks, kind="once")]}, pkg, "--return-all")
    assert rep["status"] == "converted", rep
    mod.TRACE.clear()
    res = dg.materialize([mod.d1_graph])
    assert res.success
    return mod, res


def test_a_branch_runs_the_chosen_path_and_a_join_with_min_one_success_still_runs(tmp_path):
    t = [_py("start", "step_a"),
         _py("br", "choose_a", up=["start"], **{}) | {"type": "BranchPythonOperator"},
         _py("path_a", "step_a", up=["br"]), _py("path_b", "step_b", up=["br"]),
         _py("join", "step_join", up=["path_a", "path_b"], trigger_rule="none_failed_min_one_success"),
         _py("strict", "step_end", up=["path_a", "path_b"])]
    mod, res = _run_gated(tmp_path, t, "pkg_branch")
    assert sorted(mod.TRACE) == ["a", "a", "join"]              # start, path_a, join; path_b and the all_success join were skipped


def test_a_branch_that_chooses_nothing_skips_everything_after_it(tmp_path):
    t = [_py("br", "choose_none") | {"type": "BranchPythonOperator"}, _py("x", "step_a", up=["br"]),
         _py("j", "step_join", up=["x"], trigger_rule="none_failed")]
    mod, _ = _run_gated(tmp_path, t, "pkg_branch0")
    assert mod.TRACE == ["join"]                                # none_failed runs after a skipped upstream, as in Airflow


def test_short_circuit_skips_all_downstream_or_only_the_next_task(tmp_path):
    chain = lambda hard: [_py("sc", "is_false") | {"type": "ShortCircuitOperator", "short_circuit_hard": hard}, _py("x", "step_a", up=["sc"]),
                          _py("y", "step_b", up=["x"], trigger_rule="none_failed")]
    mod, _ = _run_gated(tmp_path, chain(True), "pkg_sc_hard")
    assert mod.TRACE == []                                      # ignore_downstream_trigger_rules: everything downstream skipped
    mod, _ = _run_gated(tmp_path / "soft", chain(False), "pkg_sc_soft") if (tmp_path / "soft").mkdir() is None else None
    assert mod.TRACE == ["b"]                                   # only the direct child skipped; y's own rule lets it run


def test_shell_output_is_parsed_as_json_on_airflow_3_only(tmp_path, monkeypatch):
    dg = pytest.importorskip("dagster")
    b = task("b", "BashOperator", template_fields={"bash_command": "echo '{\"a\": 1}'"})
    c = task("c", "PythonOperator", up=["b"], callable=fn("passthrough"), op_args=[{"xcom_from": "b", "key": "return_value"}], op_kwargs={})
    out = {}
    for ver in ("2.10.5", "3.4.0"):
        rep, mod = _convert_and_load(tmp_path / ver, {"airflow_version": ver, "dags": [dag([b, c], kind="once")]}, f"pkg_json_{ver[0]}", "--return-all") if (tmp_path / ver).mkdir() is None else None
        res = dg.materialize([mod.t_b, mod.t_c])
        out[ver] = res.asset_value(dg.AssetKey(["d1", "c"]))
    assert out["2.10.5"] == '{"a": 1}' and out["3.4.0"] == {"a": 1}


def test_a_shell_task_with_do_xcom_push_false_passes_nothing_downstream(tmp_path):
    dg = pytest.importorskip("dagster")
    b = task("b", "BashOperator", template_fields={"bash_command": "echo hi"}, do_xcom_push=False)
    c = task("c", "PythonOperator", up=["b"], callable=fn("passthrough"), op_args=[{"xcom_from": "b", "key": "return_value"}], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([b, c], kind="once")]}, "pkg_nopush", "--return-all")
    res = dg.materialize([mod.t_b, mod.t_c])
    assert res.success and res.asset_value(dg.AssetKey(["d1", "c"])) is None


def test_clock_branches_follow_the_chosen_task(tmp_path):
    t = [task("w", "BranchDayOfWeekOperator", args={"follow_task_ids_if_true": "yes", "follow_task_ids_if_false": "no", "use_task_logical_date": False,
                                                   "week_day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}),
         _py("yes", "step_a", up=["w"]), _py("no", "step_b", up=["w"]),
         task("d", "BranchDateTimeOperator", args={"follow_task_ids_if_true": "in", "follow_task_ids_if_false": "out", "use_task_logical_date": False,
                                                   "target_lower": {"datetime": "2000-01-01T00:00:00+00:00"}, "target_upper": {"datetime": "2999-01-01T00:00:00+00:00"}}),
         _py("in", "step_join", up=["d"]), _py("out", "step_end", up=["d"])]
    mod, res = _run_gated(tmp_path, t, "pkg_clock")
    assert sorted(mod.TRACE) == ["a", "join"]                     # the weekday check is true every day; the date window is open


def test_a_mapped_task_over_an_empty_list_is_skipped_and_so_is_what_follows(tmp_path):
    dg = pytest.importorskip("dagster")
    m = task("m", "PythonOperator", is_mapped=True, callable={**fn("double_item"), "params": "['x']"}, op_args=[], op_kwargs={},
             expand={"form": "expand", "args": {"x": {"kind": "literal", "value": []}}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    r = _py("r", "step_a", up=["m"])
    mod, res = _run_gated(tmp_path, [m, r], "pkg_empty")
    assert mod.TRACE == []                                        # r is skipped, as in Airflow


def _sensor(tid, ty, up=(), poke=0.01, timeout=5.0, soft=False, **kw):
    return task(tid, ty, up=list(up), sensor={"poke_interval": poke, "timeout": timeout, "mode": "poke", "soft_fail": soft,
                                              "exponential_backoff": False, "silent_fail": False, "never_fail": False, **kw.pop("sn", {})}, **kw)


def test_python_sensor_polls_until_the_callable_is_true(tmp_path):
    dg = pytest.importorskip("dagster")
    s = _sensor("s", "PythonSensor", callable=fn("ready_after_two"), op_args=[], op_kwargs={})
    after = _py("after", "step_a", up=["s"])
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([s, after], kind="once")]}, "pkg_sens1", "--return-all")
    assert rep["status"] == "converted", rep
    res = dg.materialize([mod.t_s, mod.t_after])
    assert res.success and len(mod.POLLS) == 3 and mod.TRACE == ["a"]


def test_a_sensor_that_times_out_fails_the_run_or_is_skipped_with_soft_fail(tmp_path):
    dg = pytest.importorskip("dagster")
    s = _sensor("s", "PythonSensor", timeout=0.05, callable=fn("never_ready"), op_args=[], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path / "a", {"airflow_version": "x", "dags": [dag([s], kind="once")]}, "pkg_sens2") if (tmp_path / "a").mkdir() is None else None
    assert not dg.materialize([mod.t_s], raise_on_error=False).success                          # times out: the task fails
    soft = _sensor("s", "PythonSensor", timeout=0.05, soft=True, callable=fn("never_ready"), op_args=[], op_kwargs={})
    after = _py("after", "step_b", up=["s"])
    mod, res = _run_gated(tmp_path / "b", [soft, after], "pkg_sens3") if (tmp_path / "b").mkdir() is None else None
    assert mod.TRACE == []                                                                       # soft_fail: skipped, so is what follows


def test_bash_file_time_and_timedelta_sensors(tmp_path):
    dg = pytest.importorskip("dagster")
    f = tmp_path / "ready.txt"; f.write_text("x")
    t = [_sensor("b", "BashSensor", template_fields={"bash_command": "exit 0", "env": None}, args={"retry_exit_code": None}),
         _sensor("r", "BashSensor", template_fields={"bash_command": "exit 3", "env": None}, args={"retry_exit_code": 3}, timeout=0.05),
         _sensor("f", "FileSensor", template_fields={"filepath": str(f)}, args={"recursive": False, "fs_conn_id": "fs_default"}),
         _sensor("t", "TimeSensor", sn={"target_datetime": "2000-01-01T06:00:00+00:00"}),
         _sensor("d", "TimeDeltaSensor", args={"delta": {"seconds": 0.0}})]
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag(t, kind="once")]}, "pkg_sens4", "--return-all")
    assert rep["status"] == "converted", rep
    for name in ("b", "f", "t", "d"):
        assert dg.materialize([getattr(mod, f"t_{name}")]).success, name
    assert not dg.materialize([mod.t_r], raise_on_error=False).success                          # exit code 3 = retry later, then the timeout


def _run_failing(tmp_path, tasks, pkg):
    dg = pytest.importorskip("dagster")
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag(tasks, kind="once")]}, pkg, "--return-all")
    assert rep["status"] == "converted", rep
    getattr(mod, "TRACE", []).clear()
    return mod, dg.materialize([mod.d1_graph], raise_on_error=False)


def test_all_done_runs_after_a_failure_and_the_run_still_fails(tmp_path):
    t = [_py("a", "boom"), _py("next", "step_a", up=["a"]), _py("cleanup", "step_join", up=["a"], trigger_rule="all_done"),
         _py("alert", "step_b", up=["a"], trigger_rule="one_failed"), _py("fine", "step_end", up=["cleanup"], trigger_rule="none_failed")]
    mod, res = _run_failing(tmp_path, t, "pkg_fail1")
    assert not res.success
    assert sorted(mod.TRACE) == ["b", "end", "join"]            # cleanup, the one_failed alert and what follows it ran; `next` (all_success) did not


def test_one_failed_and_all_done_after_a_success_behave_as_airflow(tmp_path):
    t = [_py("a", "step_a"), _py("alert", "step_b", up=["a"], trigger_rule="one_failed"), _py("cleanup", "step_join", up=["a"], trigger_rule="all_done")]
    mod, res = _run_failing(tmp_path, t, "pkg_fail2")
    assert res.success and sorted(mod.TRACE) == ["a", "join"]    # the alert is skipped when nothing failed


def _setup_teardown(setup_fn="step_a", work_fn="step_b", teardown_fn="step_join", fail_run=False):
    return [_py("setup", setup_fn, is_setup=True), _py("work", work_fn, up=["setup"]),
            _py("teardown", teardown_fn, up=["work", "setup"], is_teardown=True, trigger_rule="all_done_setup_success", on_failure_fail_dagrun=fail_run)]


def test_setup_work_teardown_all_run_in_order(tmp_path):
    mod, res = _run_failing(tmp_path, _setup_teardown(), "pkg_st1")
    assert res.success and mod.TRACE == ["a", "b", "join"]


def test_a_teardown_runs_after_the_work_fails_and_the_run_fails(tmp_path):
    mod, res = _run_failing(tmp_path, _setup_teardown(work_fn="boom"), "pkg_st2")
    assert not res.success and mod.TRACE == ["a", "join"]         # setup ran, the work failed, the teardown still ran


def test_a_failed_setup_skips_the_work_and_the_teardown_and_fails_the_run(tmp_path):
    mod, res = _run_failing(tmp_path, _setup_teardown(setup_fn="boom"), "pkg_st3")
    assert not res.success and mod.TRACE == []                    # nothing was set up, so nothing is torn down


def test_a_failed_teardown_does_not_fail_the_run_unless_it_asks_to(tmp_path):
    mod, res = _run_failing(tmp_path / "x", _setup_teardown(teardown_fn="boom"), "pkg_st4") if (tmp_path / "x").mkdir() is None else None
    assert res.success
    mod, res = _run_failing(tmp_path / "y", _setup_teardown(teardown_fn="boom", fail_run=True), "pkg_st5") if (tmp_path / "y").mkdir() is None else None
    assert not res.success


def test_a_dag_of_only_empty_operators_converts_to_steps_that_do_nothing(tmp_path):
    dg = pytest.importorskip("dagster")
    t = [task("a", "EmptyOperator"), task("b", "EmptyOperator", up=["a"])]
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag(t, kind="once")]}, "pkg_empty_only")
    assert rep["status"] == "converted" and dg.materialize([mod.d1_graph]).success


def test_task_callbacks_run_through_on_hooks_after_the_last_attempt(tmp_path):
    dg = pytest.importorskip("dagster")
    pytest.importorskip("dagster_community_components")
    ok = _py("ok", "step_a", callbacks=["on_success_callback"], callback_fns={"on_success_callback": [fn("note_success")]})
    bad = _py("bad", "boom", callbacks=["on_failure_callback"], callback_fns={"on_failure_callback": [fn("note_failure")]})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([ok, bad], kind="once")]}, "pkg_cb")
    assert rep["status"] == "converted" and "dagster-community-components" in rep["requirements"], rep
    res = dg.materialize([mod.t_ok, mod.t_bad], raise_on_error=False)
    assert not res.success
    assert mod.CALLBACKS == [("success",), ("failure", "RuntimeError", 1)] or sorted(mod.CALLBACKS) == [("failure", "RuntimeError", 1), ("success",)]


def test_trigger_dag_run_launches_the_other_converted_dag_and_fails_with_it(tmp_path):
    pytest.importorskip("dagster")
    child = {**dag([_py("work", "step_a")], kind="once"), "dag_id": "child"}
    parent = dag([_py("first", "step_b"), task("fire", "TriggerDagRunOperator", up=["first"], template_fields={"conf": {"k": 1}},
                                                args={"trigger_dag_id": "child", "wait_for_completion": True})], kind="once")
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": [parent, child]}))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_trig" / "defs"),
                    "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]
    assert rep["d1"]["status"] == "converted" and rep["child"]["status"] == "converted", rep
    (tmp_path / "pkg_trig" / "__init__.py").write_text("")
    (tmp_path / "run.py").write_text('''
import importlib, json, os
import dagster as dg
if __name__ == "__main__":                       # the launched run is a spawned process that imports this file again
    mod = importlib.import_module("pkg_trig.defs.d1")
    os.makedirs("inst", exist_ok=True)
    with dg.DagsterInstance.local_temp("inst") as inst:
        r = dg.materialize([mod.t_first, mod.t_fire], instance=inst, raise_on_error=False)
        print("RESULT", json.dumps({"success": r.success, "runs": [[x.job_name, x.status.value, x.tags.get("airflow/conf")] for x in inst.get_runs()]}))
''')
    out = subprocess.run([sys.executable, "run.py"], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True, text=True)
    result = json.loads(next(l for l in out.stdout.splitlines() if l.startswith("RESULT")).split(" ", 1)[1])
    assert result["success"] and ["child", "SUCCESS", '{"k": 1}'] in result["runs"]


def test_a_callable_that_raises_airflow_skip_exception_skips_the_step_and_what_follows(tmp_path):
    t = [_py("s", "skips_itself"), _py("after", "step_a", up=["s"]), _py("anyway", "step_b", up=["s"], trigger_rule="none_failed")]
    mod, res = _run_gated(tmp_path, t, "pkg_skipexc")
    assert sorted(mod.TRACE) == ["b"]                              # `after` (all_success) is skipped, `anyway` (none_failed) runs


def test_airflow_fail_exception_fails_the_step_and_a_sensor_can_return_a_poke_value(tmp_path):
    dg = pytest.importorskip("dagster")
    mod, res = _run_failing(tmp_path, [_py("f", "fails_hard", trigger_rule="all_done")], "pkg_failexc")
    assert not res.success
    s = _sensor("s", "PythonSensor", callable=fn("poke_value"), op_args=[], op_kwargs={})
    rep, mod2 = _convert_and_load(tmp_path / "p", {"airflow_version": "x", "dags": [dag([s], kind="once")]}, "pkg_poke", "--return-all") if (tmp_path / "p").mkdir() is None else None
    res2 = dg.materialize([mod2.t_s])
    assert res2.success and res2.asset_value(dg.AssetKey(["d1", "s"])) == "done"


def test_dataset_keys_can_follow_the_script_orchestrator_naming(monkeypatch):
    monkeypatch.setattr(C, "DATASET_KEYS", "script-orchestrator")
    assert C.key_from_asset("file://incoming/player-stats/team_a.csv") == ["airflow_dataset_incoming_player_stats_team_a_csv"]
    assert C.key_from_asset("s3://my-bucket/raw/data.csv") == ["airflow_dataset_s3_my_bucket_raw_data_csv"]      # the component's scheme strip is [a-z]+, so `s3` stays
    assert C.key_from_asset("raw_data") == ["airflow_dataset_raw_data"]
    monkeypatch.setattr(C, "DATASET_KEYS", "path")
    assert C.key_from_asset("s3://my-bucket/raw/data.csv") == ["s3", "my_bucket", "raw", "data_csv"]


def _cron_partition_dag(dag_id, task_id, cron, outlet):
    d = dag([task(task_id, "PythonOperator", callable=fn("step_a"), op_args=[], op_kwargs={}, outlets=[outlet])], kind="cron_partition")
    d["dag_id"] = dag_id
    d["timetable"] = {"class": "CronPartitionTimetable", "kind": "cron_partition", "timezone": "UTC",
                      "partition": {"__type": "airflow.timetables.trigger.CronPartitionTimetable",
                                    "__var": {"expression": cron, "timezone": "UTC", "run_offset": None, "key_format": "%Y-%m-%dT%H:%M:%S"}}}
    return d


def _partitioned_consumer(dag_id, assets, mapper_type, outlet="file://out/combined.csv"):
    d = dag([task("combine", "PythonOperator", callable=fn("step_b"), op_args=[], op_kwargs={}, outlets=[outlet])], kind="partitioned_asset")
    d["dag_id"] = dag_id
    d["timetable"] = {"class": "PartitionedAssetTimetable", "kind": "partitioned_asset", "timezone": "UTC", "assets": assets, "asset_logic": "all",
                      "partition": {"__type": "airflow.timetables.simple.PartitionedAssetTimetable",
                                    "__var": {"default_partition_mapper": {"__type": f"airflow.partition_mappers.temporal.{mapper_type}", "__var": {}},
                                              "partition_mapper_config": []}}}
    return d


def test_partitioned_producers_and_a_consumer_aligned_by_start_of_hour(tmp_path):
    dg = pytest.importorskip("dagster")
    a, b, c = ("file://in/a.csv", "file://in/b.csv", "file://in/c.csv")
    dags = [_cron_partition_dag("pa", "ta", "0 * * * *", a), _cron_partition_dag("pb", "tb", "15 * * * *", b), _cron_partition_dag("pc", "tc", "30 * * * *", c),
            _partitioned_consumer("cons", [a, b, c], "StartOfHourMapper")]
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": dags}))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_part" / "defs"),
                    "--report", str(tmp_path / "r.json"), "--partition-start", "2026-01-01"], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]
    assert {k: v["status"] for k, v in rep.items()} == {"pa": "converted", "pb": "converted", "pc": "converted", "cons": "converted"}, rep
    (tmp_path / "pkg_part" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    import datetime
    mods = {n: importlib.import_module(f"pkg_part.defs.{n}") for n in ("pa", "pb", "pc", "cons")}
    assets = [mods["pa"].t_ta, mods["pb"].t_tb, mods["pc"].t_tc, mods["cons"].t_combine]
    defs = dg.Definitions(assets=assets, jobs=[m.__dict__[f"{n}_job"] for n, m in mods.items()], schedules=[mods[n].__dict__[f"{n}_schedule"] for n in ("pa", "pb", "pc")])
    # a producer's tick runs the partition that contains the tick (hour 10 for 10:15)
    sched = defs.get_schedule_def("pb_schedule")
    ctx = dg.build_schedule_context(scheduled_execution_time=datetime.datetime(2026, 3, 10, 10, 15, tzinfo=datetime.timezone.utc), repository_def=defs.get_repository_def())
    assert sched.evaluate_tick(ctx).run_requests[0].partition_key == "2026-03-10-10:00"
    # the consumer's hour runs only after all three producers updated that hour
    inst = dg.DagsterInstance.ephemeral()
    key = dg.AssetKey(["file", "curated" if False else "out", "combined_csv"])
    cons = [a for a in assets if a.key == key][0]
    cur = None
    def tick():
        nonlocal cur
        r = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=cur); cur = r.cursor
        return r.get_num_requested(key)
    hour = "2026-03-10-10:00"
    assert tick() == 0
    dg.materialize([mods["pa"].t_ta], instance=inst, partition_key=hour); assert tick() == 0
    dg.materialize([mods["pb"].t_tb], instance=inst, partition_key=hour); assert tick() == 0
    dg.materialize([mods["pc"].t_tc], instance=inst, partition_key=hour); assert tick() == 1
    res = dg.materialize([cons, mods["pa"].t_ta, mods["pb"].t_tb, mods["pc"].t_tc], instance=inst, partition_key=hour, selection=[key])
    assert res.success


def test_dag_run_partition_key_is_the_dagster_partition_and_a_weekly_cron_starts_on_its_weekday(tmp_path):
    dg = pytest.importorskip("dagster")
    d = _cron_partition_dag("d1", "t", "0 0 * * 1", "file://m/w.bin")
    d["tasks"][0]["callable"] = fn("uses_partition")
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [d]}, "pkg_wk", "--return-all", "--partition-start", "2026-01-05")
    assert rep["partitions"]["grain"] == "weekly" and rep["partitions"]["offset"] == 1
    pdef = mod.d1_partitions
    assert pdef.get_partition_keys(current_time=__import__("datetime").datetime(2026, 1, 20))[:3] == ["2026-01-05", "2026-01-12", "2026-01-19"]      # Mondays
    res = dg.materialize([mod.t_t], partition_key="2026-01-12")
    assert res.asset_value(dg.AssetKey(["file", "m", "w_bin"])) == ("2026-01-12", "2026-01-12T00:00:00+00:00")


def _rollup_consumer(dag_id, asset, window, wait, **mapper_kw):
    d = _partitioned_consumer(dag_id, [asset], "RollupMapper", f"file://out/{dag_id}.csv")
    d["timetable"]["asset_logic"] = "single"
    d["timetable"]["partition"]["__var"]["default_partition_mapper"] = {
        "__type": "airflow.partition_mappers.base.RollupMapper",
        "__var": {"upstream_mapper": {"__type": "airflow.partition_mappers.temporal.StartOfDayMapper", "__var": {}},
                  "window": {"__type": f"airflow.partition_mappers.window.{window}", "__var": {"direction": "forward"}},
                  "wait_policy": {"__type": f"airflow.partition_mappers.wait_policy.{wait[0]}", "__var": wait[1]}}}
    return d


def _fan_out_consumer(dag_id, asset, direction):
    d = _partitioned_consumer(dag_id, [asset], "FanOutMapper", f"file://out/{dag_id}.csv")
    d["timetable"]["asset_logic"] = "single"
    d["timetable"]["partition"]["__var"]["default_partition_mapper"] = {
        "__type": "airflow.partition_mappers.temporal.FanOutMapper",
        "__var": {"upstream_mapper": {"__type": "airflow.partition_mappers.temporal.StartOfWeekMapper", "__var": {}},
                  "window": {"__type": "airflow.partition_mappers.window.WeekWindow", "__var": {"direction": direction}}, "max_downstream_keys": 7}}
    return d


def test_rollup_waits_for_the_whole_window_and_fan_out_requests_every_day(tmp_path):
    dg = pytest.importorskip("dagster")
    pytest.importorskip("dagster_community_components")
    hourly, weekly = "file://in/h.csv", "file://in/w.bin"
    dags = [_cron_partition_dag("ph", "th", "0 * * * *", hourly), _cron_partition_dag("pw", "tw", "0 0 * * 1", weekly),
            _rollup_consumer("daily_roll", hourly, "DayWindow", ("WaitForAll", {})), _fan_out_consumer("fan", weekly, "forward")]
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": dags}))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_win" / "defs"),
                    "--report", str(tmp_path / "r.json"), "--partition-start", "2026-01-05"], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]
    assert {k: v["status"] for k, v in rep.items()} == {"ph": "converted", "pw": "converted", "daily_roll": "converted", "fan": "converted"}, rep
    (tmp_path / "pkg_win" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    m = {n: importlib.import_module(f"pkg_win.defs.{n}") for n in ("ph", "pw", "daily_roll", "fan")}
    assets = [m["ph"].t_th, m["pw"].t_tw, m["daily_roll"].t_combine, m["fan"].t_combine]
    defs = dg.Definitions(assets=assets, jobs=[m[n].__dict__[f"{n}_job"] for n in m], sensors=[m["daily_roll"].daily_roll_window_sensor, m["fan"].fan_window_sensor])
    repo = defs.get_repository_def()
    from dagster._core.definitions.partitions.context import partition_loading_context
    (tmp_path / "inst").mkdir()
    with dg.DagsterInstance.local_temp(str(tmp_path / "inst")) as inst:
        def evaluate(sensor, cursor=None):
            with partition_loading_context(dynamic_partitions_store=inst):
                return sensor.evaluate_tick(dg.build_sensor_context(instance=inst, repository_def=repo, cursor=cursor))
        roll = m["daily_roll"].daily_roll_window_sensor
        for h in range(23):                                              # 23 of 24 hours: the daily run must wait
            dg.materialize([m["ph"].t_th], instance=inst, partition_key=f"2026-01-06-{h:02d}:00")
        assert evaluate(roll).run_requests == []
        dg.materialize([m["ph"].t_th], instance=inst, partition_key="2026-01-06-23:00")
        keys = [r.partition_key for r in evaluate(roll).run_requests]
        assert keys == ["2026-01-06"]                                    # the 24th hour completes the day: one run, for the day
        dg.materialize([m["pw"].t_tw], instance=inst, partition_key="2026-01-05")                  # a Monday
        keys = sorted(r.partition_key for r in evaluate(m["fan"].fan_window_sensor).run_requests)
        assert keys == [f"2026-01-{d:02d}" for d in range(5, 12)]       # the seven days of that week


def test_airflow_2_macros_next_prev_tomorrow_yesterday_follow_the_schedule():
    pytest.importorskip("croniter")
    import datetime as dt
    rt = _runtime()
    # a daily data-interval run for 2026-07-02 (interval 07-02 to 07-03): the schedule tags carry the previous and next logical dates
    tags = {"airflow/logical_date": "2026-07-02T00:00:00+00:00", "airflow/data_interval_end": "2026-07-03T00:00:00+00:00",
            "airflow/prev_logical_date": "2026-07-01T00:00:00+00:00", "airflow/next_logical_date": "2026-07-03T00:00:00+00:00"}
    ctx = rt["af_context"](_fake_ctx(tags), "d", "t", {}, {})
    assert (ctx["ds"], ctx["next_ds"], ctx["prev_ds"]) == ("2026-07-02", "2026-07-03", "2026-07-01")
    assert (ctx["tomorrow_ds"], ctx["yesterday_ds"], ctx["next_ds_nodash"], ctx["prev_ds_nodash"]) == ("2026-07-03", "2026-07-01", "20260703", "20260701")
    assert ctx["ts_nodash_with_tz"] == "20260702T000000+0000" and ctx["prev_data_interval_start_success"] is None


@pytest.mark.parametrize("seconds,start,cron", [
    (21600, "2024-01-01T00:00:00", "0 */6 * * *"), (3600, "2024-01-01T00:00:00", "0 * * * *"), (900, "2024-01-01T00:00:00", "*/15 * * * *"),
    (86400, "2024-01-01T09:30:00", "30 9 * * *"), (604800, "2024-01-01T00:00:00", "0 0 * * 1"),          # 2024-01-01 is a Monday
    (7200, "2024-01-01T01:00:00", None), (5400, "2024-01-01T00:00:00", None), (172800, "2024-01-01T00:00:00", None)])
def test_a_timedelta_schedule_becomes_a_cron_only_when_it_is_exact(seconds, start, cron):
    assert C.delta_to_cron(seconds, start)[0] == cron


def test_a_delta_schedule_converts_and_catchup_true_defers_unless_ignored(monkeypatch):
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})], kind="delta")
    d["timetable"] = {"class": "DeltaDataIntervalTimetable", "kind": "delta", "timezone": "UTC", "interval_seconds": 21600.0}
    d["start_date"] = "2024-01-01T00:00:00+00:00"
    code, s = convert(d)
    assert "cron_schedule='0 */6 * * *'" in code and "every 21600 s" in " ".join(s["notes"])
    d["catchup"] = True
    with pytest.raises(C.Defer, match="catchup=True"):
        convert(d)
    monkeypatch.setattr(C, "CATCHUP", "ignore")
    _, s = convert(d)
    assert any("catchup=True" in n and "from now on" in n for n in s["notes"])


def test_a_subdag_is_not_converted_on_its_own():
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "true"})])
    d["is_subdag"], d["parent_dag_id"] = True, "parent"
    with pytest.raises(C.Defer, match="SubDAG of parent"):
        convert(d)


def test_the_run_is_decided_by_its_leaf_tasks_like_airflow(tmp_path):
    # `a` fails but both of its downstream tasks (a cleanup and an alert) succeed: they are the leaves, so the run succeeds, as in Airflow
    t = [_py("a", "boom"), _py("cleanup", "step_join", up=["a"], trigger_rule="all_done"), _py("alert", "step_b", up=["a"], trigger_rule="one_failed")]
    mod, res = _run_failing(tmp_path, t, "pkg_leaf")
    assert res.success and sorted(mod.TRACE) == ["b", "join"]


def test_datetimes_print_with_a_t_before_airflow_2_8_only():
    rt = _runtime()
    ctx = rt["af_context"](_fake_ctx({"airflow/logical_date": "2026-07-02T05:00:00+00:00"}), "d", "t", {}, {})
    assert str(ctx["execution_date"]) == "2026-07-02 05:00:00+00:00"                       # 2.8 and 3.x: the datetime's own text
    rt["AF_ISO_DT"] = True
    ctx = rt["af_context"](_fake_ctx({"airflow/logical_date": "2026-07-02T05:00:00+00:00"}), "d", "t", {}, {})
    assert str(ctx["execution_date"]) == "2026-07-02T05:00:00+00:00" and ctx["ds"] == "2026-07-02"


def _ext_sensor_dags(sensor_args):
    parent = {**dag([_py("p", "step_a")], kind="cron_trigger"), "dag_id": "parent"}
    sensor = task("wait", "ExternalTaskSensor", args={"external_dag_id": "parent", "external_task_ids": ["p"], "execution_delta": None,
                                                      "execution_date_fn": None, "allowed_states": ["success"], "failed_states": None,
                                                      "check_existence": False, "external_task_group_id": None, **sensor_args})
    child = {**dag([sensor, _py("after", "step_b", up=["wait"])], kind="cron_trigger"), "dag_id": "child"}
    return parent, child


def test_an_external_task_sensor_becomes_a_dependency_on_the_awaited_tasks_asset(tmp_path):
    dg = pytest.importorskip("dagster")
    parent, child = _ext_sensor_dags({})
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": [parent, child]}))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg_ext" / "defs"),
                    "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]
    assert rep["child"]["status"] == "converted" and rep["child"]["schedule"] is None, rep["child"]
    assert any("ExternalTaskSensor became a dependency" in n for n in rep["child"]["notes"])
    (tmp_path / "pkg_ext" / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    import importlib
    mp, mc = importlib.import_module("pkg_ext.defs.parent"), importlib.import_module("pkg_ext.defs.child")
    defs = dg.Definitions(assets=[mp.t_p, mc.t_after])
    inst = dg.DagsterInstance.ephemeral()
    key = dg.AssetKey(["child", "after"])
    cur = None
    def tick():
        nonlocal cur
        r = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=cur); cur = r.cursor
        return r.get_num_requested(key)
    assert tick() == 0
    dg.materialize([mp.t_p], instance=inst)
    assert tick() == 1                                         # the awaited task finished: the dependent step is requested


@pytest.mark.parametrize("args,why", [({"execution_delta": {"seconds": 3600.0}}, "execution delta"), ({"allowed_states": ["success", "skipped"]}, "custom states"),
                                      ({"external_dag_id": "nowhere"}, "not in this conversion")])
def test_an_external_task_sensor_with_a_different_wait_is_deferred_with_the_reason(args, why):
    parent, child = _ext_sensor_dags(args)
    C.build_task_catalog({"dags": [parent, child]})
    C.ALL_DAG_IDS = {"parent", "child"}
    C.TASK_TYPES = {("parent", "p"): "PythonOperator"}
    with pytest.raises(C.Defer, match=why):
        convert(child)


def cls_info(name):
    import ast
    import textwrap
    src = Path(MOD).read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == name)
    return {"name": name, "module": "mod", "file": MOD, "source": textwrap.dedent(ast.get_source_segment(src, node, padded=False)), "template_fields": []}


def test_a_user_defined_operator_runs_its_execute_with_its_arguments_and_templates(tmp_path):
    dg = pytest.importorskip("dagster")
    g = task("g", "Greeter", operator_class=cls_info("Greeter"), args={"name": "x-{{ ds }}", "times": 2})
    after = task("after", "PythonOperator", up=["g"], callable=fn("passthrough"), op_args=[{"xcom_from": "g", "key": "return_value"}], op_kwargs={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([g, after], kind="once")]}, "pkg_custom", "--return-all")
    assert rep["status"] == "converted", rep
    res = dg.materialize([mod.t_g, mod.t_after])
    value = res.asset_value(dg.AssetKey(["d1", "g"]))
    assert res.success and value.startswith("x-20") and value.endswith(":2:" + value.split(":")[2] + ":3") and "-" in value.split(":")[0]
    assert "airflow" not in (tmp_path / "pkg_custom" / "defs" / "d1.py").read_text().replace("airflow_dag", "").replace("airflow_task", "").replace("airflow_operator", "")


def test_an_operator_class_that_uses_the_airflow_api_is_deferred():
    t = task("u", "UsesAirflow", operator_class=cls_info("UsesAirflow"), args={})
    with pytest.raises(C.Defer, match="Airflow API"):
        convert(dag([t]))


def test_asset_metadata_from_yield_and_outlet_events_and_reading_inlet_events(tmp_path):
    dg = pytest.importorskip("dagster")
    uri = "s3://output/1.txt"
    y = task("y", "PythonOperator", callable=fn("yields_meta"), op_args=[], op_kwargs={}, outlets=[uri])
    e = task("e", "PythonOperator", up=["y"], callable=fn("sets_extra"), op_args=[], op_kwargs={})            # writes the event of ASSET, not its own
    r = task("r", "PythonOperator", up=["e"], callable=fn("reads_events"), op_args=[], op_kwargs={}, inlets=[uri])
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([y, e, r], kind="once")]}, "pkg_events", "--return-all")
    assert rep["status"] == "converted", rep
    key = dg.AssetKey(["s3", "output", "_1_txt"])
    (tmp_path / "inst").mkdir()
    with dg.DagsterInstance.local_temp(str(tmp_path / "inst")) as inst:
        res = dg.materialize([mod.t_y, mod.t_e, mod.t_r], instance=inst)
        assert res.success
        events = [m.materialization.metadata for m in res.get_asset_materialization_events() if m.asset_key == key]
        assert events[0]["hi"].value == "bye" and events[0]["n"].value == 3                 # `yield Metadata(asset, extra)` became materialization metadata
        assert res.asset_value(key) == "done"                                              # and the generator's return value is the asset's value
        assert any("flag" in m and m["flag"].value is True for m in events)                # outlet_events[asset].extra = {...} on another task
        assert res.asset_value(dg.AssetKey(["d1", "r"])) == [("bye", None), (None, True)]  # inlet_events[asset] lists the materializations, oldest first, with their metadata


def test_s3_operators_run_inside_a_graph_dag(tmp_path):
    dg = pytest.importorskip("dagster")
    f = lambda **k: {"aws_conn_id": "aws_default", **k}
    ls = task("ls", "S3ListOperator", template_fields=f(bucket="b", prefix="p/", delimiter=None))
    use = task("use", "PythonOperator", up=["ls"], callable=fn("passthrough"), op_args=[{"xcom_from": "ls", "key": "return_value"}], op_kwargs={}, trigger_rule="all_done")
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "x", "dags": [dag([ls, use], kind="once")]}, "pkg_s3graph", "--return-all")
    assert rep["status"] == "converted" and rep["resources"] == ["aws_default"], rep

    class Client:                                                        # what dagster-aws attaches: the boto client's methods
        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda self, **kw: [{"Contents": [{"Key": "p/a"}, {"Key": "p/b"}]}]})()
    res = dg.materialize([mod.d1_graph], resources={"aws_default": dg.ResourceDefinition.hardcoded_resource(Client())})
    assert res.success and res.asset_value(dg.AssetKey(["d1", "use"])) == ["p/a", "p/b"]


def test_dagrun_timeout_becomes_the_run_time_limit_tag_and_latest_only_is_dropped():
    d = dag([task("lo", "LatestOnlyOperator"), task("t", "BashOperator", up=["lo"], template_fields={"bash_command": "true"})])
    d["dag_settings"] = {"dagrun_timeout": {"seconds": 3600.0}}
    code, s = convert(d)
    assert "'dagster/max_runtime': '3600'" in code and s["run_timeout_s"] == 3600
    assert any("LatestOnlyOperator is dropped" in n for n in s["notes"]) and "t_lo" not in code
