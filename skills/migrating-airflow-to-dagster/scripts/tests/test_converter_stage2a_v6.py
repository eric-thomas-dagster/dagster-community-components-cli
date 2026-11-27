"""Converter pass 3 (reference/converter-triage.md, "Stage 2a, pass 3"): mapped task groups as the native form, helpers that close over DAG-function locals,
ObjectStoragePath arguments, user-defined mapped operators / non-plain constructor arguments, and Connection / hook calls through an env shim.
Every test converts a small IR and RUNS the emitted Dagster code."""
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
from test_convert_ir import dag as _dag, task  # noqa: E402

SCRATCH = Path(os.environ.get("EVAL_SCRATCH", Path.home() / "airflow-eval-scratch"))


def finfo(tmp_path, src, name):
    """A callable's IR record, from source text written to a file (the converter copies source)."""
    src = textwrap.dedent(src)
    f = tmp_path / f"{name}_src.py"
    f.write_text(src)
    import ast
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return {"name": name, "file": str(f), "module": f"{name}_src", "params": [a.arg for a in node.args.args], "source": src}


def build(tmp_path, tasks, pkg, mapped_groups=None, kind="once", caveats=True, extra_dag=None):
    d = _dag(tasks, kind=kind)
    d["mapped_groups"] = mapped_groups or {}
    d.update(extra_dag or {})
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": [d]}))
    cmd = [sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"), "--report", str(tmp_path / "r.json"), "--return-all"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["d1"]
    (tmp_path / pkg / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    return rep, tmp_path / pkg


def load(pkg_dir):
    return importlib.import_module(f"{pkg_dir.name}.defs.d1")


def graph_assets(mod):
    return [v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition)]


def values(res, mod, **_):
    """asset key string -> the value the graph asset's output returned"""
    out = {}
    ad = graph_assets(mod)[0]
    for name, key in ad.keys_by_output_name.items():
        out[key.to_user_string()] = res.output_for_node(ad.node_def.name, name)
    return out


def states(res):
    return {e.step_key: e.event_type_value for e in res.all_events if e.event_type_value in ("STEP_SUCCESS", "STEP_SKIPPED", "STEP_FAILURE")}


def xin(tid):
    return {"xcom_from": tid, "key": "return_value"}


def lit(vs):
    return {"kind": "literal", "value": vs}


def xsrc(tid):
    return {"kind": "xcom", "wrapper": "PlainXComArg", "task_id": tid, "key": "return_value"}


def group_dag(tmp_path, group_args, form="expand", pkg="g", empty=False):
    """grp(n, label) { inner(n, label) -> second(inner) } mapped over `group_args`; `numbers` upstream, `after` collects the group's last task."""
    f_numbers = finfo(tmp_path, "def numbers():\n    return [1, 2, 3]\n", "numbers")
    f_inner = finfo(tmp_path, "def inner(n, label):\n    return f'{label}{n}'\n", "inner")
    f_second = finfo(tmp_path, "def second(x):\n    return x + '!'\n", "second")
    f_after = finfo(tmp_path, "def after(vals):\n    return list(vals)\n", "after")
    t = [task("numbers", "PythonOperator", callable=f_numbers, op_args=[], op_kwargs={}),
         task("grp.inner", "PythonOperator", up=["numbers"], callable=f_inner, op_args=[{"group_arg": "n"}, "L"], op_kwargs={}, mapped_group="grp", group="grp"),
         task("grp.second", "PythonOperator", up=["grp.inner"], callable=f_second, op_args=[xin("grp.inner")], op_kwargs={}, mapped_group="grp", group="grp"),
         task("after", "PythonOperator", up=["grp.second"], callable=f_after, op_args=[xin("grp.second")], op_kwargs={})]
    rep, pkg_dir = build(tmp_path, t, pkg, mapped_groups={"grp": {"form": form, "args": group_args}})
    return rep, load(pkg_dir)


# ---- 1. mapped task groups: the verified native form, not a stub -----------------------------------------------------------------------------------------

def test_mapped_group_over_a_tasks_output_runs_one_step_per_item_and_collects_each_members_values(tmp_path):
    rep, mod = group_dag(tmp_path, {"n": xsrc("numbers")}, pkg="g1")
    assert not rep["caveats"], rep["caveats"]
    text = (tmp_path / "g1" / "defs" / "d1.py").read_text()
    assert ".map(lambda item: group_grp(item" in text and "DynamicOut" in text and "AIRFLOW-CAVEAT" not in text
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert res.success
    v = values(res, mod)
    assert v["d1/grp/inner"] == ["L1", "L2", "L3"] and v["d1/grp/second"] == ["L1!", "L2!", "L3!"] and v["d1/after"] == ["L1!", "L2!", "L3!"]
    st = states(res)
    assert sum(1 for k, s in st.items() if "grp__body.probe" in k or "d1__grp_inner" in k) == 3         # one inner step per item
    assert len([k for k in st if "d1__grp_second" in k]) == 3


def test_mapped_group_over_a_literal_cross_product_and_expand_kwargs(tmp_path):
    rep, mod = group_dag(tmp_path, {"n": lit([1, 2]), "label": lit(["a", "b"])}, pkg="g2")
    # two arguments: the callable's `label` comes from the partial (the literal 'L'); the group also maps `label`, unused by `inner` here, so 4 items run
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert res.success
    assert len(values(res, mod)["d1/grp/inner"]) == 4
    tmp2 = tmp_path / "kw"
    tmp2.mkdir()
    rep, mod = group_dag(tmp2, {"*": lit([{"n": 7}, {"n": 8}])}, form="expand_kwargs", pkg="g3")
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert res.success and values(res, mod)["d1/grp/second"] == ["L7!", "L8!"]


def test_mapped_group_with_no_items_skips_its_steps_and_everything_after_and_the_run_succeeds(tmp_path):
    rep, mod = group_dag(tmp_path, {"n": lit([])}, pkg="g4")
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert res.success
    meta = {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}
    assert meta.get("d1/after") == "skipped"                                   # Airflow: an empty expand skips the group's tasks and what follows
    assert not any(s == "STEP_FAILURE" for s in states(res).values())


def test_a_failing_item_fails_the_run_after_the_other_items_ran_and_skips_what_is_below(tmp_path):
    f_inner = finfo(tmp_path, "def inner(n):\n    if n == 2:\n        raise RuntimeError('item 2 failed')\n    return n\n", "inner")
    f_after = finfo(tmp_path, "def after(v):\n    return v\n", "after")
    t = [task("grp.inner", "PythonOperator", callable=f_inner, op_args=[{"group_arg": "n"}], op_kwargs={}, mapped_group="grp", group="grp"),
         task("after", "PythonOperator", up=["grp.inner"], callable=f_after, op_args=[xin("grp.inner")], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "g5", mapped_groups={"grp": {"form": "expand", "args": {"n": lit([1, 2, 3])}}})
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    st = states(res)
    assert not res.success
    inner = {k: s for k, s in st.items() if "d1__grp_inner" in k}
    assert sorted(inner.values()) == ["STEP_FAILURE", "STEP_SUCCESS", "STEP_SUCCESS"]       # the other items still ran
    assert not any("d1__after" in k for k in st)                                           # the task after never ran


def test_group_members_may_read_outside_tasks_and_a_group_in_a_group_with_a_shell_member(tmp_path):
    f_base = finfo(tmp_path, "def base():\n    return 100\n", "base")
    f_add = finfo(tmp_path, "def add(n, base):\n    return n + base\n", "add")
    t = [task("base", "PythonOperator", callable=f_base, op_args=[], op_kwargs={}),
         task("g.add", "PythonOperator", up=["base"], callable=f_add, op_args=[{"group_arg": "n"}, xin("base")], op_kwargs={}, mapped_group="g", group="g"),
         task("g.say", "BashOperator", up=["g.add"], template_fields={"bash_command": "echo hi"}, mapped_group="g", group="g", args={})]
    rep, pkg = build(tmp_path, t, "g6", mapped_groups={"g": {"form": "expand", "args": {"n": lit([1, 2])}}})
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert res.success and values(res, mod)["d1/g/add"] == [101, 102]


def test_a_group_that_cannot_be_converted_is_a_loud_stub_that_names_why(tmp_path):
    f = finfo(tmp_path, "def inner(n):\n    return n\n", "inner")
    t = [task("grp.inner", "PythonOperator", callable=f, op_args=[{"group_arg": "n"}], op_kwargs={}, mapped_group="grp", group="grp")]
    rep, pkg = build(tmp_path, t, "g7", mapped_groups={"grp": {"form": "expand", "args": {"n": {"kind": "xcom", "wrapper": "MapXComArg", "inner": lit([1]), "callables": []}}}})
    assert any("mapped task group" in c["reason"] and "MapXComArg" in c["reason"] for c in rep["caveats"])


def test_ir_records_mapped_group_inputs_and_group_arguments(tmp_path):
    py = os.environ.get("EVAL_AIRFLOW_PYTHON") or str(SCRATCH / "venvs" / "af210" / "bin" / "python")
    if not Path(py).exists():
        pytest.skip("no Airflow interpreter")
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "d.py").write_text(textwrap.dedent('''
        from datetime import datetime
        from airflow.decorators import dag, task, task_group

        @dag(dag_id="ir_mg", start_date=datetime(2024, 1, 1), schedule=None)
        def ir_mg():
            @task
            def numbers():
                return [1, 2]

            @task_group
            def grp(n, label):
                @task
                def inner(n, label):
                    return f"{label}{n}"
                inner(n, label)
            grp.partial(label="L").expand(n=numbers())

            @task_group
            def kw(a):
                @task
                def t(a):
                    return a
                t(a)
            kw.expand_kwargs([{"a": 1}, {"a": 2}])
        ir_mg()
        '''))
    out = tmp_path / "ir.json"
    r = subprocess.run([py, "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(dags), "--out", str(out), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert out.exists(), r.stderr[-800:]
    d = next(x for x in json.loads(out.read_text())["dags"] if x["dag_id"] == "ir_mg")
    assert d["mapped_groups"]["grp"]["args"]["n"]["task_id"] == "numbers"
    assert d["mapped_groups"]["kw"]["form"] == "expand_kwargs" and d["mapped_groups"]["kw"]["args"]["*"]["value"] == [{"a": 1}, {"a": 2}]
    ts = {t["task_id"]: t for t in d["tasks"]}
    assert ts["grp.inner"]["op_args"][0] == {"group_arg": "n"} and ts["grp.inner"]["op_args"][1] == "L"
    # and the converter runs what the extractor wrote, end to end
    (tmp_path / "pkg").mkdir()
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(out), "--out-dir", str(tmp_path / "pkg" / "defs"), "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["ir_mg"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]


# ---- 2. helpers and callables that close over the DAG function's locals -------------------------------------------------------------------------------------

def _closure_dag_file(tmp_path):
    src = textwrap.dedent('''
        from airflow.decorators import dag, task

        @dag(dag_id="closure")
        def closure():
            SCALE = 3
            NAMES = ["a", "b"]
            PREFIX = f"p-{SCALE}"

            def helper(x):
                return PREFIX + str(x * SCALE) + NAMES[0]

            def second_helper(x):
                return helper(x) + "!"

            @task
            def t(x=2):
                return second_helper(x)

            @task
            def produce():
                return 1

            res = produce()

            def needs_a_task_output():
                return res

            @task
            def uses_it():
                return needs_a_task_output()
            t()
        closure()
        ''')
    f = tmp_path / "closure_dag.py"
    f.write_text(src)
    return f


def test_a_helper_closing_over_plain_dag_function_locals_is_copied_with_them_and_runs(tmp_path):
    f = _closure_dag_file(tmp_path)
    info = {"name": "t", "file": str(f), "module": "closure_dag", "params": ["x"], "source": "def t(x=2):\n    return second_helper(x)\n"}
    t = [task("t", "PythonOperator", callable=info, op_args=[], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "c1")
    assert not rep["caveats"], rep["caveats"]
    mod = load(pkg)
    ad = graph_assets(mod)[0]
    res = dg.materialize([ad])
    assert res.success
    text = (tmp_path / "c1" / "defs" / "d1.py").read_text()
    assert "SCALE = 3" in text and 'PREFIX = f"p-{SCALE}"' in text
    assert res.output_for_node(ad.node_def.name) == "p-36a!"


def test_a_local_that_holds_a_tasks_output_stays_a_loud_stub_naming_the_name(tmp_path):
    f = _closure_dag_file(tmp_path)
    info = {"name": "uses_it", "file": str(f), "module": "closure_dag", "params": [], "source": "def uses_it():\n    return needs_a_task_output()\n"}
    t = [task("uses_it", "PythonOperator", callable=info, op_args=[], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "c2")
    assert any("res" in c["reason"] and "unresolved" in c["reason"] for c in rep["caveats"]), rep["caveats"]


# ---- 3. ObjectStoragePath arguments ---------------------------------------------------------------------------------------------------------------------

def _asset_value(mod, res):
    ad = graph_assets(mod)[0]
    return res.output_for_node(ad.node_def.name)


def test_an_object_storage_path_argument_becomes_a_pathlib_path_and_the_callable_runs_unchanged(tmp_path):
    base = tmp_path / "bears"
    base.mkdir()
    f = finfo(tmp_path, "def get_bear(base):\n    p = base / 'bear.txt'\n    p.write_text('grr')\n    return str(p)\n", "get_bear")
    t = [task("get_bear", "PythonOperator", callable=f, op_args=[], op_kwargs={"base": {"object_storage_path": f"file://{base}"}})]
    rep, pkg = build(tmp_path, t, "o1")
    assert not rep["caveats"], rep["caveats"]
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]])
    assert res.success and _asset_value(mod, res) == str(base / "bear.txt") and (base / "bear.txt").read_text() == "grr"
    assert "universal-pathlib" in (tmp_path / "o1" / "converted_requirements.txt").read_text()


def test_object_storage_path_with_a_connection_netloc_fails_loudly_when_the_connection_is_missing_and_reads_it_when_set(tmp_path, monkeypatch):
    rt = _rt()
    monkeypatch.delenv("AIRFLOW_CONN_MY_S3", raising=False)
    with pytest.raises(rt["AirflowNotFoundException"], match="AIRFLOW_CONN_MY_S3"):
        rt["ObjectStoragePath"]("s3://my_s3@bucket/key")
    monkeypatch.setenv("AIRFLOW_CONN_MY_S3", "aws://AKIA:sec%2Fret@/?endpoint_url=http%3A%2F%2Flocalhost%3A9000")
    upath = pytest.importorskip("upath")
    p = rt["ObjectStoragePath"]("s3://my_s3@bucket/key")
    assert str(p).startswith("s3://bucket/key") and p.storage_options.get("key") == "AKIA" and p.storage_options.get("secret") == "sec/ret" and p.storage_options.get("endpoint_url") == "http://localhost:9000"


def test_object_storage_path_imported_inside_a_callable_is_the_shim(tmp_path):
    target = tmp_path / "out.txt"
    src = f"def w():\n    from airflow.sdk import ObjectStoragePath\n    p = ObjectStoragePath({'file://' + str(target)!r})\n    p.write_text('x')\n    return p.exists()\n"
    t = [task("w", "PythonOperator", callable=finfo(tmp_path, src, "w"), op_args=[], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "o2")
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]])
    assert res.success and _asset_value(mod, res) is True and target.read_text() == "x"


