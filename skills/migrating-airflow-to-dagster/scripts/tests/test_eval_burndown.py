"""Tests for the burn-down fixes of the evaluation harness (duplicate dag ids, symlinked repositories, stdout noise, stub-versus-failure, thin-equivalent reporting) and for the verdict regression guard.
Pure functions and small synthetic fixtures: no Airflow, no Dagster."""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "parity"))
sys.path.insert(0, str(HERE))
import behavior_compare as B  # noqa: E402
import eval_behavior as EB  # noqa: E402
import eval_common as C  # noqa: E402
import eval_report as R  # noqa: E402
import verdict_snapshot as V  # noqa: E402
from test_eval_harness import SPEC, af, route  # noqa: E402


# ---- H1: one dag id in two files -------------------------------------------------------------------------------------------------------------------------
def dup_ir():
    mk = lambda f, r: {"dag_id": "top", "file": f, "default_args": {"retries": r}, "tasks": []}      # noqa: E731
    return {"airflow_version": "3.4.0", "dags": [mk("exercises/top.py", 0), mk("top.py", 3), {"dag_id": "other", "file": "other.py", "tasks": []}]}


def test_pick_ir_dag_takes_the_file_the_manifest_row_names():
    ir = dup_ir()
    assert C.pick_ir_dag(ir["dags"], "top", "top.py")["default_args"]["retries"] == 3
    assert C.pick_ir_dag(ir["dags"], "top", "exercises/top.py")["default_args"]["retries"] == 0
    assert C.pick_ir_dag(ir["dags"], "top")["file"] == "exercises/top.py"                    # no file: the first, as the corpus builder chooses
    assert C.pick_ir_dag(ir["dags"], "nope") is None


def test_dedupe_ir_keeps_one_entry_per_id_and_leaves_clean_irs_alone():
    ir = dup_ir()
    d, dups = C.dedupe_ir(ir, {"top": "top.py"})
    assert dups == ["top"] and [x["file"] for x in d["dags"]] == ["top.py", "other.py"]
    clean = {"dags": [{"dag_id": "a", "file": "a.py"}]}
    assert C.dedupe_ir(clean) == (clean, [])


def test_ir_dag_and_spec_use_the_row_file(tmp_path):
    (tmp_path / "irs").mkdir()
    (tmp_path / "irs" / "r.3.json").write_text(json.dumps(dup_ir()))
    d, ver = C.ir_dag(tmp_path, {"key": "r::top", "repo": "r", "airflow": "3", "dag_id": "top", "file": "top.py"})
    assert d["default_args"]["retries"] == 3 and ver == "3.4.0"


def test_the_converter_is_given_the_ir_of_the_file_airflow_runs(tmp_path, monkeypatch):
    (tmp_path / "irs").mkdir()
    (tmp_path / "irs" / "r.3.json").write_text(json.dumps(dup_ir()))
    manifest = {"dags": [{"key": "r::top", "repo": "r", "dag_id": "top", "file": "exercises/top.py"}, {"key": "r::other", "repo": "r", "dag_id": "other", "file": "other.py"}]}
    seen = {}

    def fake_run(cmd, **kw):
        irp = Path(cmd[2])
        seen["ir"] = json.loads(irp.read_text())
        rep = Path(cmd[cmd.index("--report") + 1])
        rep.write_text(json.dumps({"dags": {"top": {"status": "converted"}}}))
    monkeypatch.setattr(EB.subprocess, "run", fake_run)
    row = {"key": "r::top", "repo": "r", "airflow": "3", "dag_id": "top"}
    src, rep = EB.converter_src(tmp_path, row, tmp_path / "conv", manifest)
    assert [d["file"] for d in seen["ir"]["dags"]] == ["exercises/top.py", "other.py"]
    assert src.parent.name.endswith(".rad") and rep == {"status": "converted"}


# ---- H4: a work directory's repos is a symlink -----------------------------------------------------------------------------------------------------------
def test_local_deps_find_data_files_when_the_repository_root_is_a_symlink(tmp_path):
    real = tmp_path / "real" / "repo"
    (real / "dags").mkdir(parents=True)
    (real / "include").mkdir()
    (real / "include" / "my_log.csv").write_text("a,b\n")
    (real / "dags" / "d.py").write_text('FILE_PATH = "include/my_log.csv"\n')
    work = tmp_path / "work"
    work.mkdir()
    (work / "repos").symlink_to(tmp_path / "real")
    root = work / "repos" / "repo"
    deps = C.local_deps(root, root / "dags" / "d.py")
    assert deps == [root / "include" / "my_log.csv"]
    assert EB.seed_for(root / "dags" / "d.py", root) == {"include/my_log.csv": str(root / "include" / "my_log.csv")}


