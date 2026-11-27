"""Converter fixes found by triaging the stage-1 behaviour comparison (reference/converter-triage.md). One executed test per root cause, plus an ordering
invariant: every Airflow edge between tasks that have assets must be an asset dependency (direct or transitive) in the emitted definitions."""
import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import convert_ir as C  # noqa: E402
from test_convert_ir import dag, fn, task  # noqa: E402
from test_converter_patterns import convert_and_load, states  # noqa: E402

SCRATCH = Path.home() / "airflow-eval-scratch"


def _meta_states(res):
    return {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}


def _convert_cli(tmp_path, ir, pkg):
    (tmp_path / f"{pkg}.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / f"{pkg}.json"), "--out-dir", str(tmp_path / pkg / "defs"),
                    "--report", str(tmp_path / f"{pkg}-r.json"), "--return-all"], check=True, capture_output=True)
    (tmp_path / pkg / "__init__.py").write_text("")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return json.loads((tmp_path / f"{pkg}-r.json").read_text())


def _asset_graph_of(mod):
    """asset key -> (airflow task id, set of direct dependency keys) from every AssetsDefinition in a converted module."""
    out = {}
    for v in vars(mod).values():
        if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition):
            for k in v.keys:
                out[k] = ((v.specs_by_key[k].metadata or {}).get("airflow_task"), set(v.asset_deps.get(k, set())))
    return out


def _reach(deps):
    memo = {}

    def go(n, seen=()):
        if n in memo:
            return memo[n]
        r = set()
        for u in deps.get(n, ()):
            if u not in seen:
                r.add(u)
                r |= go(u, seen + (n,))
        memo[n] = r
        return r
    return {n: go(n) for n in deps}


def missing_edges(dag_ir, mod):
    """IR task pairs (u before t, both with assets) where t's asset does not reach u's asset."""
    ag = _asset_graph_of(mod)
    key_of = {tid: k for k, (tid, _) in ag.items() if tid}
    tasks = {t["task_id"]: t for t in dag_ir["tasks"]}

    def ancestors(tid, seen=None):
        seen = set() if seen is None else seen
        for u in tasks[tid]["upstream"]:
            if u not in seen:
                seen.add(u)
                ancestors(u, seen)
        return seen
    reach = _reach({k: d for k, (_, d) in ag.items()})
    miss = []
    for tid, k in key_of.items():
        if tid not in tasks:                                 # a synthetic task (a converted task-group loop is one step named after its group)
            continue
        for u in ancestors(tid):
            if u in key_of and key_of[u] != k and key_of[u] not in reach.get(k, set()):
                miss.append((tid, u))
    return miss


# ---- 1. the graph form declares every edge as an asset dependency -------------------------------------------------------------------------------

def test_graph_form_declares_task_group_branch_and_mapped_edges(tmp_path):
    start = task("start", "EmptyOperator")
    a = task("tg.a", "PythonOperator", up=["start"], group="tg", callable=fn("numbers"), op_args=[], op_kwargs={})
    b = task("tg.b", "PythonOperator", up=["tg.a"], group="tg", callable=fn("numbers"), op_args=[], op_kwargs={})
    br = task("br", "_BranchPythonDecoratedOperator", up=["tg.b"], callable=fn("choose_a"), op_args=[], op_kwargs={})
    left = task("left", "PythonOperator", up=["br"], callable=fn("numbers"), op_args=[], op_kwargs={})
    right = task("right", "PythonOperator", up=["br"], callable=fn("numbers"), op_args=[], op_kwargs={})
    m = task("m", "PythonOperator", up=["left"], is_mapped=True, callable={**fn("double_item"), "params": ["x"]}, op_args=[], op_kwargs={},
             expand={"form": "expand", "args": {"x": {"kind": "xcom", "wrapper": "PlainXComArg", "task_id": "left", "key": "return_value"}}, "fixed": {},
                     "fixed_unserializable": [], "op_args": False})
    join = task("join", "EmptyOperator", up=["m", "right"], trigger_rule="none_failed_min_one_success")
    d = dag([start, a, b, br, left, right, m, join], kind="once")
    rep = _convert_cli(tmp_path, {"airflow_version": "x", "dags": [d]}, "pkg_edges")
    assert rep["dags"]["d1"]["status"] == "converted"
    mod = importlib.import_module("pkg_edges.defs.d1")
    assert missing_edges(d, mod) == []
    ag = _asset_graph_of(mod)
    assert any("tg/b" in k.to_user_string() and any("tg/a" in u.to_user_string() for u in deps) for k, (_, deps) in ag.items())      # a task-group edge, directly


