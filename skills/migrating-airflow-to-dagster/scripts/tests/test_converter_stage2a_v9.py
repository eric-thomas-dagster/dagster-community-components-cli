"""Converter pass 6 and harness fixes (reference/converter-triage.md, "Stage 2a, pass 6"). Every converter test converts an IR written by real Airflow where the claim is about what the extractor records, and
RUNS the emitted Dagster code; every harness test runs `run_dagster.py` or the comparison functions on recorded-shaped data. No DAG-specific case exists."""
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
import behavior_compare as B  # noqa: E402
import convert_ir as C  # noqa: E402
from test_converter_stage2a_v6 import graph_assets  # noqa: E402
from test_converter_stage2a_v8 import extract_and_convert, load_dag, run_assets, value  # noqa: E402
from test_eval_harness import _run_converter_module  # noqa: E402


def step_failures(res):
    return [e for e in res.all_events if e.event_type_value == "STEP_FAILURE"]


# ============================ HARNESS ============================================================================================================================

# ---- (a) a dynamic partition set needs an instance and a key ----------------------------------------------------------------------------------------------------

DYNAMIC = '''
import dagster as dg

parts = dg.DynamicPartitionsDefinition(name="incoming")


@dg.asset(partitions_def=parts)
def summarize(context: dg.AssetExecutionContext):
    print("partition", context.partition_key)
    return context.partition_key
'''

MULTI = '''
import dagster as dg


@dg.asset(partitions_def=dg.MultiPartitionsDefinition({"region": dg.StaticPartitionsDefinition(["eu", "us"]), "seg": dg.DynamicPartitionsDefinition(name="segs")}))
def multi(context: dg.AssetExecutionContext):
    return str(context.partition_key)
'''


def test_run_dagster_registers_a_key_for_a_dynamic_partition_set_and_records_it(tmp_path):
    r = _run_converter_module(tmp_path, DYNAMIC)
    assert r["error"] is None, r
    assert r["state"] == "success" and r["partition_key"] == "harness_partition"
    assert json.loads(r["assets"]["summarize"]["value"]) == "harness_partition"
    m = _run_converter_module(tmp_path / "m", MULTI)                                # a multi-partition set with a dynamic dimension gets a key for that dimension too
    assert m["error"] is None and m["state"] == "success" and "harness_partition" in json.loads(m["assets"]["multi"]["value"]), m


# ---- (e) dag_run.partition_key: dag.test() prints None, the partitioned route prints the key ---------------------------------------------------------------------------

def test_the_harness_partition_key_in_a_printed_line_is_read_as_None_and_the_rest_of_the_line_is_still_compared():
    route_text = "All 24 hourly partitions received. Day: harness_partition\nplain line\n"
    lines = B.printed_lines(route_text, "harness_partition")
    assert lines == ["All 24 hourly partitions received. Day: None", "plain line"]
    base = {"stdout_lines": ["All 24 hourly partitions received. Day: None", "plain line"]}
    ok = B.compare_stdout(base, base, {"stdout_lines": lines, "partition_key": "harness_partition"})
    assert ok["verdict"] == "match" and ok["evidence"]["partition_key_read_as_None"] == "harness_partition"
    bad = B.compare_stdout(base, base, {"stdout_lines": ["All 12 hourly partitions received. Day: None", "plain line"], "partition_key": "harness_partition"})
    assert bad["verdict"] == "differ"                                              # only the key is excused, not the text around it
    # a date-shaped partition key is read as None BEFORE the date mask would hide it
    assert B.printed_lines("Day: 2026-11-27\n", "2026-11-27") == ["Day: None"]


# ---- (b) loop groups: Airflow's inner task ids belong to the loop's one step ------------------------------------------------------------------------------------------

LOOP = '''
    from airflow.sdk import LoopDecision, dag, task, task_group

    @dag(schedule=None, catchup=False)
    def lp():
        @task_group
        def grow():
            @task
            def step(*, loop):
                return (0 if loop.index == 1 else loop.previous) + 1

            step()

        def done(*, loop):
            return LoopDecision(stop=loop.result >= 3, reason="enough")

        @task
        def finished():
            return "ok"

        grow.loop(max_iterations=5, until=done) >> finished()

    lp()
'''


