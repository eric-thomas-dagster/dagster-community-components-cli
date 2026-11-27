"""Converter pass 5 (reference/converter-triage.md, "Stage 2a, pass 5"): DAG-factory closure variables, Dataset / Asset objects in callables, the `asset_state_store` context name, DAG and task
callbacks (functions and notifier instances), and the stub-matches-failure verdict in the behaviour harness. Every converter test converts an IR and RUNS the emitted Dagster code; where the
claim is about what the extractor records, the IR comes from real Airflow."""
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
sys.path.insert(0, str(HERE.parent / "parity"))
import convert_ir as C  # noqa: E402,F401
from test_convert_ir import dag as _dag, task  # noqa: E402
from test_converter_stage2a_v6 import build, finfo, graph_assets, load  # noqa: E402

SCRATCH = Path(os.environ.get("EVAL_SCRATCH", Path.home() / "airflow-eval-scratch"))


def af_python(major):
    env = os.environ.get("EVAL_AIRFLOW_PYTHON") if major == 2 else None
    py = env or str(SCRATCH / "venvs" / ("af210" if major == 2 else "af340") / "bin" / "python")
    if not Path(py).exists():
        pytest.skip(f"no Airflow {major} interpreter")
    return py


def extract_and_convert(tmp_path, source, major=2, pkg="x", flags=()):
    """Real Airflow parses `source`; the converter converts what the extractor wrote. Returns (ir dags by id, converter report dags, package dir)."""
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "d.py").write_text(textwrap.dedent(source))
    out = tmp_path / "ir.json"
    r = subprocess.run([af_python(major), "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(dags), "--out", str(out), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert out.exists(), r.stderr[-1500:]
    irs = {d["dag_id"]: d for d in json.loads(out.read_text())["dags"]}
    (tmp_path / pkg).mkdir()
    (tmp_path / pkg / "__init__.py").write_text("")
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(out), "--out-dir", str(tmp_path / pkg / "defs"), "--report", str(tmp_path / "r.json"), "--return-all", *flags],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]
    sys.path.insert(0, str(tmp_path))
    return irs, json.loads((tmp_path / "r.json").read_text())["dags"], tmp_path / pkg


def load_dag(pkg_dir, dag_id):
    return importlib.import_module(f"{pkg_dir.name}.defs.{dag_id}")


def run_assets(mod, **kw):
    return dg.materialize(graph_assets(mod), raise_on_error=False, **kw)


def value(res, key):
    return res.asset_value(dg.AssetKey(key))


# ---- 1. DAG-factory closure variables ---------------------------------------------------------------------------------------------------------------------

FACTORY = '''
    from datetime import datetime
    from airflow.decorators import dag, task

    def create_dag(dag_id, dag_number, labels, ratio):
        @dag(dag_id=dag_id, schedule=None, start_date=datetime(2024, 1, 1), catchup=False)
        def generated():
            greeting = f"hello {dag_number}"

            @task()
            def hello(*args):
                return {"n": dag_number, "labels": labels, "ratio": ratio, "greeting": greeting, "dag": dag_id}

            hello()
        return generated()

    for n in range(1, 4):
        globals()[f"fac_{n}"] = create_dag(f"fac_{n}", n, ("a", n), 0.5)
'''


def test_dag_factory_closure_values_are_recorded_per_generated_dag_and_the_emitted_code_runs_with_them(tmp_path):
    irs, reps, pkg = extract_and_convert(tmp_path, FACTORY, pkg="f1")
    assert sorted(irs) == ["fac_1", "fac_2", "fac_3"]
    clo = irs["fac_2"]["tasks"][0]["callable"]["closure"]
    assert clo["dag_number"] == "2" and clo["labels"] == "('a', 2)" and clo["dag_id"] == "'fac_2'" and clo["greeting"] == "'hello 2'"
    for n in (1, 2, 3):
        assert reps[f"fac_{n}"]["status"] == "converted" and not reps[f"fac_{n}"]["caveats"], reps[f"fac_{n}"]["caveats"]
        res = run_assets(load_dag(pkg, f"fac_{n}"))
        assert res.success
        assert value(res, [f"fac_{n}", "hello"]) == {"n": n, "labels": ("a", n), "ratio": 0.5, "greeting": f"hello {n}", "dag": f"fac_{n}"}   # tuple stays a tuple