def test_every_converted_stage1_dag_keeps_every_edge_as_an_asset_dependency(tmp_path):
    root = SCRATCH / "conv_v3" / "stage1-private" / "behavior"
    if not root.is_dir() or not (SCRATCH / "irs").is_dir():
        pytest.skip("the stage-1 scratch (irs and per-DAG rows) is not present")
    rows = [json.loads((p / "behavior.json").read_text()) for p in sorted(root.iterdir()) if (p / "behavior.json").exists()]
    checked, bad = 0, {}
    for n, row in enumerate(rows):
        irf = SCRATCH / "irs" / f"{row['repo']}.{str(row['airflow']).split('.')[0]}.json"
        if not irf.exists():
            continue
        ir = json.loads(irf.read_text())
        mine = [x for x in ir["dags"] if x["dag_id"] == row["dag_id"]]
        if not mine:
            continue
        pkg = f"pkg_s1_{n}"
        rep = _convert_cli(tmp_path, {**ir, "dags": mine}, pkg)
        r = rep["dags"].get(row["dag_id"], {})
        if r.get("status") != "converted":
            continue
        try:
            mod = importlib.import_module(f"{pkg}.defs.{C.safe(row['dag_id'])}")
        except ImportError:                                  # the DAG imports code from its repository (an `include` package): not loadable here
            continue
        miss = missing_edges(mine[0], mod)
        checked += 1
        if miss:
            bad[row["dag_id"]] = miss[:3]
    assert checked >= 15
    assert not bad, bad


# ---- 2. bash exit code 99 skips instead of failing -----------------------------------------------------------------------------------------------

def test_bash_exit_99_skips_the_task_and_its_downstream_and_the_run_succeeds(tmp_path):
    s = task("s", "BashOperator", template_fields={"bash_command": "echo hello; exit 99"}, args={"skip_on_exit_code": [99]})
    after = task("after", "BashOperator", up=["s"], template_fields={"bash_command": "echo after"}, args={"skip_on_exit_code": [99]})
    ok = task("ok", "BashOperator", template_fields={"bash_command": "echo fine"}, args={"skip_on_exit_code": [99]})
    rep, mod, _ = convert_and_load(tmp_path, [s, after, ok], "cp_exit99")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    assert res.success
    meta = _meta_states(res)
    assert meta.get("d1/s") == "skipped" and meta.get("d1/after") == "skipped" and meta.get("d1/ok") != "skipped"


def test_bash_exit_99_in_a_task_bash_decorator_and_a_custom_skip_code(tmp_path):
    src = "@task.bash\ndef this_will_skip() -> str:\n    return 'echo hello world; exit 99;'\n"
    (tmp_path / "skipsrc.py").write_text(src)
    s = task("s", "_BashDecoratedOperator", callable={"name": "this_will_skip", "module": "m", "file": str(tmp_path / "skipsrc.py"), "params": [], "source": src}, op_args=[], op_kwargs={},
             template_fields={}, args={"skip_on_exit_code": [99]})
    c = task("c", "BashOperator", template_fields={"bash_command": "exit 7"}, args={"skip_on_exit_code": [7]})
    rep, mod, _ = convert_and_load(tmp_path, [s, c], "cp_exit99b")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    assert res.success and _meta_states(res).get("d1/s") == "skipped" and _meta_states(res).get("d1/c") == "skipped"
    assert "skip_on_exit_code=[7]" in (tmp_path / "cp_exit99b" / "defs" / "d1.py").read_text()


def test_a_non_skip_exit_code_still_fails_the_task(tmp_path):
    f = task("f", "BashOperator", template_fields={"bash_command": "exit 3"}, args={"skip_on_exit_code": [99]})
    rep, mod, _ = convert_and_load(tmp_path, [f], "cp_exit3")
    res = dg.materialize([mod.t_f], raise_on_error=False)
    assert not res.success