def test_loop_group_inner_task_ids_are_reported_and_mapped_to_the_loops_step(tmp_path):
    irs, reps, pkg = extract_and_convert(tmp_path, LOOP, major=3, pkg="v9_l1")
    rep = reps["lp"]
    assert rep["status"] == "converted", rep
    gate = next(m for m in rep["loop_members"] if m != "grow.step")                              # the gate is named after the `until` callable (`grow.done`), or `__loop_gate`
    assert set(rep["loop_members"]) == {"grow.step", gate} and set(rep["loop_members"].values()) <= set(rep["task_keys"].values())
    assert {"grow.step", gate} <= set(rep["task_keys"])
    assert set(rep["loop_members"].values()) <= {"/".join(k) for k in rep["keys"]}           # each maps to an asset the module defines
    import eval_behavior as EB
    tm = EB.converter_taskmap(rep)
    assert tm["grow.step"]["mode"] == "folded" and tm["grow.step"]["asset"] == rep["loop_members"]["grow.step"]
    res = run_assets(load_dag(pkg, "lp"))
    assert res.success and value(res, ["lp", "finished"]) == "ok"
    # compared against Airflow's recorded states, the inner tasks match the loop's asset and the verdict is not `different`
    import dag_spec
    spec = json.loads(json.dumps(dag_spec.build(irs["lp"], None, None, None, "3")))
    a1 = {"state": "success", "tasks": {t: {"state": "success", "xcom": {}} for t in ("grow.step", gate, "finished")}}
    keys = {k: {"deps": [], "node": k} for k in set(rep["task_keys"].values())}
    route = {"state": "success", "error": None, "assets": {k: {"state": "success"} for k in keys}, "structure": {"assets": keys, "jobs": ["lp"], "schedules": [], "sensors": []}}
    out = B.compare(spec, a1, a1, route, tm, "converter")
    assert out["dimensions"]["tasks"]["verdict"] == "match", out["dimensions"]["tasks"]


# ---- (c) a folded chain whose leaf fails ------------------------------------------------------------------------------------------------------------------------

def test_loop_members_of_a_loop_folded_into_its_consumer_map_to_the_consumers_asset():
    dag = {"tasks": [{"task_id": "refine", "loop_members": ["refine.a", "refine.gate"]}, {"task_id": "finished"}]}
    keyparts = {"refine": ["d", "refine"], "finished": ["d", "finished"]}                      # every task has key parts, the folded one too
    assert C.loop_members_report(dag, keyparts, {"refine": "finished"}) == {"refine.a": "d/finished", "refine.gate": "d/finished"}
    assert C.loop_members_report(dag, keyparts, {}) == {"refine.a": "d/refine", "refine.gate": "d/refine"}


def _chain_case(af_states, route_states):
    spec = {"dag_id": "d", "tasks": [
        {"task_id": "start", "upstream": [], "downstream": ["leaf1", "leaf2"]}, {"task_id": "leaf1", "upstream": ["start"], "downstream": ["join"]},
        {"task_id": "leaf2", "upstream": ["start"], "downstream": ["join"]}, {"task_id": "join", "upstream": ["leaf1", "leaf2"], "downstream": ["after"]},
        {"task_id": "after", "upstream": ["join"], "downstream": []}], "schedule": {}}
    a1 = {"state": "failed", "tasks": {t: {"state": s, "xcom": {}} for t, s in af_states.items()}}
    tm = {"start": {"mode": "replaced", "asset": "d/start"}, "leaf1": {"mode": "folded", "asset": "d/join"}, "leaf2": {"mode": "folded", "asset": "d/join"},
          "join": {"mode": "replaced", "asset": "d/join"}, "after": {"mode": "replaced", "asset": "d/after"}}
    assets = {"d/start": {"deps": []}, "d/join": {"deps": ["d/start"]}, "d/after": {"deps": ["d/join"]}}
    route = {"state": "failed", "error": None, "assets": {k: {"state": s} for k, s in route_states.items()}, "structure": {"assets": assets, "jobs": [], "schedules": [], "sensors": []}}
    return B.compare(spec, a1, a1, route, tm, "converter")