# ---- H6: printed-output artefacts ------------------------------------------------------------------------------------------------------------------------
def test_logger_level_prefix_and_chained_traceback_text_are_not_printed_output():
    text = "CRITI [airflow.task] This line is red\nplain line\nDuring handling of the above exception, another exception occurred:\nStack Trace:\n   ^^^^^^^^^^^^\n"
    assert B.printed_lines(text) == ["plain line"]


def test_a_long_line_cut_at_a_different_point_is_the_same_line():
    long_ = "{'key0': '" + "x" * 150 + "', 'key1': 'yyy'}"
    assert B.truncated_same(long_, long_[:120]) and B.truncated_same(long_[:110], long_)
    assert not B.truncated_same("short", "short line")                                    # below the minimum length a prefix proves nothing
    a1 = {"stdout_lines": [long_]}
    r = {"stdout_lines": [long_[:120]]}
    assert B.compare_stdout(a1, None, r)["verdict"] == B.MATCH
    assert B.compare_stdout(a1, None, {"stdout_lines": ["something else"]})["verdict"] == B.DIFFER


# ---- H2: the baseline's virtualenv task ------------------------------------------------------------------------------------------------------------------
def test_virtualenv_baseline_failure_names_the_requirements_and_is_environmental():
    spec = {"tasks": [{"task_id": "venv_t", "operator": "BranchPythonVirtualenvOperator", "arguments": {"requirements": ["numpy~=1.26.0"]}}]}
    a1 = af("failed", {"venv_t": {"state": "failed"}}, errors=["airflow.sdk.exceptions.AirflowException: Unable to create new virtual environment in /x"])
    cls, why = B.baseline_class(a1, None, spec)
    assert cls == "baseline-failed" and "virtualenv-build" in why and "numpy~=1.26.0" in why


# ---- stubs are never a successful match ------------------------------------------------------------------------------------------------------------------
TASKS = {"a": {"state": "failed", "xcom": {}}, "b": {"state": "upstream_failed", "xcom": {}}}
TM = {"a": {"mode": "replaced", "asset": "d/a"}, "b": {"mode": "replaced", "asset": "d/b"}}
ST = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"d/a": {"deps": [], "retry": [], "pool": []}, "d/b": {"deps": ["d/a"], "retry": [], "pool": []}}}


def failing_route(error, **kw):
    return route(state="failed", assets={"d/a": {"state": "failed", "errors": [error]}, "d/b": {"state": "not_run"}}, structure=ST, **kw)


def verdict_of(rt, base_errors=("ValueError: by design",)):
    r = B.compare(SPEC, af("failed", TASKS, errors=list(base_errors)), af("failed", TASKS), rt, TM, "converter")
    return r["rollup"]


def test_the_dags_own_failure_matches_and_a_stub_failure_does_not():
    own = verdict_of(failing_route("ValueError: by design"))
    assert own["verdict"] == "matches-baseline-failure" and "detail" not in own and own["cause"]["same"] is True
    stub = verdict_of(failing_route("Failure: AIRFLOW-CAVEAT: bash_command is not a string"))
    assert stub["verdict"] == "matches-baseline-failure" and stub["detail"] == "stub-matches-failure"


def test_a_not_implemented_error_from_the_converter_runtime_is_a_stub_failure_unless_the_baseline_has_one_too():
    msg = "NotImplementedError: xcom_pull of another DAG's return value is not mirrored"
    assert verdict_of(failing_route(msg))["detail"] == "stub-matches-failure"
    assert "detail" not in verdict_of(failing_route(msg), base_errors=["NotImplementedError: the operator has no execute"])


def test_a_failing_asset_the_converter_report_lists_as_stubbed_is_a_stub_failure():
    rt = {**failing_route("ValueError: by design"), "stub_assets": ["d/a"]}
    assert verdict_of(rt)["detail"] == "stub-matches-failure"