def _rt():
    ns: dict = {}
    exec(C.RUNTIME.replace("from __future__ import annotations", ""), ns)
    return ns


# ---- 4. user-defined mapped operators, and constructor arguments that are not plain -----------------------------------------------------------------------

OP_SRC = '''
from airflow.models.baseoperator import BaseOperator
from datetime import datetime, timedelta


class MyOp(BaseOperator):
    template_fields = ("url",)

    def __init__(self, url, n=1, when=None, wait=None, root=None, **kw):
        super().__init__(**kw)
        self.url, self.n, self.when, self.wait, self.root = url, n, when, wait, root

    def execute(self, context):
        return [self.url, self.n, self.when.year if self.when else None, self.wait.total_seconds() if self.wait else None, str(self.root) if self.root else None]
'''


def op_info(tmp_path):
    f = tmp_path / "ops.py"
    f.write_text(OP_SRC)
    return {"name": "MyOp", "module": "ops", "file": str(f), "source": textwrap.dedent(OP_SRC[OP_SRC.index("class MyOp"):]), "template_fields": ["url"]}


def test_a_user_defined_mapped_operator_runs_once_per_item_with_its_partial_arguments(tmp_path):
    t = [task("m", "MyOp", operator_class=op_info(tmp_path), is_mapped=True, args={"n": 5},
              expand={"form": "expand", "args": {"url": lit(["a", "b"])}, "fixed": {}, "fixed_unserializable": [], "partial": {}, "op_args": False}),
         task("after", "PythonOperator", up=["m"], callable=finfo(tmp_path, "def after(r):\n    return len(r)\n", "after"), op_args=[xin("m")], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "m1")
    assert not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]])
    v = values(res, mod)
    assert v["d1/m"] == [["a", 5, None, None, None], ["b", 5, None, None, None]] and v["d1/after"] == 2