def test_a_closure_value_that_is_not_a_literal_stays_a_loud_stub_naming_the_name(tmp_path):
    src = '''
        from datetime import datetime
        from airflow.decorators import dag, task

        def make(dag_id, conf):
            @dag(dag_id=dag_id, schedule=None, start_date=datetime(2024, 1, 1))
            def g():
                @task()
                def t():
                    return conf.read()
                t()
            return g()

        class Conf:
            def read(self):
                return 1

        globals()["fac_obj"] = make("fac_obj", Conf())
    '''
    irs, reps, pkg = extract_and_convert(tmp_path, src, pkg="f2")
    assert "closure" not in irs["fac_obj"]["tasks"][0]["callable"]
    assert any("unresolved name 'conf'" in c["reason"] for c in reps["fac_obj"]["caveats"]), reps["fac_obj"]["caveats"]


# ---- 2. Dataset / Asset objects used in callables ---------------------------------------------------------------------------------------------------------

DATASETS = '''
    from datetime import datetime
    from airflow import Dataset
    from airflow.decorators import dag, task

    MY = Dataset("my_blob_storage://pictures", {"owner": "me"})

    @dag(dag_id="ds_dn", schedule=[MY], start_date=datetime(2024, 1, 1), catchup=False)
    def ds_dn():
        @task
        def create_new(my_dataset):
            print(f"scheduled on {my_dataset}")
            new = Dataset(f"{my_dataset.uri.split(':')[0]}://more", my_dataset.extra)
            return new
        create_new(MY)
    ds_dn()

    @dag(dag_id="ds_up", schedule=None, start_date=datetime(2024, 1, 1), catchup=False)
    def ds_up():
        @task
        def define(folder):
            return Dataset(f"my_blob_storage://{folder}")

        @task
        def folder():
            return "pics"
        d = define(folder())

        @task(outlets=[d])
        def hi():
            return "hi"
        d >> hi()
    ds_up()
'''


def test_dataset_objects_passed_to_or_built_in_a_callable_run_as_a_plain_class(tmp_path):
    irs, reps, pkg = extract_and_convert(tmp_path, DATASETS, pkg="d1")
    arg = irs["ds_dn"]["tasks"][0]["op_args"][0]
    assert arg == {"asset_obj": {"cls": "Dataset", "name": None, "uri": "my_blob_storage://pictures", "extra": {"owner": "me"}}}
    assert reps["ds_dn"]["status"] == "converted" and not [c for c in reps["ds_dn"]["caveats"] if c["kind"] == "stub"], reps["ds_dn"]["caveats"]
    mod = load_dag(pkg, "ds_dn")
    res = run_assets(mod)
    assert res.success
    new = value(res, ["ds_dn", "create_new"])
    assert (new.uri, new.extra) == ("my_blob_storage://more", {"owner": "me"})
    assert "AIRFLOW-CAVEAT" not in (pkg / "defs" / "ds_dn.py").read_text()
    # the upstream DAG: a task returns a Dataset and another uses it as outlet
    assert reps["ds_up"]["status"] == "converted" and not [c for c in reps["ds_up"]["caveats"] if c["kind"] == "stub"], reps["ds_up"]["caveats"]
    res = run_assets(load_dag(pkg, "ds_up"))
    assert res.success


def test_asset_objects_have_name_uri_group_extra_and_default_each_other():
    a = C_runtime("Asset", name="orders/daily", uri="s3://w/o")
    assert (a.name, a.uri, a.group, a.extra) == ("orders/daily", "s3://w/o", "asset", {})
    b = C_runtime("Asset", name="only_name")
    assert (b.name, b.uri) == ("only_name", "only_name")
    d = C_runtime("Dataset", "s3://x/y", {"k": 1})
    assert (d.uri, d.extra) == ("s3://x/y", {"k": 1}) and repr(d) == "Dataset(uri='s3://x/y', extra={'k': 1})"
    assert C_runtime("Dataset", "s3://x/y").extra is None


def C_runtime(cls, *a, **k):
    return runtime_namespace()[cls](*a, **k)


_NS = {}


def runtime_namespace():
    if not _NS:
        import importlib.util
        import tempfile
        f = Path(tempfile.mkdtemp()) / "pass5_runtime_copy.py"
        f.write_text(C.RUNTIME)
        spec = importlib.util.spec_from_file_location("pass5_runtime_copy", f)      # loaded by path: no package name that another test could also use
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pass5_runtime_copy"] = mod
        spec.loader.exec_module(mod)
        _NS.update(vars(mod))
    return _NS


