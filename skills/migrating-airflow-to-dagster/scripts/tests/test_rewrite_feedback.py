"""The worker-side behaviour feedback and the new gates: `compare_behavior.py` in a self-contained kit, the task-state gate (names the differing task; a folded not-taken branch is accepted only when declared),
the decision-record gate, the pattern detection and prompt wiring. Needs dagster; the Airflow baselines are synthetic results in the format `run_airflow.py` writes (no Airflow needed)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("dagster")
HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "parity"))
import accept_rewrite as A  # noqa: E402
import behavior_compare as B  # noqa: E402
import compare_behavior as CB  # noqa: E402
import eval_kit as K  # noqa: E402
import rewrite_patterns as RP  # noqa: E402
import rewrite_prompt as RPR  # noqa: E402

BRANCH_SPEC = {"dag_id": "br", "connections": [], "variables": [], "schedule": {"kind": "manual", "cron": None, "timezone": None, "assets": None},
               "tasks": [{"task_id": "decide", "operator": "BranchPythonOperator", "upstream": [], "downstream": ["a", "b"], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []},
                         {"task_id": "a", "operator": "PythonOperator", "upstream": ["decide"], "downstream": [], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []},
                         {"task_id": "b", "operator": "PythonOperator", "upstream": ["decide"], "downstream": [], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []}]}

GOOD = '''"""decide: folded into `br_asset`
a: folded into `br_asset`
b: folded into `br_asset`: branch not taken (Airflow marks it skipped; here it is not executed)