# ---- 3. macros, variables and task params the runtime was missing -------------------------------------------------------------------------------

def test_render_provides_json_yaml_time_uuid_macros_and_var(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    rt = importlib.util.module_from_spec(importlib.util.spec_from_loader("rt_m", loader=None))
    exec(C.RUNTIME, rt.__dict__)
    ctx = {"ds": "2026-07-02", "my_json": '{"a": [1, 2]}', "my_yaml": "a: 1\nb: [x, y]"}
    assert rt.render(ctx, "{{ macros.json.loads(my_json)['a'][1] }} {{ macros.yaml.safe_load(my_yaml)['b'][0] }}") == "2 x"
    assert len(rt.render(ctx, "{{ macros.uuid.uuid4().hex }}")) == 32
    monkeypatch.setenv("AIRFLOW_VAR_GREETING", "hi")
    monkeypatch.setenv("AIRFLOW_VAR_CFG", '{"k": 3}')
    assert rt.render(ctx, "{{ var.value.greeting }} {{ var.json.cfg.k }}") == "hi 3"
    with pytest.raises(KeyError):
        rt.render(ctx, "{{ var.value.missing_one }}")


def test_converter_accepts_these_macros_and_var_instead_of_stubbing(tmp_path):
    t = task("t", "BashOperator", template_fields={"bash_command": "echo {{ macros.json.dumps({'a': 1}) }} {{ var.value.x }} {{ macros.ds_format_locale(ds, '%Y-%m-%d', 'd MMM', 'de') }}"})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_macros")
    assert not rep.get("caveats")


def test_task_level_params_reach_the_template_and_the_callable_executed(tmp_path):
    t = task("t", "BashOperator", template_fields={"bash_command": "echo {{ params.my_param }}"}, params={"my_param": "Parameter I passed in"})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_tparams", "once", "--return-all")
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert res.success
    assert res.output_for_node("d1__t") == "Parameter I passed in"


# ---- 4. a stub fails once, loudly, without retries ----------------------------------------------------------------------------------------------

def test_a_caveat_stub_is_not_retried(tmp_path):
    (tmp_path / "uses.py").write_text("def uses_ti():\n    return airflow.nothing()\n")
    t = task("t", "PythonOperator", retries=3, retry_delay_s=300, callable={"name": "uses_ti", "module": "m", "file": str(tmp_path / "uses.py"), "params": [], "source": "def uses_ti():\n    return airflow.nothing()\n"},
             op_args=[], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_stub", "once")
    assert any(c["kind"] == "stub" for c in rep["caveats"])
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert not res.success
    assert not any(e.event_type_value == "STEP_UP_FOR_RETRY" for e in res.all_events)


# ---- 5. xcom_push(return_value) inside a sensor callable ----------------------------------------------------------------------------------------

def test_xcom_push_return_value_is_the_sensor_value():
    rt = importlib.util.module_from_spec(importlib.util.spec_from_loader("rt_s", loader=None))
    exec(C.RUNTIME, rt.__dict__)

    class _Ctx:
        run = None
        run_id = "r"
        has_partition_key = False
        retry_number = 0

        def add_output_metadata(self, *_):
            pass
    ctx = rt.af_context(_Ctx(), "d", "t", {}, {})

    def check(**context):
        context["ti"].xcom_push(key="return_value", value={"ok": True})
        return True
    out = rt.call_with_context(check, [], {}, ctx)
    assert bool(out) is True and out.xcom_value == {"ok": True}


# ---- 6. mapped task groups are reported, not wired to a dangling argument ----------------------------------------------------------------------

def test_a_task_in_a_mapped_task_group_is_a_reported_stub(tmp_path):
    t = task("tg.t3", "PythonOperator", group="tg", mapped_group="tg", callable=fn("numbers"), op_args=[{"xcom_from": None, "key": None}], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_mgroup", "once")
    assert any("mapped task group" in c["reason"] for c in rep["caveats"])


# ---- 7. the resource an S3 op needs is provided by the converted project (defs/resources.py) ----------------------------------------------------------

def test_converted_s3_project_provides_the_resource_key_its_op_requires(tmp_path):
    t = task("up", "LocalFilesystemToS3Operator", template_fields={"filename": "include/x.csv", "dest_key": "k/{{ ds }}.csv", "dest_bucket": "b"},
             args={"filename": "include/x.csv", "dest_key": "k/{{ ds }}.csv", "dest_bucket": "b", "replace": False, "aws_conn_id": "aws_default"})
    rep, mod, pkg = convert_and_load(tmp_path, [t], "cp_s3res", "once")
    pytest.importorskip("dagster_aws")
    res_mod = importlib.import_module("cp_s3res.defs.resources")
    assets = [v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition)]
    d = dg.Definitions.merge(dg.Definitions(assets=assets), res_mod.resources() if callable(res_mod.resources) else res_mod.resources)
    d.validate_loadable(d)
    assert "aws_default" in (d.resources or {})


# ---- 8. the IR extractor (needs Airflow 2.10 in EVAL_AIRFLOW_PYTHON) ---------------------------------------------------------------------------------

def test_ir_records_mapped_classic_callable_task_params_and_mapped_groups(tmp_path):
    py = os.environ.get("EVAL_AIRFLOW_PYTHON") or str(SCRATCH / "venvs" / "af210" / "bin" / "python")
    if not Path(py).exists():
        pytest.skip("no Airflow interpreter")
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "d.py").write_text(textwrap.dedent('''
        from datetime import datetime
        from airflow.decorators import dag, task, task_group
        from airflow.operators.bash import BashOperator
        from airflow.operators.python import PythonOperator

        def plus(x):
            return x + 10

        @dag(dag_id="ir_probe", start_date=datetime(2024, 1, 1), schedule=None)
        def ir_probe():
            PythonOperator.partial(task_id="mapped_classic", python_callable=plus).expand(op_args=[[1], [2]])
            BashOperator(task_id="with_params", bash_command="echo {{ params.p }}", params={"p": "v"})

            @task_group
            def grp(n):
                @task
                def inner(n):
                    return n
                inner(n)
            grp.expand(n=[1, 2])
        ir_probe()
        '''))
    out = tmp_path / "ir.json"
    r = subprocess.run([py, "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(dags), "--out", str(out), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert out.exists(), r.stderr[-800:]
    d = next(x for x in json.loads(out.read_text())["dags"] if x["dag_id"] == "ir_probe")
    ts = {t["task_id"]: t for t in d["tasks"]}
    assert ts["mapped_classic"]["callable"]["name"] == "plus"
    assert ts["with_params"]["params"].get("p") == "v"
    assert ts["grp.inner"]["mapped_group"] == "grp"


# ---- 9. a @task.bash command is rendered as a template ------------------------------------------------------------------------------------------

def test_task_bash_command_template_is_rendered(tmp_path):
    src = "@task.bash\ndef k() -> str:\n    return 'echo \"key={{ task_instance_key_str }}\"'\n"
    (tmp_path / "ksrc.py").write_text(src)
    t = task("k", "_BashDecoratedOperator", callable={"name": "k", "module": "m", "file": str(tmp_path / "ksrc.py"), "params": [], "source": src},
             op_args=[], op_kwargs={}, template_fields={}, args={})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_bashrender", "once", "--return-all")
    res = dg.materialize([mod.t_k], raise_on_error=False)
    assert res.success and res.output_for_node("d1__k").startswith("key=d1__k__")


# ---- 10. @task.run_if / @task.skip_if skip at run time ------------------------------------------------------------------------------------------

def test_run_if_and_skip_if_conditions_skip_the_task_executed(tmp_path):
    def mk(tid, deco):
        src = f"@task.{deco}(lambda context: context[\"task_instance\"].task_id.endswith(\"_do_run\"))\n@task\ndef say():\n    return \"hello!\"\n"
        (tmp_path / f"{tid}.py").write_text(src)
        return task(tid, "_PythonDecoratedOperator", callable={"name": "say", "module": "m", "file": str(tmp_path / f"{tid}.py"), "params": [], "source": src}, op_args=[], op_kwargs={})
    ts = [mk("a_do_run", "run_if"), mk("b_skip_me", "run_if"), mk("c_do_run", "skip_if"), mk("d_other", "skip_if")]
    rep, mod, _ = convert_and_load(tmp_path, ts, "cp_runif", "once")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    assert res.success
    m = _meta_states(res)
    assert m.get("d1/b_skip_me") == "skipped" and m.get("d1/c_do_run") == "skipped"      # run_if false; skip_if true
    assert m.get("d1/a_do_run") != "skipped" and m.get("d1/d_other") != "skipped"


# ---- 11. DAG-level user_defined_macros and a named run_if / skip_if condition ------------------------------------------------------------------

def test_dag_user_defined_macros_reach_the_templates_executed(tmp_path):
    pytest.importorskip("yaml")
    d = dag([task("t", "BashOperator", template_fields={"bash_command": "echo {{ macros.json.loads(my_json)['a'] }}"})], kind="once")
    d["user_defined_macros"] = {"my_json": '{"a": "hello"}'}
    rep = _convert_cli(tmp_path, {"airflow_version": "x", "dags": [d]}, "pkg_umac")
    assert rep["dags"]["d1"]["status"] == "converted" and not rep["dags"]["d1"].get("caveats")
    mod = importlib.import_module("pkg_umac.defs.d1")
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert res.success and res.output_for_node("d1__t") == "hello"


def test_skip_if_with_a_named_condition_function_is_copied_and_applied_executed(tmp_path):
    src = "@task.skip_if(skip_decision)\n@task\ndef say():\n    return 'hello!'\n"
    f = tmp_path / "named.py"
    f.write_text("def skip_decision(context):\n    return context[\"task_instance\"].task_id.startswith(\"skipme\")\n\n\n" + src)
    ts = [task(tid, "_PythonDecoratedOperator", callable={"name": "say", "module": "m", "file": str(f), "params": [], "source": src}, op_args=[], op_kwargs={}) for tid in ("skipme_1", "keep_1")]
    rep, mod, _ = convert_and_load(tmp_path, ts, "cp_named_cond", "once")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    m = _meta_states(res)
    assert res.success and m.get("d1/skipme_1") == "skipped" and m.get("d1/keep_1") != "skipped"


def test_ds_format_locale_adds_babel_to_the_converted_requirements(tmp_path):
    t = task("t", "BashOperator", template_fields={"bash_command": "echo {{ macros.ds_format_locale(ds, '%Y-%m-%d', 'd MMM', 'de') }}"})
    rep, mod, pkg = convert_and_load(tmp_path, [t], "cp_babel", "once")
    assert "babel" in (pkg / "converted_requirements.txt").read_text()


def test_airflow_task_logger_name_is_not_an_airflow_dependency(tmp_path):
    f = tmp_path / "lg.py"
    src = "def work():\n    t_log.info('hi')\n    return 1\n"
    f.write_text("import logging\n\nt_log = logging.getLogger(\"airflow.task\")\n\n\n" + src)
    t = task("t", "PythonOperator", callable={"name": "work", "module": "m", "file": str(f), "params": [], "source": src}, op_args=[], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [t], "cp_tlog", "once", "--return-all")
    assert not rep.get("caveats")
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert res.success and res.output_for_node("d1__t") == 1


def test_a_plain_helper_defined_inside_the_dag_function_is_copied_executed(tmp_path):
    f = tmp_path / "nested.py"
    src = "@task.skip_if(skip_decision)\n@task\ndef say():\n    return 'hello!'\n"
    f.write_text("def my_dag():\n    def skip_decision(context):\n        return context[\"task_instance\"].task_id.endswith(\"_skip_me\")\n\n" +
                 "".join("    " + ln + "\n" for ln in src.splitlines()))
    ts = [task(tid, "_PythonDecoratedOperator", callable={"name": "say", "module": "m", "file": str(f), "params": [], "source": src}, op_args=[], op_kwargs=[]) for tid in ("a_skip_me", "b_keep")]
    ts = [{**t, "op_kwargs": {}} for t in ts]
    rep, mod, _ = convert_and_load(tmp_path, ts, "cp_nested", "once")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    m = _meta_states(res)
    assert res.success and m.get("d1/a_skip_me") == "skipped" and m.get("d1/b_keep") != "skipped"
