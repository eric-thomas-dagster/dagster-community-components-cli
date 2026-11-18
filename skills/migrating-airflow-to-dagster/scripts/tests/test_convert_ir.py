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
    (lambda d: d["tasks"][0].update(trigger_rule="all_done"), "trigger_rule all_done"),
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
    (lambda d: d.update(dag_settings={"dagrun_timeout": {"seconds": 60.0}}), "dagrun_timeout"),
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
    assert mod.TRACE == ["a", "a", "join"]                      # start, path_a, join; path_b and the all_success join were skipped


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