{record}"""
import dagster as dg
from pathlib import Path


@dg.asset
def br_asset() -> None:
    Path("a.txt").write_text("a")
    {extra}


br = dg.define_asset_job("br", selection=[br_asset])
defs = dg.Definitions(assets=[br_asset], jobs=[br])
'''
RECORD = "Decision record:\n- skip/branch: not preserved: Airflow's skipped state of b; Dagster has no state for code that is not run, effects are preserved\n"


def project(tmp, body):
    p = tmp / "proj"
    d = p / "src" / "rw_br" / "defs"
    d.mkdir(parents=True)
    (p / "src" / "rw_br" / "__init__.py").write_text("")
    (d / "__init__.py").write_text("")
    (d / "br.py").write_text(body)
    return p


def baseline(tmp, project_dir, states=None):
    """an Airflow-format result whose effects are those of the correct project, with the Airflow task states of a branch run"""
    ok = CB.run_rewrite(project_dir, BRANCH_SPEC, "br", None, python=sys.executable)
    assert ok["state"] == "success", ok
    a = {"dag_id": "br", "state": "success", "tasks": {t: {"state": s, "xcom": {}} for t, s in (states or {"decide": "success", "a": "success", "b": "skipped"}).items()}, "observed": ok["observed"], "errors": [], "error": None}
    bd = tmp / "baseline"
    bd.mkdir()
    (bd / "airflow1.json").write_text(json.dumps(a))
    (bd / "airflow2.json").write_text(json.dumps(a))
    return bd


def test_state_gate_accepts_a_declared_folded_not_taken_branch_and_names_the_task_otherwise(tmp_path):
    p = project(tmp_path, GOOD.format(record=RECORD, extra="pass"))
    bd = baseline(tmp_path, p)
    src = (p / "src/rw_br/defs/br.py").read_text()
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, src)
    assert g["passed"], g["detail"]
    assert "accepted" in g["detail"]
    undeclared = GOOD.format(record="", extra="pass")
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, undeclared)
    assert not g["passed"] and "`b`" in g["detail"] and "decision record" in g["detail"]


def test_state_gate_flags_an_unchosen_branch_that_ran_and_a_wrong_state_names_the_task(tmp_path):
    p = project(tmp_path, GOOD.format(record=RECORD, extra="pass"))
    bd = baseline(tmp_path, p)
    # the rewrite also does the branch that was not taken: its side effect (a file) appears
    bad = GOOD.format(record=RECORD, extra='Path("b.txt").write_text("b")')
    (p / "src/rw_br/defs/br.py").write_text(bad)
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, bad)
    assert not g["passed"] and "files" in g["detail"] and "b.txt" in g["detail"]
    # Airflow says task a failed but the rewrite succeeds: the gate names `a`
    p2 = project(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, GOOD.format(record=RECORD, extra="pass"))
    a1 = json.loads((bd / "airflow1.json").read_text())
    a1["tasks"]["a"]["state"] = "failed"
    a1["state"] = "failed"
    (bd / "airflow1.json").write_text(json.dumps(a1))
    (bd / "airflow2.json").write_text(json.dumps(a1))
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p2, bd, None, GOOD.format(record=RECORD, extra="pass"))
    assert not g["passed"] and "task `a`: Airflow failed, rewrite success" in g["detail"]


def test_state_gate_without_a_baseline_is_not_run_and_does_not_fail(tmp_path):
    g = A.g_states(BRANCH_SPEC, "m", tmp_path, None, None, "")
    assert g["passed"] and g["detail"].startswith("not run")


def test_decision_record_gate_requires_every_flagged_behaviour_with_a_reason():
    spec = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "retries": 2}, *BRANCH_SPEC["tasks"][1:]]}
    assert RP.flagged(spec) == ["skip/branch", "retries"]
    ok, detail, _ = RP.check_record(spec, "tasks.\n")
    assert not ok and "no `Decision record:` section" in detail and "skip/branch" in detail
    doc = "x\n\nDecision record:\n- skip/branch: preserved\n- retries: not preserved\n"
    ok, detail, _ = RP.check_record(spec, doc)
    assert not ok and "without a reason" in detail
    doc = "x\n\nDecision record:\n- Skip / branch decisions: preserved\n- retries: not preserved: one RetryPolicy for the whole asset\n"
    ok, detail, declared = RP.check_record(spec, doc)
    assert ok and declared["not_preserved"] == {"retries": "one RetryPolicy for the whole asset"} and declared["preserved"] == ["skip/branch"]
    g = A.g_decision(spec, '"""' + doc + '"""\n')
    assert g["passed"] and g["declared"]["not_preserved"]
    assert RP.check_record({**BRANCH_SPEC, "tasks": [BRANCH_SPEC["tasks"][1]]}, "")[0]       # nothing flagged: no record needed


def test_pattern_detection_and_prompt_wiring():
    spec = {"dag_id": "d", "schedule": {}, "converter": None, "tasks": [
        {"task_id": "s", "operator": "ShortCircuitOperator", "trigger_rule": "all_success"},
        {"task_id": "t", "operator": "PythonOperator", "trigger_rule": "all_done", "depends_on_past": True},
        {"task_id": "m", "operator": "PythonOperator", "is_mapped": True},
        {"task_id": "w", "operator": "HttpSensor", "sensor": {"timeout": 5}}]}
    assert RP.detect(spec) == ["short_circuit", "trigger_rules", "mapped_tasks", "sensors", "depends_on_past"]
    more = {"dag_id": "d", "schedule": {"assets": ["x"]}, "tasks": [{"task_id": "p", "operator": "PythonOperator", "pool": "launch_pad", "group": "g", "reads_value_from": ["q"]},
                                                              {"task_id": "t", "operator": "TriggerDagRunOperator"}]}
    assert RP.detect(more) == ["cross_dag", "pools", "xcom", "task_groups"]
    assert set(RP.flagged(more)) >= {"pools", "cross-DAG triggers", "xcom", "task groups"}
    plain = {"dag_id": "d", "schedule": {}, "converter": None, "tasks": [{"task_id": "t", "operator": "PythonOperator", "trigger_rule": "all_success"}]}
    p = RPR.render(plain, "spec", None, "p", "p")
    assert "Patterns this DAG needs" not in p and "native Dagster forms first" in p and "Decision record:" in p
    q = RPR.render(spec, "spec", None, "p", "p")
    assert "Patterns this DAG needs" in q and "short_circuit.py (tested)" in q and "skip.py (tested)" in q and "poll_until" in q and "mapped_in_python" in q and "nine" in q
    assert "can_subset" in q and "task states" in q and "fallback to plain Python" in q
    # a skipped state may not be dropped silently
    assert "never silently dropped" in q


def test_folded_skip_is_accepted_only_for_a_rewrite_and_a_replaced_task_still_differs():
    spec = {**BRANCH_SPEC}
    a1 = {"state": "success", "tasks": {t: {"state": s} for t, s in (("decide", "success"), ("a", "success"), ("b", "skipped"))}, "observed": {}}
    rt = {"state": "success", "assets": {"x": {"state": "success", "value": None}}, "structure": {"jobs": ["br"], "schedules": [], "sensors": [], "assets": {"x": {"deps": []}}}, "observed": {}}
    folded = {t: {"mode": "folded", "asset": "x"} for t in ("decide", "a", "b")}
    r = B.compare(spec, a1, a1, rt, folded, "rewrite")
    assert r["dimensions"]["tasks"]["verdict"] == "match" and r["dimensions"]["tasks"]["evidence"]["accepted_representation"][0]["task"] == "b"
    replaced = {**folded, "b": {"mode": "replaced", "asset": "x"}}
    assert B.compare(spec, a1, a1, rt, replaced, "rewrite")["dimensions"]["tasks"]["verdict"] == "differ"
    assert B.compare(spec, a1, a1, rt, folded, "converter")["dimensions"]["tasks"]["verdict"] == "differ"


def test_header_parse_prefers_the_tasks_own_line():
    doc = 'should_run: replaced by asset `should_run` (returns "empty_task_1" or "empty_task_2")\nempty_task_1: replaced by asset `t1`\nempty_task_2: replaced by asset `t2`\n'
    m = B.parse_header(doc, ["should_run", "empty_task_1", "empty_task_2"], ["should_run", "t1", "t2"], {})
    assert m["empty_task_2"] == {"mode": "replaced", "asset": "t2"} and m["should_run"]["asset"] == "should_run"


# ---- the kit is self-contained ------------------------------------------------------------------------------------------------------------------
def make_work(tmp):
    FX = HERE / "fixtures_behavior"
    ir = json.loads((FX / "ir.json").read_text())
    w = tmp / "work"
    repo = w / "repos" / "fixrepo"
    (repo / "dags").mkdir(parents=True)
    for f in (FX / "dags").glob("*.py"):
        (repo / "dags" / f.name).write_text(f.read_text())
    (w / "irs").mkdir()
    (w / "irs" / "fixrepo.2.json").write_text((FX / "ir.json").read_text())
    rows = [{"key": f"fixrepo::{d['dag_id']}", "repo": "fixrepo", "dag_id": d["dag_id"], "file": d["file"].replace("dags/", "", 1), "airflow": "2", "eligible": True, "converter": "clean", "pattern": "python", "patterns": ["python"]} for d in ir["dags"]]
    (w / "manifest.json").write_text(json.dumps({"repos": {"fixrepo": {"dag_folders": ["dags"]}}, "dags": rows}))
    (w / "sample.json").write_text(json.dumps({"seed": 1, "label": "t", "dags": [{"key": r["key"], "dag_id": r["dag_id"], "repo": "fixrepo", "stratum": "clean", "pattern": "python"} for r in rows]}))
    for r in rows:                                                             # an earlier run's raw Airflow results (synthetic, Airflow's format)
        bd = w / "base-private" / "behavior" / r["dag_id"]
        bd.mkdir(parents=True)
        for f in ("airflow1.json", "airflow2.json"):
            (bd / f).write_text(json.dumps({"dag_id": r["dag_id"], "state": "success", "tasks": {}, "observed": {"files": {}, "sqlite": {}, "http": [], "smtp": [], "s3": {}}, "errors": [], "error": None, "airflow_version": "2.10.0"}))
    return w


def test_kit_with_baseline_is_self_contained_and_the_tool_runs_from_it_alone(tmp_path):
    w = make_work(tmp_path)
    a = argparse.Namespace(work=w, manifest=w / "manifest.json", sample=w / "sample.json", run="kit", dcc_skill="/example/skills/dagster-community-components", baseline_from="base")
    assert K.make(a) == 0
    d = w / "kit" / "fixture_pipeline"
    tools = {p.name for p in (d / "tools").iterdir() if p.is_file()}
    assert {"compare_behavior.py", "behavior_compare.py", "harness_cfg.py", "rewrite_patterns.py", "accept_rewrite.py", "run_dagster.py"} <= tools
    assert (d / "tools" / "baseline" / "airflow1.json").exists()
    index = json.loads((w / "kit-private" / "kit_index.json").read_text())
    assert K.check_all(w, "kit", index, a.dcc_skill) == []
    # no other DAG's data and no converter output reached the baseline
    for f in (d / "tools" / "baseline").glob("*.json"):
        assert json.loads(f.read_text())["dag_id"] == "fixture_pipeline"
    # the tool runs with nothing but the kit on the path (-I: no PYTHONPATH, no script directory on sys.path), against a one-asset project
    proj = d / "out" / "rw_fixture_pipeline"
    defs = proj / "src" / "rw_fixture_pipeline" / "defs"
    defs.mkdir(parents=True)
    (defs / "fixture_pipeline.py").write_text('"""t: folded into `x`"""\nimport dagster as dg\n\n\n@dg.asset\ndef x() -> None:\n    pass\n\n\nj = dg.define_asset_job("fixture_pipeline", selection=[x])\ndefs = dg.Definitions(assets=[x], jobs=[j])\n')
    spec = json.loads((d / "spec.json").read_text())
    r = subprocess.run([sys.executable, "-I", "tools/compare_behavior.py", "--project", str(proj), "--spec", "spec.json", "--source", "source"], cwd=d, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert "VERDICT:" in r.stdout and "Airflow baseline:" in r.stdout and "DIFFERENT" in r.stdout or "match" in r.stdout, r.stdout + r.stderr
    assert r.returncode in (0, 1), r.stdout + r.stderr
    # no baseline: exit 2 with a plain message
    shutil.rmtree(d / "tools" / "baseline")
    r = subprocess.run([sys.executable, "-I", "tools/compare_behavior.py", "--project", str(proj), "--spec", "spec.json"], cwd=d, capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert r.returncode == 2 and "no Airflow baseline" in r.stdout
    assert spec["dag_id"] == "fixture_pipeline"


def test_kit_check_fails_when_a_tool_imports_something_that_is_not_in_the_kit_or_the_baseline_is_another_dags(tmp_path):
    w = make_work(tmp_path)
    a = argparse.Namespace(work=w, manifest=w / "manifest.json", sample=w / "sample.json", run="kit", dcc_skill="/example/skills/dagster-community-components", baseline_from="base")
    assert K.make(a) == 0
    d = w / "kit" / "fixture_pipeline"
    index = json.loads((w / "kit-private" / "kit_index.json").read_text())
    (d / "tools" / "harness_cfg.py").unlink()
    assert any("harness_cfg" in p for p in K.check_all(w, "kit", index, a.dcc_skill))
    shutil.copy2(SCRIPTS / "parity" / "harness_cfg.py", d / "tools" / "harness_cfg.py")
    other = json.loads((d / "tools" / "baseline" / "airflow1.json").read_text())
    other["dag_id"] = "fixture_fails"
    (d / "tools" / "baseline" / "airflow1.json").write_text(json.dumps(other))
    assert any("another DAG's result" in p for p in K.check_all(w, "kit", index, a.dcc_skill))


def test_compare_render_names_the_differing_task_and_dimension():
    spec = {**BRANCH_SPEC}
    a1 = {"state": "success", "tasks": {t: {"state": s} for t, s in (("decide", "success"), ("a", "success"), ("b", "success"))}, "observed": {}}
    rt = {"state": "success", "assets": {"x": {"state": "failed"}}, "structure": {"jobs": ["br"], "schedules": [], "sensors": [], "assets": {"x": {"deps": []}}}, "observed": {}}
    m = {t: {"mode": "replaced", "asset": "x"} for t in ("decide", "a", "b")}
    res = B.compare(spec, a1, a1, rt, m, "rewrite")
    res["route_error"] = None
    out = CB.render(res)
    assert "[DIFFERENT   ] tasks" in out and "task `a`: Airflow success, yours failed" in out and "VERDICT: different" in out


OPTIONAL = '''"""decide: replaced by asset `decide`
a: replaced by asset `asset_a`
b: replaced by asset `asset_b`
"""
import dagster as dg


@dg.asset
def decide() -> list[str]:
    return ["a"]


@dg.multi_asset(outs={"asset_a": dg.AssetOut(is_required=False), "asset_b": dg.AssetOut(is_required=False)}, ins={"decide": dg.AssetIn("decide")}, name="branches")
def branches(decide: list[str]):
    if "a" in decide:
        yield dg.Output("va", output_name="asset_a")
    if "b" in decide:
        yield dg.Output("vb", output_name="asset_b")


br = dg.define_asset_job("br", selection="*")
defs = dg.Definitions(assets=[decide, branches], jobs=[br])
'''


def test_state_gate_compares_skipped_states_when_the_rewrite_uses_optional_outputs(tmp_path):
    p = project(tmp_path, OPTIONAL)
    a = {"dag_id": "br", "state": "success", "tasks": {"decide": {"state": "success"}, "a": {"state": "success"}, "b": {"state": "skipped"}}, "observed": CB.run_rewrite(p, BRANCH_SPEC, "br", None, python=sys.executable)["observed"], "errors": [], "error": None}
    bd = tmp_path / "bl"
    bd.mkdir()
    for f in ("airflow1.json", "airflow2.json"):
        (bd / f).write_text(json.dumps(a))
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, OPTIONAL)
    assert g["passed"], g["detail"]                                            # b: Airflow skipped, asset_b not materialized -> skipped: a real state match, no declaration needed
    a["tasks"]["b"]["state"] = "success"                                       # Airflow says b succeeded: the rewrite skipped it
    for f in ("airflow1.json", "airflow2.json"):
        (bd / f).write_text(json.dumps(a))
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, OPTIONAL)
    assert not g["passed"] and "task `b`: Airflow success, rewrite skipped" in g["detail"]


def test_decision_record_fallback_verdict_needs_a_reason_and_is_counted():
    spec = {**BRANCH_SPEC}
    ok, detail, declared = RP.check_record(spec, "x\n\nDecision record:\n- skip/branch: fallback to plain Python: the branches produce the same output\n")
    assert ok and declared["fallback"] == {"skip/branch": "the branches produce the same output"} and declared["not_preserved"] == {} and "1 fallback" in detail
    ok, detail, _ = RP.check_record(spec, "x\n\nDecision record:\n- skip/branch: fallback to plain Python\n")
    assert not ok and "without a reason" in detail


def test_state_gate_accepts_folded_skips_only_as_a_declared_fallback(tmp_path):
    p = project(tmp_path, GOOD.format(record=RECORD, extra="pass"))
    bd = baseline(tmp_path, p)
    plain_ok = GOOD.format(record="Decision record:\n- skip/branch: fallback to plain Python: both branches yield the same output\n", extra="pass")
    assert A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, plain_ok)["passed"]
    claims_preserved = GOOD.format(record="Decision record:\n- skip/branch: preserved\n", extra="pass")
    g = A.g_states(BRANCH_SPEC, "rw_br.defs.br", p, bd, None, claims_preserved)
    assert not g["passed"] and "native optional-output form" in g["detail"] and "`b`" in g["detail"]


def test_report_counts_fallbacks_and_declarations(tmp_path):
    import eval_report as R
    recs = [{"gates": {"passed": True, "gates": [{"gate": "decision_record", "passed": True, "detail": "", "declared": {"flagged": ["skip/branch"], "preserved": [], "not_preserved": {"retries": "one policy"}, "fallback": {"skip/branch": "same output"}}}]},
             "verdicts": {}, "baseline_failed": False}]
    assert hasattr(R, "main")                                                    # the section is generated from gates[...]["declared"]; its text is exercised in the end-to-end report test
    assert recs[0]["gates"]["gates"][0]["declared"]["fallback"]


def test_baseline_that_failed_on_a_virtualenv_task_is_baseline_failed_not_by_design():
    spec = {"dag_id": "v", "tasks": [{"task_id": "decide", "operator": "BranchPythonOperator"}, {"task_id": "venv_task", "operator": "_PythonVirtualenvDecoratedOperator"}, {"task_id": "ok", "operator": "PythonOperator"}]}
    a1 = {"state": "failed", "tasks": {"decide": {"state": "success"}, "venv_task": {"state": "failed"}, "ok": {"state": "upstream_failed"}}, "errors": [], "error": None}
    cls, why = B.baseline_class(a1, a1, spec)
    assert cls == "baseline-failed" and "virtualenv" in why
    spec2 = {**spec, "tasks": [t for t in spec["tasks"] if t["task_id"] != "venv_task"] + [{"task_id": "venv_task", "operator": "PythonOperator"}]}
    assert B.baseline_class(a1, a1, spec2)[0] == "by-design-failure"                           # an ordinary task that fails is still the DAG's own failure
    # and the roll-up excludes it from the shares: a rewrite that "succeeds" is not counted as different
    rt = {"state": "success", "assets": {}, "structure": {"jobs": ["v"], "schedules": [], "sensors": [], "assets": {}}, "observed": {}}
    res = B.compare(spec, a1, a1, rt, {}, "rewrite")
    assert res["rollup"]["verdict"] == "baseline-failed"


# ---- generated_tests gate: a DAG that fails in Airflow must fail in the rewrite ------------------------------------------------------------------
FAILING = '''"""decide: replaced by asset `x`
a: folded into `x`
b: folded into `x`
"""
import dagster as dg


@dg.asset
def x() -> None:
    raise dg.Failure("fails loudly, as the DAG does")


br = dg.define_asset_job("br", selection=[x])
defs = dg.Definitions(assets=[x], jobs=[br])
'''
SUCCEEDING = FAILING.replace('    raise dg.Failure("fails loudly, as the DAG does")', "    pass")


def baseline_dir(tmp, a1):
    bd = tmp / "bl_fail"
    bd.mkdir()
    for f in ("airflow1.json", "airflow2.json"):
        (bd / f).write_text(json.dumps(a1))
    return bd


def test_generated_test_asserts_failure_when_airflow_fails_by_design(tmp_path):
    a1 = {"dag_id": "br", "state": "failed", "tasks": {"decide": {"state": "success"}, "a": {"state": "failed"}, "b": {"state": "upstream_failed"}}, "observed": {}, "errors": [], "error": None}
    bd = baseline_dir(tmp_path, a1)
    p = project(tmp_path, FAILING)
    g = A.g_tests(BRANCH_SPEC, "rw_br.defs.br", p, bd)
    assert g["passed"] and "EXPECTED FAILURE asserted instead of success" in g["detail"] and "failed task(s) ['a']" in g["detail"], g["detail"]
    ok_project = project(tmp_path / "ok" if (tmp_path / "ok").mkdir() is None else tmp_path, SUCCEEDING)
    g = A.g_tests(BRANCH_SPEC, "rw_br.defs.br", ok_project, bd)
    assert not g["passed"]                                                                    # Airflow failed, the rewrite succeeded: the generated test now fails the gate


def test_generated_test_asserts_failure_for_a_virtualenv_task_that_cannot_build(tmp_path):
    spec = {**BRANCH_SPEC, "tasks": [BRANCH_SPEC["tasks"][0], {**BRANCH_SPEC["tasks"][1], "operator": "_PythonVirtualenvDecoratedOperator"}, BRANCH_SPEC["tasks"][2]]}
    a1 = {"dag_id": "br", "state": "failed", "tasks": {"decide": {"state": "success"}, "a": {"state": "failed"}, "b": {"state": "upstream_failed"}}, "observed": {}, "errors": [], "error": None}
    assert A.expected_failure(spec, baseline_dir(tmp_path, a1))["why"].startswith("a virtualenv")
    g = A.g_tests(spec, "rw_br.defs.br", project(tmp_path, FAILING), tmp_path / "bl_fail")
    assert g["passed"] and "virtualenv" in g["detail"] and "EXPECTED FAILURE" in g["detail"]


def test_generated_test_keeps_expecting_success_without_a_baseline_or_when_airflow_failed_for_another_environmental_reason(tmp_path):
    p = project(tmp_path, FAILING)
    g = A.g_tests(BRANCH_SPEC, "rw_br.defs.br", p, None)
    assert not g["passed"] and "EXPECTED FAILURE" not in g["detail"]                            # today's behaviour: the generated materialize test expects success
    a1 = {"dag_id": "br", "state": "failed", "tasks": {"decide": {"state": "failed"}}, "observed": {}, "errors": ["ModuleNotFoundError: No module named 'pandas'"], "error": None}
    assert A.expected_failure(BRANCH_SPEC, baseline_dir(tmp_path, a1)) is None                 # an environmental failure says nothing about what the rewrite should do


# ---- nondeterministic decisions ---------------------------------------------------------------------------------------------------------------------
def test_nondeterministic_decisions_only_for_a_decision_that_reads_time_or_random():
    rnd = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "callable": {"source": "def f():\n    return random.choice(['a', 'b'])"}}, *BRANCH_SPEC["tasks"][1:]]}
    assert list(B.nondeterministic_decisions(rnd)) == ["decide"]
    minute = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "callable": {"source": "def f(ctx):\n    return 'a' if ctx['logical_date'].minute % 2 == 0 else 'b'"}}, *BRANCH_SPEC["tasks"][1:]]}
    assert list(B.nondeterministic_decisions(minute)) == ["decide"]
    det = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "callable": {"source": "def f(ctx):\n    return 'a' if ctx['params']['x'] else 'b'"}}, *BRANCH_SPEC["tasks"][1:]]}
    assert B.nondeterministic_decisions(det) == {}
    not_a_decision = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "operator": "PythonOperator", "callable": {"source": "def f():\n    return datetime.now()"}}, *BRANCH_SPEC["tasks"][1:]]}
    assert B.nondeterministic_decisions(not_a_decision) == {}                                  # the clock elsewhere does not trigger it


def test_task_states_below_a_nondeterministic_decision_are_not_compared_but_outcome_and_failure_still_are():
    spec = {**BRANCH_SPEC, "tasks": [{**BRANCH_SPEC["tasks"][0], "callable": {"source": "def f():\n    return random.choice(['a', 'b'])"}}, *BRANCH_SPEC["tasks"][1:]]}
    a1 = {"state": "success", "tasks": {"decide": {"state": "success"}, "a": {"state": "success"}, "b": {"state": "skipped"}}, "observed": {}}
    structure = {"jobs": ["br"], "schedules": [], "sensors": [], "assets": {"d": {"deps": []}, "a": {"deps": []}, "b": {"deps": []}}}
    # the route took the other branch: a skipped, b success
    rt = {"state": "success", "assets": {"d": {"state": "success"}, "a": {"state": "not_run"}, "b": {"state": "success"}}, "structure": structure, "observed": {}}
    tm = {"decide": {"mode": "replaced", "asset": "d"}, "a": {"mode": "replaced", "asset": "a"}, "b": {"mode": "replaced", "asset": "b"}}
    res = B.compare(spec, a1, a1, rt, tm, "rewrite")
    tasks = res["dimensions"]["tasks"]
    assert tasks["verdict"] == "match" and tasks["compared"] == 1 and tasks["nondeterministic"]["not_compared_tasks"] == ["a", "b"]    # only the decision task itself is compared
    assert res["rollup"]["verdict"] != "different"
    # the same states with a DETERMINISTIC decision: different
    spec2 = {**BRANCH_SPEC}
    assert B.compare(spec2, a1, a1, rt, tm, "rewrite")["dimensions"]["tasks"]["verdict"] == "differ"
    # outcome is still compared: the route fails where Airflow succeeded
    bad = {**rt, "state": "failed"}
    assert B.compare(spec, a1, a1, bad, tm, "rewrite")["dimensions"]["outcome"]["verdict"] == "differ"
    # when nothing but the nondeterministic part could be compared the dimension says so, as `not compared`, never `different`
    tm2 = {**tm, "decide": {"mode": "dropped", "asset": None}}
    r2 = B.compare(spec, a1, a1, rt, tm2, "rewrite")
    assert r2["dimensions"]["tasks"]["verdict"] == "not-compared" and r2["dimensions"]["tasks"]["reason"].startswith("nondeterministic: not compared")


def test_report_lists_the_dags_whose_task_states_were_not_compared_for_a_nondeterministic_decision():
    import eval_report as R
    recs = [{"dag_id": "a", "behavior": {"routes": {"rewrite": {"dimensions": {"tasks": {"verdict": "match", "nondeterministic": {"nondeterministic_decisions": {"branching": "the decision reads `random.choice` (time or random)"}, "not_compared_tasks": ["x"]}}}}}}},
            {"dag_id": "b", "behavior": {"routes": {"rewrite": {"dimensions": {"tasks": {"verdict": "match"}}}}}}, {"dag_id": "c", "behavior": None}]
    assert R.nondeterministic_rows(recs) == [("a", "the decision reads `random.choice` (time or random)")]