def test_a_folded_chains_leaf_failure_translates_to_upstream_failed_for_the_final_step_and_matches():
    out = _chain_case({"start": "success", "leaf1": "failed", "leaf2": "success", "join": "upstream_failed", "after": "upstream_failed"},
                      {"d/start": "success", "d/join": "failed", "d/after": "not_run"})
    assert out["dimensions"]["tasks"]["verdict"] == "match", out["dimensions"]["tasks"]
    assert out["dimensions"]["failure"]["verdict"] == "match", out["dimensions"]["failure"]
    assert any(a["task"] == "join" for a in out["dimensions"]["tasks"]["evidence"]["accepted_representation"])


def test_the_translation_does_not_hide_a_real_difference():
    # Airflow: join failed by itself (no folded step failed) but the route's asset also failed: that is the same failed state, no translation needed; and
    # Airflow: join upstream_failed because of a task OUTSIDE the folded chain is not translated either.
    out = _chain_case({"start": "failed", "leaf1": "upstream_failed", "leaf2": "upstream_failed", "join": "upstream_failed", "after": "upstream_failed"},
                      {"d/start": "success", "d/join": "failed", "d/after": "not_run"})
    assert out["dimensions"]["tasks"]["verdict"] == "differ"


# ---- (d) wall-clock strings --------------------------------------------------------------------------------------------------------------------------------------

def _val_case(af, af2, rt, extra=None, run_epoch=None):
    spec = {"dag_id": "d", "tasks": [{"task_id": "t", "upstream": [], "downstream": [], "reads_value_from": []}], "schedule": {}}
    mk = lambda v: {"state": "success", "tasks": {"t": {"state": "success", "xcom": {"return_value": json.dumps(v)}}}}      # noqa: E731
    a1, a2 = mk(af), mk(af2)
    if run_epoch:
        a1["run_epoch"] = run_epoch
    route = {"state": "success", "error": None, "assets": {"d/t": {"state": "success", "value": json.dumps(rt)}}, "structure": {"assets": {"d/t": {"deps": []}}, "jobs": [], "schedules": [], "sensors": []}}
    return B.compare(spec, a1, a2, route, {"t": {"mode": "replaced", "asset": "d/t"}}, "converter", extra_runs=[mk(extra)] if extra is not None else None)["dimensions"]["values"]


def test_two_baseline_runs_in_the_same_second_do_not_make_a_wall_clock_value_comparable():
    import calendar
    import time
    now = time.gmtime()
    af = time.strftime("%a %b %d %H:%M:%S UTC %Y", now)
    later = time.strftime("%a %b %d %H:%M:%S UTC %Y", time.gmtime(calendar.timegm(now) + 2))
    v = _val_case(af, af, later, run_epoch=calendar.timegm(now))
    assert v["verdict"] == "not-compared" and "wall-clock" in str(v["reason"]) + json.dumps(v.get("evidence"))
    iso = _val_case("2026-11-27T14:25:28+00:00", "2026-11-27T14:25:28+00:00", "2026-11-27T14:25:30+00:00", run_epoch=calendar.timegm((2026, 11, 27, 14, 26, 0)))
    assert iso["verdict"] == "not-compared"


