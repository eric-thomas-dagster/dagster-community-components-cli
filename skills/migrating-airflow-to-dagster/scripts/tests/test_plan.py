"""plan.py: patterns, complexity buckets, paths, waves, effort and the pilot set. The plan is a heuristic; these tests pin the rules, not their wisdom."""
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import plan as PL  # noqa: E402

EXAMPLE = HERE.parents[1] / "examples" / "migration_plan"


def task(i, dag, tid, op, cls="MECH", signals=None):
    return {"id": f"T{i}", "kind": "task", "dag_id": dag, "task_id": tid, "operator": op, "classification": cls, "signals": signals or {}, "status": "pending"}


def dag(i, name, cls="MECH", sched="cron", signals=None, kind="dag"):
    return {"id": f"D{i}", "kind": kind, "dag_id": name, "file": f"{name}.py", "schedule": {"kind": sched}, "classification": cls, "signals": signals or {}, "status": "pending",
            "task_classes": {}}


def manifest():
    units = [dag(1, "dbt_all", signals={"cosmos": 1}, kind="cosmos"),
             dag(2, "elt_load"), task(3, "elt_load", "copy", "CopyFromExternalStageToSnowflakeOperator"), task(4, "elt_load", "ls", "S3ListOperator"),
             dag(5, "py_report", cls="JUDG"), task(6, "py_report", "a", "PythonOperator", "MECH"), task(7, "py_report", "b", "@task", "JUDG", {"xcom": 1}),
             dag(8, "router", cls="JUDG"), task(9, "router", "pick", "BranchPythonOperator", "REDESIGN"), task(10, "router", "x", "PythonOperator"),
             dag(11, "manual_branchy", cls="JUDG", sched="manual"), task(12, "manual_branchy", "pick", "BranchPythonOperator", "REDESIGN"),
             dag(13, "waiter", cls="JUDG", sched="dataset", signals={"dataset_or_asset": 1}), task(14, "waiter", "w", "FileSensor", "JUDG"), task(15, "waiter", "p", "PythonOperator")]
    return {"version": 1, "root": ".", "units": units}


def test_pattern_complexity_path_and_wave():
    p = PL.build(manifest(), None, None)
    by = {d["dag_id"]: d for d in p["dags"]}
    assert by["dbt_all"]["pattern"] == "dbt" and by["elt_load"]["pattern"] == "elt_transfer" and by["py_report"]["pattern"] == "python_etl"
    assert by["waiter"]["pattern"] == "sensor_driven"
    assert by["elt_load"]["bucket"] == "mechanical" and by["py_report"]["bucket"] == "needs-care" and by["router"]["bucket"] == "redesign"
    assert by["elt_load"]["wave"] == 1 and by["py_report"]["wave"] == 2 and by["router"]["wave"] == 4        # redesign-heavy is the last wave
    assert by["router"]["path"] == "observe/bridge with airlift"                 # scheduled redesign: observe it while the redesign is done
    assert by["manual_branchy"]["path"] == "defer" and by["manual_branchy"]["wave"] == 4        # unscheduled redesign with no lineage value
    assert by["dbt_all"]["lineage_value"] == "high"
    assert p["waves"][-1]["wave"] == 4 and {"router", "manual_branchy"} <= set(p["waves"][-1]["dags"])


def test_converter_report_downgrades_effort_and_states_stubs():
    m = manifest()
    base = {d["dag_id"]: d for d in PL.build(m, None, None)["dags"]}
    ok = {"dags": {"py_report": {"status": "converted", "caveats": [{"task": "b", "kind": "degraded", "reason": "x"}]}}}
    stubbed = {"dags": {"py_report": {"status": "converted", "caveats": [{"task": "b", "kind": "stub", "reason": "x"}]}}}
    deferred = {"dags": {"router": {"status": "deferred", "reason": "custom timetable"}}}
    a = {d["dag_id"]: d for d in PL.build(m, None, ok)["dags"]}["py_report"]
    b = {d["dag_id"]: d for d in PL.build(m, None, stubbed)["dags"]}["py_report"]
    assert a["effective_bucket"] == "mechanical" and a["effort_days"] < base["py_report"]["effort_days"]
    assert b["effective_bucket"] == "needs-care" and b["stubs"] == 1 and b["effort_days"] > a["effort_days"]
    d = {x["dag_id"]: x for x in PL.build(m, None, deferred)["dags"]}["router"]
    assert any("custom timetable" in r for r in d["bucket_reasons"])