def test_non_plain_constructor_arguments_datetime_timedelta_path_and_dag_param_are_built_in_code(tmp_path):
    root = tmp_path / "data"
    t = [task("o", "MyOp", operator_class=op_info(tmp_path),
              args={"url": "{{ params.u }}", "when": {"datetime": "2024-03-05T00:00:00+00:00"}, "wait": {"seconds": 90.0}, "root": {"object_storage_path": f"file://{root}"}})]
    rep, pkg = build(tmp_path, t, "m2", extra_dag={"params": {"u": "http://x"}})
    assert not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]])
    assert res.success and _asset_value(mod, res) == ["http://x", 1, 2024, 90.0, str(root)]


def test_a_constructor_argument_the_ir_could_only_describe_stays_a_loud_stub_naming_the_argument(tmp_path):
    t = [task("o", "MyOp", operator_class=op_info(tmp_path), args={"url": {"repr": "<SomeObject at 0x1>"}})]
    rep, pkg = build(tmp_path, t, "m3")
    assert any("'url'" in c["reason"] and "plain value" in c["reason"] for c in rep["caveats"]), rep["caveats"]


def test_ir_records_mapped_custom_operator_class_partial_arguments_dag_param_and_object_storage_path(tmp_path):
    py = os.environ.get("EVAL_AIRFLOW_PYTHON") or str(SCRATCH / "venvs" / "af210" / "bin" / "python")
    if not Path(py).exists():
        pytest.skip("no Airflow interpreter")
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "d.py").write_text(textwrap.dedent('''
        from datetime import datetime
        from airflow import DAG
        from airflow.models.baseoperator import BaseOperator
        from airflow.io.path import ObjectStoragePath

        class MyOp(BaseOperator):
            template_fields = ("url",)
            def __init__(self, url, n=1, root=None, **kw):
                super().__init__(**kw)
                self.url, self.n, self.root = url, n, root
            def execute(self, context):
                return self.url

        with DAG("ir_ops", start_date=datetime(2024, 1, 1), schedule=None, params={"u": "http://a"}) as d:
            MyOp.partial(task_id="m", n=5).expand(url=["a", "b"])
            MyOp(task_id="p", url=d.param("u"), root=ObjectStoragePath("file:///tmp/x/y"))
        '''))
    out = tmp_path / "ir.json"
    r = subprocess.run([py, "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(dags), "--out", str(out), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert out.exists(), r.stderr[-800:]
    ts = {t["task_id"]: t for t in next(x for x in json.loads(out.read_text())["dags"] if x["dag_id"] == "ir_ops")["tasks"]}
    assert ts["m"]["operator_class"]["name"] == "MyOp" and ts["m"]["is_mapped"] and ts["m"]["args"] == {"n": 5}
    assert ts["p"]["args"]["url"] == "{{ params.u }}" and ts["p"]["args"]["root"] == {"object_storage_path": "file:///tmp/x/y"}
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(out), "--out-dir", str(tmp_path / "pkg" / "defs"), "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["ir_ops"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]


# ---- 5. Connection / hook calls through the environment ---------------------------------------------------------------------------------------------------

def test_connection_get_and_basehook_read_airflow_conn_env_in_uri_and_json_form_and_are_loud_when_missing(tmp_path, monkeypatch):
    rt = _rt()
    monkeypatch.setenv("AIRFLOW_CONN_MY_DB", "postgres://user:p%40ss@db.example.com:5432/analytics?sslmode=require")
    c = rt["Connection"].get("my_db")
    assert (c.conn_type, c.host, c.login, c.password, c.port, c.schema) == ("postgres", "db.example.com", "user", "p@ss", 5432, "analytics")
    assert c.extra_dejson == {"sslmode": "require"} and "p@ss" not in repr(c)
    monkeypatch.setenv("AIRFLOW_CONN_API", json.dumps({"conn_type": "http", "host": "api.example.com", "password": "tok", "extra": {"region": "us"}}))
    h = rt["BaseHook"].get_connection("api")
    assert h.host == "api.example.com" and h.password == "tok" and h.extra_dejson == {"region": "us"}
    monkeypatch.delenv("AIRFLOW_CONN_NOPE", raising=False)
    with pytest.raises(rt["AirflowNotFoundException"], match="AIRFLOW_CONN_NOPE"):
        rt["Connection"].get("nope")


def test_a_callable_using_airflow_sdk_connection_and_basehook_is_copied_and_runs_against_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_CONN_SVC", "http://u:pw@svc.example.com:8080/v1")
    src = ("def go():\n    from airflow.sdk import Connection\n    from airflow.hooks.base import BaseHook\n"
           "    a = Connection.get('svc')\n    b = BaseHook.get_connection('svc')\n    return [a.host, b.port, b.schema]\n")
    t = [task("go", "PythonOperator", callable=finfo(tmp_path, src, "go"), op_args=[], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "k1")
    assert not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    mod = load(pkg)
    res = dg.materialize([graph_assets(mod)[0]])
    assert res.success and _asset_value(mod, res) == ["svc.example.com", 8080, "v1"]
    assert any("AIRFLOW_CONN_" in n for n in rep.get("notes", [])), rep.get("notes")
    monkeypatch.delenv("AIRFLOW_CONN_SVC")
    res = dg.materialize([graph_assets(mod)[0]], raise_on_error=False)
    assert not res.success                                                       # loud, not a silent None
    msgs = " ".join(str(e.event_specific_data.error.message) + str(e.event_specific_data.error.cause) for e in res.all_events if e.event_type_value == "STEP_FAILURE")
    assert "AIRFLOW_CONN_SVC" in msgs


def test_airflow_metadata_and_session_calls_stay_loud_stubs_that_name_the_call_and_the_dagster_equivalent(tmp_path):
    cases = {
        "from airflow.models import DagRun\ndef a():\n    return DagRun.find(dag_id='x')\n": ("DagRun", "dagster"),
        "from airflow.utils.session import provide_session\ndef b():\n    return provide_session\n": ("provide_session", "DagsterInstance"),
        "from airflow.models import TaskInstance\ndef c():\n    return TaskInstance\n": ("TaskInstance", "instance"),
        "from airflow.models import XCom\ndef d():\n    return XCom.get_many(dag_ids=['x'])\n": ("XCom", "asset"),
    }
    for i, (src, (call, eq)) in enumerate(cases.items()):
        name = src.split("def ")[1][0]
        f = tmp_path / f"m{i}.py"
        f.write_text(src)
        info = {"name": name, "file": str(f), "module": f"m{i}", "params": [], "source": f"def {name}():\n" + src.split(":\n", 1)[1] if False else src[src.index(f"def {name}"):]}
        sub = tmp_path / f"c{i}"
        sub.mkdir()
        rep, pkg = build(sub, [task("t", "PythonOperator", callable=info, op_args=[], op_kwargs={})], f"s{i}")
        stub = [c for c in rep["caveats"] if c["kind"] == "stub"]
        assert stub and call in stub[0]["reason"] and eq.lower() in stub[0]["reason"].lower(), (call, stub)