def test_a_timestamp_that_is_not_the_run_time_or_a_real_difference_is_still_compared():
    import calendar
    # a fixed business timestamp far from the run time (a logical date) is a value: a different one is a difference
    far = _val_case("2021-01-01T00:00:00", "2021-01-01T00:00:00", "2021-01-02T00:00:00", run_epoch=calendar.timegm((2026, 11, 27, 14, 26, 0)))
    assert far["verdict"] == "differ"
    # text around the timestamp differs
    other = _val_case("done at 2026-11-27 14:25:28", "done at 2026-11-27 14:25:28", "failed at 2026-11-27 14:25:30", run_epoch=calendar.timegm((2026, 11, 27, 14, 26, 0)))
    assert other["verdict"] == "differ"
    assert B.wallclock_only_difference("a 2026-11-27 14:25:28 b", "a 2026-11-27 14:25:29 b") is not None
    assert B.wallclock_only_difference("same", "same") is None


def test_a_value_that_differs_from_an_earlier_baseline_run_is_nondeterministic():
    same_second = _val_case("x-1", "x-1", "x-2", extra="x-9")                     # the two measured runs agree, an earlier recorded run does not
    assert same_second["verdict"] == "not-compared"
    stable = _val_case("x-1", "x-1", "x-1", extra="x-1")
    assert stable["verdict"] == "match"


# ---- baseline classification: deferral and asset registry limits of dag.test() ---------------------------------------------------------------------------------------

def test_dag_test_limits_with_deferrable_tasks_and_unregistered_inlet_assets_are_baseline_failed_not_blamed_on_the_route():
    spec = {"dag_id": "d", "tasks": [{"task_id": "t", "operator": "MyOp", "arguments": {"deferrable": True}, "upstream": [], "downstream": []}]}
    for err in ("TypeError: MyOp.execute_complete() missing 1 required positional argument: 'kwarg'", "NotImplementedError"):
        cls, why = B.baseline_class({"state": "failed", "errors": [err], "tasks": {"t": {"state": "failed"}}}, None, spec)
        assert cls == "baseline-failed" and "deferrable" in why, (err, cls, why)
    # without deferrable=True a NotImplementedError is the DAG's own failure
    spec2 = {**spec, "tasks": [{**spec["tasks"][0], "arguments": {}}]}
    assert B.baseline_class({"state": "failed", "errors": ["NotImplementedError"], "tasks": {"t": {"state": "failed"}}}, None, spec2)[0] == "by-design-failure"
    cls, _ = B.baseline_class({"state": "failed", "errors": ["AttributeError: 'InProcessSupervisorComms' object has no attribute 'asend'. Did you mean: 'send'?"], "tasks": {"t": {"state": "failed"}}}, None, spec2)
    assert cls == "baseline-failed"                                                            # dag.test()'s in-process supervisor cannot run an async task's `aget` / `aset`
    cls, _ = B.baseline_class({"state": "failed", "errors": ["airflow.sdk.exceptions.AirflowRuntimeError: ASSET_NOT_FOUND: {'name': 'a'}"], "tasks": {"t": {"state": "failed"}}}, None, spec2)
    assert cls == "baseline-failed"


# ============================ CONVERTER ===========================================================================================================================

# ---- (f) NEVER_EXPIRE and the task state store, sync and async -------------------------------------------------------------------------------------------------------

STATE = '''
    import asyncio
    from datetime import datetime, timedelta
    from airflow.sdk import DAG, task
    from airflow.sdk.execution_time.context import NEVER_EXPIRE

    async def _page(p):
        await asyncio.sleep(0)
        return p * 10

    with DAG(dag_id="ts", schedule=None, start_date=datetime(2026, 1, 1), catchup=False):
        @task(retries=2, retry_delay=timedelta(seconds=0))
        def run_job(task_state_store=None, ti=None):
            job = task_state_store.get("job_id")
            if job:
                print(f"try {ti.try_number}: reattached to {job}")
            else:
                task_state_store.set("job_id", "job-7", retention=NEVER_EXPIRE)
                print(f"try {ti.try_number}: submitted")
                raise RuntimeError("simulated crash after submit")
            return {"job": job, "tries": ti.try_number}

        @task(retries=2, retry_delay=0)
        async def ingest(task_state_store=None, ti=None):
            done = await task_state_store.aget("done", default=[])
            for p in [1, 2, 3]:
                if p in done:
                    continue
                done.append(p)
                await task_state_store.aset("done", done, retention=NEVER_EXPIRE)
                if ti.try_number == 1 and p == 2:
                    raise RuntimeError("staged loss")
            return {"pages": sorted(done), "rows": sum([await _page(p) for p in done]), "tries": ti.try_number}

        run_job()
        ingest()
'''