def test_effort_formula_matches_the_stated_rules():
    d = next(x for x in PL.build(manifest(), None, None)["dags"] if x["dag_id"] == "py_report")
    r = PL.RULES["effort_days"]
    expect = r["base"]["needs-care"] + r["per_task"]["MECH"] * 1 + r["per_task"]["JUDG"] * 1
    assert d["effort_days"] == max(round(expect * 2) / 2, 0.5)
    assert d["score"] == PL.RULES["task_points"]["MECH"] + PL.RULES["task_points"]["JUDG"] + PL.RULES["signal_points"] * 1


def test_pilot_covers_a_plain_a_trigger_and_an_xcom_dag_and_skips_non_rewrites():
    p = PL.build(manifest(), None, None)
    ids = [c["dag_id"] for c in p["pilot"]]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert "waiter" in ids and "py_report" in ids                                  # trigger, and XCom
    assert "manual_branchy" not in ids and "router" not in ids                      # not native-rewrite candidates


def test_output_is_deterministic_and_inputs_are_untouched(tmp_path):
    m = manifest()
    (tmp_path / "m.json").write_text(json.dumps(m))
    before = hashlib.sha256((tmp_path / "m.json").read_bytes()).hexdigest()
    outs = []
    for k in ("a", "b"):
        subprocess.run([sys.executable, str(HERE.parent / "plan.py"), str(tmp_path / "m.json"), "--out-dir", str(tmp_path / k)], check=True, capture_output=True)
        outs.append(((tmp_path / k / "plan.md").read_bytes(), (tmp_path / k / "plan.json").read_bytes()))
    assert outs[0] == outs[1]
    assert hashlib.sha256((tmp_path / "m.json").read_bytes()).hexdigest() == before
    shuffled = copy.deepcopy(m)
    shuffled["units"].reverse()
    (tmp_path / "s.json").write_text(json.dumps(shuffled))
    subprocess.run([sys.executable, str(HERE.parent / "plan.py"), str(tmp_path / "s.json"), "--out-dir", str(tmp_path / "c")], check=True, capture_output=True)
    assert (tmp_path / "c" / "plan.json").read_bytes() == outs[0][1]               # unit order in the manifest does not change the plan


def test_plan_md_states_that_it_is_a_heuristic_and_prints_the_formula(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps(manifest()))
    subprocess.run([sys.executable, str(HERE.parent / "plan.py"), str(tmp_path / "m.json"), "--out-dir", str(tmp_path)], check=True, capture_output=True)
    text = (tmp_path / "plan.md").read_text()
    plan = json.loads((tmp_path / "plan.json").read_text())
    assert "heuristic" in text.lower() and plan["heuristic"] is True
    assert "**Effort**" in text and "**Score** =" in text and "reference/mapping.md" in text
    assert "### Wave 4: redesign-heavy" in text and "## Suggested pilot" in text


def test_committed_example_is_current():
    for sub, extra in (("", []), ("with_conversion", ["--ir", "ir.json", "--report", "report.json"])):
        d = EXAMPLE / sub if sub else EXAMPLE
        got = PL.build(json.loads((d / "manifest.json").read_text()), json.loads((d / "ir.json").read_text()) if extra else None,
                       json.loads((d / "report.json").read_text()) if extra else None)
        assert json.loads((d / "plan.json").read_text()) == json.loads(json.dumps(got, sort_keys=True))
        assert (d / "plan.md").read_text() == PL.render(got)