def test_failure_cause_tells_a_coincidental_agreement():
    assert B.failure_cause(["KeyError: 'time'"], ["KeyError: 'x'"])["same"] is True
    assert B.failure_cause(["TypeError: NoneType is not iterable"], ["ModuleNotFoundError: No module named 'pyarrow'"])["same"] is False
    assert B.failure_cause(["airflow.sdk.exceptions.AirflowException: Bash command failed"], ["Failure: command exited 1"])["same"] is None
    assert B.failure_cause([], ["KeyError: 'x'"])["same"] is None


def test_a_route_that_lacks_a_module_is_flagged_as_environment():
    rt = failing_route("ModuleNotFoundError: No module named 'pyarrow'")
    r = B.compare(SPEC, af("success", {"a": {"state": "success", "xcom": {}}, "b": {"state": "success", "xcom": {}}}), None, rt, TM, "converter")
    assert r["rollup"]["route_missing_modules"] == ["pyarrow"]
    load_fail = route(state=None, error="defs/resources.py fails to load: No module named 'dagster_snowflake'")
    r = B.compare(SPEC, af("success", {}), None, load_fail, TM, "converter")
    assert r["rollup"]["route_missing_modules"] == ["dagster_snowflake"]


# ---- the report: separate groups, and what a thin strict did not observe ---------------------------------------------------------------------------------
def rec(name, verdict, thin=False, dims=None, rollup=None):
    x = {"rollup": {"verdict": verdict, "why": "w", "thin": thin, **(rollup or {})}, "dimensions": dims or {}}
    return {"key": f"r::{name}", "dag_id": name, "repo": "r", "behavior": {"routes": {"converter": x}, "baseline": {"class": ["ok", "ok"]}}, "baseline_failed": False,
            "verdicts": {"converter": x["rollup"].get("detail") or verdict}}


def test_thin_strict_equivalents_list_what_was_not_observable_and_count_them():
    nc = lambda why: {"verdict": "not-compared", "reason": why}      # noqa: E731
    thin_dims = {"values": nc("no value to compare: t: Airflow recorded no value"), "files": nc("no files observed on either side"), "stdout": nc("the original printed no stable line to compare"), "failure": nc("no task failed"),
                 "http": {"verdict": "match"}}
    recs = [rec("t1", "equivalent", True, thin_dims), rec("t2", "equivalent", True, {"files": nc("no files")}), rec("s1", "equivalent", False, {"values": {"verdict": "match"}})]
    text = "\n".join(R.strict_unobserved_lines(recs, "converter"))
    assert "2 of 3 are thin" in text
    assert "| `t1` | values (t: Airflow recorded no value); files; stdout (the original printed no stable line to compare); failure |" in text
    assert "files 2" in text and "`s1`" not in text


def test_failed_by_design_is_its_own_group_and_stubs_are_a_third():
    recs = [rec("own", "matches-baseline-failure", True, rollup={"cause": {"same": True, "baseline": ["KeyError"], "route": ["KeyError"]}}),
            rec("coincidence", "matches-baseline-failure", False, rollup={"cause": {"same": False, "baseline": ["TypeError"], "route": ["ModuleNotFoundError"]}}),
            rec("stubby", "matches-baseline-failure", True, rollup={"detail": "stub-matches-failure", "why": "stub fails"}), rec("ok", "equivalent")]
    text = "\n".join(R.failed_by_design_lines(recs, "converter"))
    assert "Failed by design in Airflow, the route fails the same way: 2." in text and "same 1, different 1" in text
    assert "| `own` | thin | same |" in text and "| `coincidence` | substantive | different |" in text
    assert "Stub fails the same way: 1" in text and "`stubby`" in text.split("Stub fails the same way")[1] and "`stubby`" not in text.split("Stub fails the same way")[0]


def test_route_environment_section_lists_missing_modules():
    recs = [rec("e1", "different", rollup={"route_missing_modules": ["pyarrow"]}), rec("e2", "equivalent")]
    text = "\n".join(R.route_environment_lines(recs, "converter"))
    assert "install: pyarrow" in text and "`e1`" in text and "`e2`" not in text


# ---- the regression guard --------------------------------------------------------------------------------------------------------------------------------
def write_behavior(work, run, name, verdict, thin=False, detail=None, mtime=None, repo="r"):
    d = Path(work) / f"{run}-private" / "behavior" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "behavior.json"
    ru = {"verdict": verdict, "why": "w", "thin": thin, **({"detail": detail} if detail else {})}
    f.write_text(json.dumps({"key": f"{repo}::{name}", "dag_id": name, "repo": repo, "routes": {"converter": {"rollup": ru, "dimensions": {"files": {"verdict": "not-compared"}}}}, "baseline": {"class": ["baseline-failed" if verdict == "baseline-failed" else "ok", "x"]}}))
    if mtime:
        os.utime(f, (mtime, mtime))