# ---- 3. asset_state_store ---------------------------------------------------------------------------------------------------------------------------------

def test_asset_state_store_persists_a_watermark_on_the_asset_across_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_ASSET_STATE_DIR", str(tmp_path / "state"))
    f = finfo(tmp_path, textwrap.dedent('''
        def load(asset_state_store=None):
            state = asset_state_store[ORDERS]
            wm = state.get("watermark", default="start")
            runs = state.get("total_runs", default=0) + 1
            state.set("watermark", f"after-{runs}")
            state.set("total_runs", runs)
            state.set("summary", {"prev": wm})
            return wm
        '''), "load")
    f["source"] = f["source"]
    mod_file = tmp_path / "load_src.py"
    mod_file.write_text('from airflow.sdk import Asset\nORDERS = Asset(name="orders/daily", uri="s3://warehouse/orders/daily")\n' + f["source"])
    t = [task("load", "PythonOperator", callable=f, op_args=[], op_kwargs={})]
    rep, pkg = build(tmp_path, t, "a1")
    assert not rep["caveats"], rep["caveats"]
    mod = load(pkg)
    vals = []
    for _ in range(3):
        res = dg.materialize(graph_assets(mod))
        vals.append(res.output_for_node(graph_assets(mod)[0].node_def.name))
    assert vals == ["start", "after-1", "after-2"]
    files = list((tmp_path / "state").iterdir())
    assert len(files) == 1 and json.loads(files[0].read_text())["total_runs"] == 3 and json.loads(files[0].read_text())["summary"] == {"prev": "after-2"}


def test_asset_state_store_in_a_real_airflow_3_dag_converts_and_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_ASSET_STATE_DIR", str(tmp_path / "state"))
    src = '''
        from datetime import UTC, datetime
        from airflow.sdk import DAG, Asset, task

        ORDERS = Asset(name="orders/daily", uri="s3://warehouse/orders/daily")

        with DAG(dag_id="ass", schedule=None, start_date=datetime(2026, 1, 1), catchup=False):
            @task(inlets=[ORDERS], outlets=[ORDERS])
            def load(asset_state_store=None):
                state = asset_state_store[ORDERS]
                n = state.get("total_runs", default=0) + 1
                state.set("total_runs", n)
                return n
            load()
    '''
    irs, reps, pkg = extract_and_convert(tmp_path, src, major=3, pkg="a2")
    assert reps["ass"]["status"] == "converted" and not [c for c in reps["ass"]["caveats"] if c["kind"] == "stub"], reps["ass"]["caveats"]
    mod = load_dag(pkg, "ass")
    got = [value(dg.materialize(graph_assets(mod)), ["s3", "warehouse", "orders", "daily"]) for _ in range(2)]
    assert got == [1, 2]


# ---- 4. callbacks -----------------------------------------------------------------------------------------------------------------------------------------

CALLBACKS = '''
    from datetime import datetime, timedelta
    from airflow.decorators import dag, task
    from airflow.notifications.basenotifier import BaseNotifier

    LOG = "@LOG@"

    def _w(line):
        with open(LOG, "a") as f:
            f.write(line + "\\n")

    class Note(BaseNotifier):
        def __init__(self, tag):
            self.tag = tag

        def notify(self, context):
            ti = context.get("ti")
            _w(f"note:{self.tag}:{ti.task_id if ti else '-'}:{ti.state if ti else '-'}")

    def cb(context):
        _w(f"cb:{context['ti'].task_id}:{context['ti'].state}")

    def dag_ok(context):
        _w(f"dag_ok:{context['dag_run'].dag_id}:{context['dag_run'].state}")

    def dag_bad(context):
        _w(f"dag_bad:{context['dag_run'].state}:{bool(context['exception'])}:{context['reason']}")

    @dag(dag_id="cbs", schedule=None, start_date=datetime(2024, 1, 1), catchup=False,
         on_success_callback=[dag_ok], on_failure_callback=[dag_bad, Note("dagfail")],
         default_args={"on_execute_callback": [cb], "retries": 1, "retry_delay": timedelta(seconds=0)})
    def cbs():
        @task(on_success_callback=[cb, Note("ok")], on_failure_callback=[cb], on_retry_callback=[Note("retry")])
        def good():
            return 1

        @task(on_failure_callback=[cb, Note("bad")], on_retry_callback=[cb])
        def bad():
            raise ValueError("boom")

        good()
        bad()
    cbs()
'''


