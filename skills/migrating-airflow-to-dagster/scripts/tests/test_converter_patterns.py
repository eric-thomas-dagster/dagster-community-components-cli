"""The deterministic converter against the verified pattern library (reference/rewrite-patterns/), so the two routes cannot drift.

Two kinds of test:
  * divergence tests: fail when the converter's emitted form or tables differ from the library's for the same feature;
  * execution tests: convert a small IR, import the output and run it on the installed Dagster, asserting run status and which steps ran or were skipped.
Needs `dagster`; skips without it.
"""
import importlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import convert_ir as C  # noqa: E402
import pattern_emit as PE  # noqa: E402
from test_convert_ir import dag, fn, task  # noqa: E402

PAT = HERE.parents[1] / "reference" / "rewrite-patterns"


def lib(name):
    spec = importlib.util.spec_from_file_location(f"conv_pattern_{name}", PAT / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def convert_and_load(tmp_path, tasks, pkg, kind="once", *extra):
    ir = {"airflow_version": "x", "dags": [dag(tasks, kind=kind)]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"),
                    "--report", str(tmp_path / "r.json"), *extra], check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["d1"]
    (tmp_path / pkg / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    assert rep["status"] == "converted", rep
    return rep, importlib.import_module(f"{pkg}.defs.d1"), tmp_path / pkg


def runtime_module(tmp_path):
    p = tmp_path / "rt_check.py"
    p.write_text(C.RUNTIME)
    spec = importlib.util.spec_from_file_location("rt_check", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def states(res):
    return {e.step_key: e.event_type_value for e in res.all_events if e.event_type_value in ("STEP_SUCCESS", "STEP_SKIPPED", "STEP_FAILURE")}


# ---- divergence tests --------------------------------------------------------------------------------------------------------------------------

def test_library_claims_the_converter_relies_on_are_verified():
    assert PE.status("depends_on_past", "STEP_FAILURE") == "VERIFIED"
    assert PE.status("pools", "pool= exists on asset") == "VERIFIED"
    assert PE.status("venv_pipes", "missing interpreter fails the run loudly") == "VERIFIED"
    assert PE.status("xcom", "value flows as asset argument") == "VERIFIED"
    assert C.DOP_GUARD is True


def test_runtime_embeds_the_librarys_depends_on_past_source_verbatim():
    assert PE.pull("depends_on_past", ["_latest_id", "previous_run_failed"]) in C.RUNTIME


def test_template_context_and_macro_tables_cover_the_librarys():
    assert {"ds", "ds_nodash", "ts", "logical_date", "data_interval_start", "data_interval_end"} <= C.SUPPORTED_CTX
    assert {"ds_add", "ds_format"} <= C.SUPPORTED_MACROS


def test_ds_add_agrees_with_the_librarys_interval_arithmetic(tmp_path):
    rt = runtime_module(tmp_path)
    m = lib("templating_config")
    start = m.dt.datetime(2024, 3, 1, tzinfo=m.dt.timezone.utc)
    assert rt._ds_add("2024-03-01", 7) == (start + m.dt.timedelta(days=7)).strftime("%Y-%m-%d")


def test_skipped_state_metadata_is_the_librarys(tmp_path):
    rt = runtime_module(tmp_path)
    seen = {}
    ctx = types.SimpleNamespace(add_output_metadata=lambda md: seen.update(md))
    rt.af_skip(ctx, False)
    lib_md = lib("skip").skipped_output("x").metadata["airflow/state"]
    assert seen == {"airflow/state": lib_md.value} and lib_md.value == "skipped"       # same key, same value as the library's skipped_output


def test_poke_and_poll_until_agree_on_outcomes(tmp_path, monkeypatch):
    rt = runtime_module(tmp_path)
    sensors = lib("sensors")
    clock = {"t": 0.0}
    monkeypatch.setattr(rt.__dict__.get("time", None) or __import__("time"), "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(__import__("time"), "monotonic", lambda: clock["t"])
    ctx = types.SimpleNamespace(add_output_metadata=lambda md: None)
    assert rt.af_poke(ctx, lambda: True, 1, 5) is None and sensors.poll_until(lambda: True, 5, 1, sleep=lambda s: None, clock=lambda: 0.0)
    with pytest.raises(dg.Failure):
        rt.af_poke(ctx, lambda: False, 1, 5)
    assert not sensors.poll_until(lambda: False, 5, 1, sleep=lambda s: clock.__setitem__("t", clock["t"] + s), clock=lambda: clock["t"])
    assert isinstance(rt.af_poke(ctx, lambda: False, 1, 5, soft_fail=True), rt.Skipped)     # soft_fail: skipped, as the library's metadata form


def test_partition_window_drives_ds_when_no_schedule_tags(tmp_path):
    rt = runtime_module(tmp_path)
    import datetime as dt
    win = types.SimpleNamespace(start=dt.datetime(2024, 3, 1, tzinfo=dt.timezone.utc), end=dt.datetime(2024, 3, 2, tzinfo=dt.timezone.utc))
    ctx = types.SimpleNamespace(run=types.SimpleNamespace(tags={}, run_id="r"), has_partition_key=True, partition_key="2024-03-01", partition_time_window=win,
                                retry_number=0, instance=None)
    out = rt.af_context(ctx, "d1", "t", {}, {})
    m = lib("templating_config")
    tw = win                                                       # the library's interval_asset reads exactly these
    assert out["ds"] == "2024-03-01" == ctx.partition_key
    assert out["data_interval_start"] == rt._iso(tw.start) and out["data_interval_end"] == rt._iso(tw.end)
    assert m.interval_asset is not None


def test_pool_is_first_class_on_the_asset_and_on_graph_ops_not_the_graph_asset():
    a = task("a", "BashOperator", template_fields={"bash_command": "true"}, pool="launch_pad")
    code, s = C.convert_dag(dag([a], kind="once"), "n", {})
    assert "pool='launch_pad'" in code and "op_tags" not in code and "dagster/concurrency_key" not in code
    g = task("g", "BashOperator", template_fields={"bash_command": "true"}, pool="launch_pad", trigger_rule="all_done")
    gb = task("b", "_ShortCircuitDecoratedOperator", callable=fn("numbers"), op_args=[], op_kwargs={}, up=[])
    code, s = C.convert_dag(dag([gb, {**g, "upstream": ["b"]}], kind="once"), "n", {})
    head = code.split("@dg.graph(")[1].split("def ")[0]      # updated: the graph asset is now @dg.graph + from_graph (it declares internal deps)
    assert "pool=" not in head and "pool='launch_pad'" in code.split("@dg.graph(")[0]


# ---- execution tests ---------------------------------------------------------------------------------------------------------------------------

def test_depends_on_past_blocks_after_a_failed_previous_run_executed(tmp_path, monkeypatch):
    a = task("a", "PythonOperator", callable=fn("flaky_step"), op_args=[], op_kwargs={}, depends_on_past=True)
    monkeypatch.setattr(C, "CAVEATS", "raise")
    rep, mod, _ = convert_and_load(tmp_path, [a], "cp_dop", "once")
    assert [(c["kind"], c["task"]) for c in rep["caveats"]] == [("degraded", "a")] and "fail" in rep["caveats"][0]["reason"]
    with dg.DagsterInstance.ephemeral() as inst:
        assert dg.materialize([mod.t_a], instance=inst).success                       # first run: no predecessor
        monkeypatch.setenv("DOP_FAIL", "1")
        assert not dg.materialize([mod.t_a], instance=inst, raise_on_error=False).success
        monkeypatch.delenv("DOP_FAIL")
        r = dg.materialize([mod.t_a], instance=inst, raise_on_error=False)             # the previous run failed: blocked
        assert not r.success
        assert any("depends_on_past" in str(e.event_specific_data.error.message) + str(e.event_specific_data.error.cause)
                   for e in r.all_events if e.event_type_value == "STEP_FAILURE")


def test_venv_missing_interpreter_fails_loudly_and_never_runs_in_the_current_one(tmp_path):
    rt = runtime_module(tmp_path)

    def work():
        return "ran in " + __import__("sys").executable
    run = rt.af_isolated(work, python=str(tmp_path / "no" / "such" / "python"))
    with pytest.raises(dg.Failure, match="not falling back"):
        run()


def test_venv_existing_interpreter_runs_the_callable(tmp_path):
    rt = runtime_module(tmp_path)

    def work():
        return 41 + 1
    assert rt.af_isolated(work, python=sys.executable)() == 42


def test_mapped_empty_expansion_skips_the_step_below_executed(tmp_path):
    e = task("e", "PythonOperator", is_mapped=True, callable={**fn("double_item"), "params": ["x"]}, op_args=[], op_kwargs={},
             expand={"form": "expand", "args": {"x": {"kind": "literal", "value": []}}, "fixed": {}, "fixed_unserializable": [], "op_args": False})
    after = task("after", "PythonOperator", up=["e"], callable=fn("to_str"), op_args=[{"xcom_from": "e", "key": "return_value"}], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [e, after], "cp_empty")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    assert res.success
    meta = {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}
    assert meta["d1/e"] == "skipped" and meta["d1/after"] == "skipped"                # Airflow: the mapped task and its downstream are skipped
    st = states(res)
    assert not any(v == "STEP_FAILURE" for v in st.values())


def test_short_circuit_skips_downstream_and_the_run_succeeds_executed(tmp_path):
    sc = task("sc", "_ShortCircuitDecoratedOperator", callable=fn("falsy"), op_args=[], op_kwargs={})
    nxt = task("nxt", "PythonOperator", up=["sc"], callable=fn("numbers"), op_args=[], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [sc, nxt], "cp_sc")
    res = dg.materialize([mod.d1_graph], raise_on_error=False)
    assert res.success
    meta = {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}
    assert meta.get("d1/nxt") == "skipped" and meta.get("d1/sc") != "skipped"          # the short-circuit task succeeded, its downstream is skipped


def test_xcom_keyed_push_is_metadata_and_a_return_value_is_an_argument_executed(tmp_path):
    p = task("p", "PythonOperator", callable=fn("pusher"), op_args=[], op_kwargs={})
    rep, mod, _ = convert_and_load(tmp_path, [p], "cp_xc")
    res = dg.materialize([mod.t_p])
    ev = res.get_asset_materialization_events()[0].materialization.metadata
    assert ev["count"].value == 3 and ev["detail"].value == {"a": [1, 2]}              # xcom.py's add_output_metadata form