def write_conv(conv, repo, dags):
    d = Path(conv) / f"{repo}.3.ra"
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"dags": dags}))


def run_cli(*args):
    return V.main([str(a) for a in args])


def test_save_takes_the_freshest_behavior_per_dag_across_work_dirs(tmp_path, capsys):
    write_behavior(tmp_path / "w1", "s", "a", "different", mtime=1000)
    write_behavior(tmp_path / "w2", "s", "a", "equivalent", mtime=2000)
    write_behavior(tmp_path / "w1", "s", "b", "equivalent", thin=True)
    write_behavior(tmp_path / "w1", "s", "c", "no-output", thin=None)
    write_behavior(tmp_path / "w1", "s", "d", "matches-baseline-failure", detail="stub-matches-failure")
    write_conv(tmp_path / "conv", "r", {"a": {"status": "converted", "caveats": [{"kind": "stub", "task": "t"}, {"kind": "degraded", "task": "u"}]}, "b": {"status": "converted", "caveats": []}})
    out = tmp_path / "snap.json"
    assert run_cli("save", tmp_path / "w1", tmp_path / "w2", "--conv-dir", tmp_path / "conv", "--out", out) == 0
    s = json.loads(out.read_text())
    assert s["counts"]["dags"] == 4 and s["counts"]["by_verdict"]["stub-matches-failure"] == 1 and s["counts"]["strict_thin"] == 1 and s["counts"]["strict_substantive"] == 1
    a = s["dags"]["r::a"]
    assert a["verdict"] == "equivalent" and a["kind"] == "substantive" and a["converter"] == {"deferred": False, "stubs": ["t"], "degraded": ["u"], "caveats": 2}
    assert s["dags"]["r::b"]["kind"] == "thin" and s["dags"]["r::b"]["unobserved"] == ["files"]
    assert s["dags"]["r::c"]["kind"] == "n/a" and s["dags"]["r::c"]["converter"] is None


def snapshot_of(tmp_path, spec, conv_dags=None):
    for n, (v, thin) in spec.items():
        write_behavior(tmp_path / "base", "s", n, v, thin=thin)
    if conv_dags:
        write_conv(tmp_path / "convbase", "r", conv_dags)
    out = tmp_path / "snap.json"
    run_cli("save", tmp_path / "base", "--conv-dir", *([tmp_path / "convbase"] if conv_dags else []), "--out", out)
    return out


def test_check_reports_regressions_improvements_and_exits_nonzero(tmp_path, capsys):
    snap = snapshot_of(tmp_path, {"same": ("equivalent", False), "worse": ("equivalent", False), "thinner": ("equivalent", False), "better": ("different", False), "env": ("baseline-failed", None), "gone": ("equivalent", True)})
    for n, v, thin in (("same", "equivalent", False), ("worse", "different", False), ("thinner", "equivalent", True), ("better", "equivalent-with-differences", False), ("env", "equivalent", True), ("fresh", "equivalent", True)):
        write_behavior(tmp_path / "now", "s", n, v, thin=thin)
    code = run_cli("check", tmp_path / "now", "--snapshot", snap)
    text = capsys.readouterr().out
    assert code == 1
    assert "r::worse: verdict equivalent -> different" in text and "r::thinner: strict equivalent went from substantive to thin" in text
    assert "r::better: verdict different -> equivalent-with-differences" in text
    assert "r::env: baseline: baseline-failed -> equivalent" in text and "REGRESSIONS: 2" in text
    assert "not measured in this run: 1; new (not in the snapshot): 1; unchanged: 1" in text