def run_status_sensor(mod, name):
    return next(v for v in vars(mod).values() if isinstance(v, dg.SensorDefinition) and v.name == name)


def test_task_callbacks_functions_and_notifier_instances_run_with_a_ti_state_and_dag_callbacks_run_in_run_status_sensors(tmp_path):
    log = tmp_path / "cb.log"
    irs, reps, pkg = extract_and_convert(tmp_path, CALLBACKS.replace("@LOG@", str(log)), pkg="cb1")
    d = irs["cbs"]
    assert set(d["callback_fns"]) == {"on_success_callback", "on_failure_callback"}
    notifier = d["callback_fns"]["on_failure_callback"][1]["notifier"]
    assert notifier["class"]["name"] == "Note" and notifier["state"] == {"tag": "'dagfail'"}
    rep = reps["cbs"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    assert not [c for c in rep["caveats"] if "callbacks" in c["reason"] and "not converted" in c["reason"]], rep["caveats"]
    text = (pkg / "defs" / "cbs.py").read_text()
    assert "run_status_sensor" in text and "class Note(BaseNotifier)" in text and text.count("class Note(") == 1
    mod = load_dag(pkg, "cbs")
    from dagster import DagsterInstance
    with DagsterInstance.ephemeral() as inst:
        res = dg.materialize(graph_assets(mod), raise_on_error=False, instance=inst)
        assert not res.success                                        # `bad` fails, as in Airflow
        lines = log.read_text().splitlines()
        # task level: on_execute (default_args) on every attempt, success / retry / failure callbacks, notifier instances, ti.state set by the adapter
        assert lines.count("cb:good:running") == 1 and lines.count("cb:bad:running") == 2
        assert "cb:good:success" in lines and "note:ok:good:success" in lines
        assert "cb:bad:up_for_retry" in lines and "cb:bad:failed" in lines and "note:bad:bad:failed" in lines
        assert not any(l.startswith("note:retry:bad") for l in lines)        # on_retry of `good` never fires
        # DAG level: run-status sensors on the job; run them against the run that just failed
        sensor = run_status_sensor(mod, "cbs_on_failure_callback")
        run = inst.get_run_records()[0].dagster_run
        ev = next(e for e in inst.all_logs(run.run_id) if e.dagster_event and e.dagster_event.event_type_value == "PIPELINE_FAILURE")
        sctx = dg.build_run_status_sensor_context(sensor_name=sensor.name, dagster_event=ev.dagster_event, dagster_instance=inst, dagster_run=run)
        sensor(sctx)
        lines = log.read_text().splitlines()
        assert "dag_bad:failed:True:task_failure" in lines and "note:dagfail:-:-" in lines      # a DAG callback has no task instance: the notifier ran with the minimal context


def test_dag_callback_success_sensor_runs_on_a_successful_run_and_sla_miss_callback_is_reported_not_preserved(tmp_path):
    log = tmp_path / "ok.log"
    f = finfo(tmp_path, "def t():\n    return 5\n", "t")
    cb = finfo(tmp_path, f"def done(context):\n    open({str(log)!r}, 'a').write('done:' + context['dag_run'].state + ':' + context['reason'] + ':' + context['ds'] + '\\n')\n", "done")
    d = {"callback_fns": {"on_success_callback": [cb]}, "sla_miss_callback": True, "has_callbacks": True}
    rep, pkg = build(tmp_path, [task("t", "PythonOperator", callable=f, op_args=[], op_kwargs={})], "s1", extra_dag=d)
    assert any("sla_miss_callback" in c["reason"] and "not converted" in c["reason"] for c in rep["caveats"]), rep["caveats"]
    assert not any("on_success_callback" in c["reason"] and "not converted" in c["reason"] for c in rep["caveats"])
    mod = load(pkg)
    from dagster import DagsterInstance
    with DagsterInstance.ephemeral() as inst:
        res = dg.materialize(graph_assets(mod), instance=inst)
        assert res.success
        sensor = run_status_sensor(mod, "d1_on_success_callback")
        run = inst.get_run_records()[0].dagster_run
        ev = next(e for e in inst.all_logs(run.run_id) if e.dagster_event and e.dagster_event.event_type_value == "PIPELINE_SUCCESS")
        sensor(dg.build_run_status_sensor_context(sensor_name=sensor.name, dagster_event=ev.dagster_event, dagster_instance=inst, dagster_run=run))
    line = log.read_text().strip()
    assert line.startswith("done:success:success:") and len(line.split(":")[-1]) == 10


def test_an_old_ir_without_the_callable_records_keeps_the_degraded_caveat_and_asks_to_re_extract(tmp_path):
    f = finfo(tmp_path, "def t():\n    return 5\n", "t")
    rep, pkg = build(tmp_path, [task("t", "PythonOperator", callable=f, op_args=[], op_kwargs={})], "s2", extra_dag={"has_callbacks": True})
    assert any("DAG-level callbacks" in c["reason"] and "re-extract" in c["reason"] for c in rep["caveats"]), rep["caveats"]


# ---- 6. harness: a loud stub that fails is not a success --------------------------------------------------------------------------------------------------

def test_a_route_whose_failing_step_is_a_loud_stub_scores_stub_matches_failure_not_matches_baseline_failure():
    from test_eval_harness import SPEC, af, route
    import behavior_compare as B
    import eval_report as R
    tasks = {"a": {"state": "failed", "xcom": {}}, "b": {"state": "upstream_failed", "xcom": {}}}
    tm = {"a": {"mode": "replaced", "asset": "d/a"}, "b": {"mode": "replaced", "asset": "d/b"}}
    st = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"d/a": {"deps": [], "retry": [], "pool": []}, "d/b": {"deps": ["d/a"], "retry": [], "pool": []}}}
    real = route(state="failed", assets={"d/a": {"state": "failed", "errors": ["ValueError: by design"]}, "d/b": {"state": "not_run"}}, structure=st)
    stub = route(state="failed", assets={"d/a": {"state": "failed", "errors": ["dagster.Failure: AIRFLOW-CAVEAT: unresolved name 'x'"]}, "d/b": {"state": "not_run"}}, structure=st)
    r = B.compare(SPEC, af("failed", tasks, errors=["ValueError: by design"]), af("failed", tasks), real, tm, "converter")
    assert r["rollup"]["verdict"] == "matches-baseline-failure" and "detail" not in r["rollup"]
    r = B.compare(SPEC, af("failed", tasks, errors=["ValueError: by design"]), af("failed", tasks), stub, tm, "converter")
    assert r["rollup"]["verdict"] == "matches-baseline-failure" and r["rollup"]["detail"] == "stub-matches-failure" and "not a success" in r["rollup"]["why"]
    # the report counts it separately and not in the either-form share
    recs = [{"behavior": {"routes": {"converter": r}}, "baseline_failed": False, "verdicts": {"converter": "stub-matches-failure"}}]
    c = R.counts(recs, "converter")
    assert c["stub-matches-failure"] == 1 and c["matches-baseline-failure"] == 0
    assert sum(c[v] for v in R.GOOD_LENIENT) == 0 and "stub-matches-failure" not in R.GOOD_LENIENT
    assert any("stub-matches-failure" in row for row in R.route_table(recs, "converter"))