def test_never_expire_and_the_task_state_store_survive_a_retry_in_sync_and_async_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_TASK_STATE_DIR", str(tmp_path / "state"))
    irs, reps, pkg = extract_and_convert(tmp_path, STATE, major=3, pkg="v9_s1")
    assert reps["ts"]["status"] == "converted" and not [c for c in reps["ts"]["caveats"] if c["kind"] == "stub"], reps["ts"]["caveats"]
    res = run_assets(load_dag(pkg, "ts"))
    assert res.success, [e.event_specific_data.error.cause.message if e.event_specific_data.error.cause else "" for e in step_failures(res)]
    assert value(res, ["ts", "run_job"]) == {"job": "job-7", "tries": 2}                        # the retry reattached from the store
    assert value(res, ["ts", "ingest"]) == {"pages": [1, 2, 3], "rows": 60, "tries": 2}        # the async retry resumed from the checkpoint
    assert C.NEVER_EXPIRE if hasattr(C, "NEVER_EXPIRE") else True


# ---- (g) task.render_template, templates_exts files --------------------------------------------------------------------------------------------------------------

def test_task_render_template_renders_strings_and_containers_with_the_context_and_a_templates_exts_file_is_read(tmp_path):
    (tmp_path / "dags").mkdir()
    (tmp_path / "dags" / "q.sql").write_text("select * from t where run = '{{ run_id }}' and p = '{{ params.p }}'\n")
    src = '''
        from datetime import datetime
        from airflow.sdk import dag, get_current_context, task

        @dag(schedule=None, start_date=datetime(2026, 1, 1), catchup=False, params={"p": "pv"})
        def rt():
            @task(templates_exts=[".sql"])
            def go(sql, tag, data_interval_end):
                context = get_current_context()
                return {"sql": sql.strip(), "own": context["task"].render_template("{{ params.p }}-{{ ds }}", context),
                        "deep": context["task"].render_template({"a": ["{{ params.p }}", 3]}, context), "end": str(data_interval_end)[:4]}

            go("q.sql", tag="{{ params.p }}")

        rt()
    '''
    dags = tmp_path / "dags"
    # the template file sits next to the DAG file, as Airflow's default search path expects
    import textwrap as tw
    (dags / "d.py").write_text(tw.dedent(src))
    out = tmp_path / "ir.json"
    from test_converter_stage2a_v8 import af_python
    r = subprocess.run([af_python(3), "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(dags), "--out", str(out), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert out.exists(), r.stderr[-800:]
    ir = json.loads(out.read_text())["dags"][0]
    assert ir["tasks"][0]["template_files"] == {"op_args.0": "select * from t where run = '{{ run_id }}' and p = '{{ params.p }}'\n"}
    (tmp_path / "v9_pk").mkdir()
    (tmp_path / "v9_pk" / "__init__.py").write_text("")
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(out), "--out-dir", str(tmp_path / "v9_pk" / "defs"), "--report", str(tmp_path / "r.json"), "--return-all"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["rt"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    sys.path.insert(0, str(tmp_path))
    res = run_assets(load_dag(tmp_path / "v9_pk", "rt"))
    assert res.success, [e.event_specific_data.error.cause.message if e.event_specific_data.error.cause else "" for e in step_failures(res)]
    v = value(res, ["rt", "go"])
    run_id = v["sql"].split("run = '")[1].split("'")[0]
    assert run_id and v["sql"] == f"select * from t where run = '{run_id}' and p = 'pv'"      # the file's text, rendered
    assert v["own"].startswith("pv-20") and v["deep"] == {"a": ["pv", 3]} and v["end"].startswith("20")


# ---- (h) a module-level ObjectStoragePath -----------------------------------------------------------------------------------------------------------------------

OSP = '''
    from datetime import datetime
    from airflow.sdk import ObjectStoragePath, dag, task

    # `airflow` appears in the bucket name only: it is not a reference to the package
    base = ObjectStoragePath("s3://aws_default@airflow-tutorial-data/")
    local = ObjectStoragePath("file://@LOCAL@")

    @dag(schedule=None, start_date=datetime(2026, 1, 1), catchup=False)
    def osp():
        @task
        def write():
            local.mkdir(parents=True, exist_ok=True)
            p = local / "x.txt"
            p.write_text("hello")
            return str(p)

        @task
        def remote():
            return str(base / "y.txt")

        write()
        remote()

    osp()
'''


def test_a_module_level_object_storage_path_is_built_on_first_use_and_a_bucket_named_airflow_is_not_an_airflow_reference(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRFLOW_CONN_AWS_DEFAULT", raising=False)
    local = tmp_path / "data"
    irs, reps, pkg = extract_and_convert(tmp_path, OSP.replace("@LOCAL@", str(local)), major=3, pkg="v9_o1")
    rep = reps["osp"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    mod = load_dag(pkg, "osp")                                                                   # importing does NOT need the connection (it would raise if `base` were built at import)
    res = run_assets(mod)
    assert not res.success                                                                       # `remote` needs aws_default: loud, and naming it
    assert value(res, ["osp", "write"]) == str(local / "x.txt") and (local / "x.txt").read_text() == "hello"
    msgs = " ".join(e.event_specific_data.error.cause.message if e.event_specific_data.error.cause else e.event_specific_data.error.message for e in step_failures(res))
    assert "aws_default" in msgs.lower() or "AWS_DEFAULT" in msgs


def test_mentions_airflow_looks_at_code_not_at_words_in_strings():
    assert not C.mentions_airflow("base = ObjectStoragePath('s3://aws_default@airflow-tutorial-data/')")
    assert not C.mentions_airflow("print('run by airflow')")
    assert C.mentions_airflow("from airflow.models import DagRun")
    assert C.mentions_airflow("import airflow.utils.dates as d")
    assert C.mentions_airflow("x = airflow.settings.Session()")
    assert C.mentions_airflow("import_module('airflow.models')")


# ---- (i) the task context as a parameter, asset functions' inlets -----------------------------------------------------------------------------------------------------------

CTX = '''
    from datetime import datetime
    from airflow.sdk import Asset, asset, dag, task

    @asset(uri="s3://bucket/src", schedule=None)
    def src():
        return None

    @asset(uri="s3://bucket/dst", schedule=None)
    def dst(self, context, src):
        return {"self": self.name, "inlet": src.name, "run_id": bool(context["run_id"]), "dag": context["dag"].dag_id, "ti": context["ti"].task_id,
                "params": context["params"], "ds": len(context["ds"]), "var": context["var"].value.my_var, "conn": context["conn"].my_conn.host, "keys": sorted(k for k in ("ti", "dag_run", "params", "logical_date", "ds", "run_id", "task", "dag", "var", "conn") if k in context)}

'''


def test_an_asset_function_that_takes_the_context_gets_it_and_its_inlets_as_assets(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_VAR_MY_VAR", "vv")
    monkeypatch.setenv("AIRFLOW_CONN_MY_CONN", "http://u:p@example.org:80/x")
    irs, reps, pkg = extract_and_convert(tmp_path, CTX, major=3, pkg="v9_c1")
    assert reps["dst"]["status"] == "converted" and not [c for c in reps["dst"]["caveats"] if c["kind"] == "stub"], reps["dst"]["caveats"]
    res = run_assets(load_dag(pkg, "dst"))
    assert res.success, [e.event_specific_data.error.cause.message if e.event_specific_data.error.cause else "" for e in step_failures(res)]
    got = res.asset_value(dg.AssetKey(["s3", "bucket", "dst"]))
    assert got["self"] == "dst" and got["inlet"] == "src" and got["run_id"] and got["dag"] == "dst" and got["ti"] == "dst" and got["var"] == "vv" and got["conn"] == "example.org"
    assert got["keys"] == sorted(["ti", "dag_run", "params", "logical_date", "ds", "run_id", "task", "dag", "var", "conn"])


def test_the_context_dict_is_given_to_a_plain_python_callable_that_names_it_and_a_callable_with_kwargs_keeps_working():
    seen = {}

    def named(context, x=0):
        seen["named"] = sorted(context)

    def kw(**ctx):
        seen["kw"] = "ti" in ctx and "context" not in ctx

    ns: dict = {}
    exec(compile(C.RUNTIME, "runtime", "exec"), ns)
    ctx = {"ti": 1, "ds": "d"}
    ns["call_with_context"](named, [], {}, ctx)
    ns["call_with_context"](kw, [], {}, ctx)
    assert seen == {"named": ["ds", "ti"], "kw": True}


def test_airflow_metadata_and_session_members_of_the_context_stay_loud_stubs_naming_the_call():
    with pytest.raises(C.Defer) as e:
        C.resolve_callable({"name": "f", "file": __file__, "params": ["context"], "source": "def f(context):\n    from airflow.utils.session import create_session\n    with create_session() as s:\n        return s.query(1)\n"})
    assert "airflow" in str(e.value).lower() and ("context.instance" in str(e.value) or "session" in str(e.value).lower())


# ---- (j) the pushed dict's key order survives the xcom_push round trip --------------------------------------------------------------------------------------------------

PUSH = '''
    from datetime import datetime
    from airflow.sdk import dag, task

    @dag(schedule=None, start_date=datetime(2026, 1, 1), catchup=False)
    def px():
        @task
        def push(ti=None):
            ti.xcom_push(key="obj", value={f"key{i}": i for i in range(12)} | {"z": {"b": 1, "a": 2}})
            ti.xcom_push(key="rows", value=[{"b": 1, "a": 2}])

        @task
        def pull(ti=None):
            obj = ti.xcom_pull(task_ids="push", key="obj")
            return {"keys": list(obj), "inner": list(obj["z"]), "rows": [list(r) for r in ti.xcom_pull(task_ids="push", key="rows")]}

        push() >> pull()

    px()
'''


def test_a_dict_pushed_under_a_key_is_pulled_back_in_the_pushed_key_order(tmp_path):
    irs, reps, pkg = extract_and_convert(tmp_path, PUSH, major=3, pkg="v9_p1")
    assert reps["px"]["status"] == "converted" and not [c for c in reps["px"]["caveats"] if c["kind"] == "stub"], reps["px"]["caveats"]
    res = run_assets(load_dag(pkg, "px"))
    assert res.success
    v = value(res, ["px", "pull"])
    assert v["keys"] == [f"key{i}" for i in range(12)] + ["z"]               # Dagster's JSON metadata sorts keys (key0, key1, key10, key11, key2 ...): the order is kept beside it
    assert v["inner"] == ["b", "a"] and v["rows"] == [["b", "a"]]


# ---- (k) deferrable operators: run in place -------------------------------------------------------------------------------------------------------------------

DEFER = '''
    import asyncio
    from datetime import datetime
    from typing import Any, AsyncIterator
    from asgiref.sync import sync_to_async
    from airflow.configuration import conf
    from airflow.models.baseoperator import BaseOperator
    from airflow.triggers.base import BaseTrigger, StartTriggerArgs, TriggerEvent
    from airflow.decorators import dag


    class Wait(BaseTrigger):
        def __init__(self, polls: int = 2, label: str = "none"):
            super().__init__()
            self.polls, self.label = polls, label

        def serialize(self):
            return ("x.Wait", {"polls": self.polls, "label": self.label})

        async def run(self) -> AsyncIterator[TriggerEvent]:
            n = 0
            while True:
                n += 1
                if await self.ready(n):
                    self.label = "done-" + self.label
                    yield TriggerEvent(self.serialize())
                    return
                await asyncio.sleep(0)

        @sync_to_async
        def ready(self, n):
            return n >= self.polls


    class Deferring(BaseOperator):
        def __init__(self, *, polls: int = 1, deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False), **kwargs) -> None:
            super().__init__(**kwargs)
            self.polls, self._defer = polls, deferrable

        def execute(self, context):
            if self._defer:
                self.defer(trigger=Wait(polls=self.polls, label="a"), method_name="execute_complete", kwargs={"extra": "kw"})
            return "sensor-mode"

        def execute_complete(self, context, event: tuple[str, dict[str, Any]], extra: str):
            context["ti"].xcom_push("label", event[1]["label"])
            return f"{event[1]['label']}/{extra}/{event[1]['polls']}"


    class FromTrigger(BaseOperator):
        start_trigger_args = StartTriggerArgs(trigger_cls="pkg.mod.Wait", trigger_kwargs={"polls": 3, "label": "s"}, next_method="execute_complete", next_kwargs={"extra": "nk"}, timeout=None)
        start_from_trigger = True

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)

        def execute_complete(self, context, event, extra):
            return [event[1]["label"], extra]


    @dag(schedule=None, start_date=datetime(2026, 1, 1), catchup=False)
    def df():
        a = Deferring(task_id="a", polls=3, deferrable=True)
        b = Deferring(task_id="b")
        c = FromTrigger(task_id="c")
        a >> b >> c

    df()
'''


def test_deferrable_operators_and_start_from_trigger_operators_run_in_place_as_plain_python(tmp_path):
    irs, reps, pkg = extract_and_convert(tmp_path, DEFER, major=2, pkg="v9_d1")
    ta = next(t for t in irs["df"]["tasks"] if t["task_id"] == "a")
    assert ta["args"].get("deferrable") is True                                                  # the extractor keeps an argument the class stores under another name (`_defer`)
    rep = reps["df"]
    assert rep["status"] == "converted" and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    mod = load_dag(pkg, "df")
    res = run_assets(mod)
    assert res.success, [e.event_specific_data.error.cause.message if e.event_specific_data.error.cause else "" for e in step_failures(res)]
    assert value(res, ["df", "a"]) == "done-a/kw/3"                                            # the trigger ran, execute_complete got the event and the kwargs
    assert value(res, ["df", "b"]) == "sensor-mode"                                            # deferrable not passed: the class's own default (the environment, not deferred)
    assert value(res, ["df", "c"]) == ["done-s", "nk"]                                         # start_from_trigger: no execute, trigger built from start_trigger_args


def test_an_operator_with_no_execute_and_no_start_trigger_args_is_a_loud_stub_that_says_what_airflow_does():
    info = {"name": "Op", "module": "m", "file": __file__, "source": "class Op(BaseOperator):\n    def __init__(self, **kw):\n        super().__init__(**kw)\n"}
    with pytest.raises(C.Defer) as e:
        C.resolve_operator_class(info, set(), {})
    assert "without a execute method" in str(e.value) and "NotImplementedError" in str(e.value)
    info2 = {**info, "source": "class Op(BaseOperator):\n    def execute(self, context):\n        self.xcom_push(context, 'k', 1)\n"}
    with pytest.raises(C.Defer) as e2:
        C.resolve_operator_class(info2, set(), {})
    assert "self.xcom_push" in str(e2.value) and "context['ti']" in str(e2.value)


def test_the_runtime_shims_are_importable_names_of_the_generated_runtime():
    ns: dict = {}
    exec(compile(C.RUNTIME, "runtime", "exec"), ns)       # the RUNTIME text is the generated runtime module
    for name in ("NEVER_EXPIRE", "BaseTrigger", "TriggerEvent", "StartTriggerArgs", "sync_to_async", "conf", "af_lazy_path", "_TaskShim"):
        assert name in ns, name
    assert ns["conf"].getboolean("operators", "default_deferrable", fallback=True) is True