def test_check_flags_a_new_stub_or_caveat_even_when_the_verdict_holds(tmp_path, capsys):
    snap = snapshot_of(tmp_path, {"a": ("equivalent", False), "b": ("equivalent", False)}, {"a": {"status": "converted", "caveats": []}, "b": {"status": "converted", "caveats": [{"kind": "stub", "task": "x"}]}})
    write_behavior(tmp_path / "now", "s", "a", "equivalent")
    write_behavior(tmp_path / "now", "s", "b", "equivalent")
    write_conv(tmp_path / "convnow", "r", {"a": {"status": "converted", "caveats": [{"kind": "stub", "task": "t"}]}, "b": {"status": "converted", "caveats": []}})
    code = run_cli("check", tmp_path / "now", "--conv-dir", tmp_path / "convnow", "--snapshot", snap)
    text = capsys.readouterr().out
    assert code == 1 and "r::a: new stub: t; caveats 0 -> 1" in text and "r::b: stub gone: x; caveats 1 -> 0" in text


def test_check_is_clean_when_nothing_got_worse(tmp_path, capsys):
    snap = snapshot_of(tmp_path, {"a": ("equivalent", False)})
    write_behavior(tmp_path / "now", "s", "a", "equivalent")
    assert run_cli("check", tmp_path / "now", "--snapshot", snap) == 0
    assert "OK: no regressions" in capsys.readouterr().out


def test_the_shipped_snapshot_is_well_formed():
    s = json.loads(V.DEFAULT_SNAPSHOT.read_text())
    assert s["counts"]["dags"] == len(s["dags"]) and sum(s["counts"]["by_verdict"].values()) == len(s["dags"])
    assert all(e["verdict"] in list(V.RANK) + [V.UNRANKED] for e in s["dags"].values())


# ---- pools and retries are attributed per task (op), not aggregated over a graph asset ------------------------------------------------------------------
POOLED_GRAPH = '''
import dagster as dg


@dg.op(name="d__a", pool="shared")
def op_a():
    return 1


@dg.op(name="d__b", retry_policy=dg.RetryPolicy(max_retries=3, delay=7))
def op_b(x):
    return x


@dg.op(name="d__c", pool="d__c")
def op_c(x):
    return x


@dg.graph(out={"o_a": dg.GraphOut(), "o_b": dg.GraphOut(), "o_c": dg.GraphOut()})
def d_body():
    a = op_a()
    return {"o_a": a, "o_b": op_b(a), "o_c": op_c(a)}


d_graph = dg.AssetsDefinition.from_graph(
    d_body,
    keys_by_output_name={"o_a": dg.AssetKey(["d", "a"]), "o_b": dg.AssetKey(["d", "b"]), "o_c": dg.AssetKey(["d", "c"])},
    internal_asset_deps={"o_a": set(), "o_b": {dg.AssetKey(["d", "a"])}, "o_c": {dg.AssetKey(["d", "a"])}},
    metadata_by_output_name={n: {"airflow_dag": "d", "airflow_task": t} for n, t in (("o_a", "a"), ("o_b", "b"), ("o_c", "c"))},
)
'''


def test_pool_and_retry_are_attributed_to_the_task_that_declares_them_not_the_whole_graph(tmp_path):
    from test_eval_harness import _run_converter_module
    r = _run_converter_module(tmp_path, POOLED_GRAPH)
    assert r["error"] is None, r
    a = r["structure"]["assets"]
    assert a["d/a"]["pool"] == ["shared"] and a["d/b"]["pool"] == [] and a["d/c"]["pool"] == ["d__c"]
    assert a["d/b"]["retry"] == [{"max_retries": 3, "delay_s": 7, "backoff": None}] and a["d/a"]["retry"] == [] and a["d/c"]["retry"] == []


def test_a_max_active_tis_per_dag_pool_is_not_a_pool_difference_but_a_wrong_or_extra_pool_is():
    spec = {"dag_id": "d", "schedule": {"kind": "manual"}, "tasks": [
        {"task_id": "a", "upstream": [], "downstream": [], "retries": 0, "pool": "shared"},
        {"task_id": "b", "upstream": [], "downstream": [], "retries": 0, "pool": None},
        {"task_id": "c", "upstream": [], "downstream": [], "retries": 0, "pool": None}]}
    tm = {t: {"mode": "replaced", "asset": f"d/{t}"} for t in "abc"}

    def pools(**p):
        st = {"jobs": [], "schedules": [], "sensors": [], "assets": {f"d/{t}": {"deps": [], "retry": [], "pool": p.get(t, [])} for t in "abc"}}
        return B.metadata_dims(spec, route(structure=st), tm, "converter")["pools"]["verdict"]
    ok = pools(a=["shared"], c=["d__c"])
    bad = pools(a=["shared"], b=["shared"])
    assert ok == B.MATCH and bad == B.DIFFER