def test_gather_reports_the_stratum_of_the_current_converter_report_not_the_manifest(tmp_path):
    import eval_report as R
    priv = tmp_path / "r1-private"
    (priv / "behavior" / "d").mkdir(parents=True)
    (priv / "kit_index.json").write_text(json.dumps({"k::d": {"dir": "d", "dag_id": "d", "stratum": "stubbed", "pattern": "python", "repo": "k", "project": "p"}}))
    (priv / "behavior" / "d" / "behavior.json").write_text(json.dumps({"stratum": "clean", "manifest_stratum": "stubbed", "baseline": {"class": ["ok", ""]}, "routes": {"converter": {"rollup": {"verdict": "equivalent"}}}}))
    rec = R.gather(tmp_path, "r1")["k::d"]
    assert rec["stratum"] == "clean" and rec["manifest_stratum"] == "stubbed"


def test_eval_behavior_recomputes_the_stratum_from_the_current_report_and_converts_manifest_deferred_dags():
    import eval_behavior as EB
    import eval_corpus as EC
    assert EC.converter_stratum({"status": "converted", "caveats": []})[0] == "clean"
    src = (HERE.parent / "eval_behavior.py").read_text()
    assert "EC.converter_stratum(rep)[0]" in src and 'row["converter"] != "deferred"' not in src and "manifest_stratum" in src
    assert EB.EC is EC
