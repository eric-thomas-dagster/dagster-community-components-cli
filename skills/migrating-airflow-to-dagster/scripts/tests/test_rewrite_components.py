"""Registry components accepted on equal terms with module-style rewrites, the generated test's environment, the printed-output comparison, nondeterministic baseline tasks, the thin/substantive split,
the two-arm kits and the arm comparison.

The component fixture (`fixtures_component_project/rw_comp_demo`) is a real project: made with `uvx create-dagster project rw_comp_demo --uv-sync`, then `dagster-component add shell_command_asset` run against the live
registry; its files are vendored here so the tests run offline."""
import argparse
import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "parity"))
pytest.importorskip("dagster")
import accept_rewrite as A  # noqa: E402
import behavior_compare as B  # noqa: E402
import eval_kit as K  # noqa: E402
import eval_report as R  # noqa: E402
import eval_sample as S  # noqa: E402
import rewrite_project as RPJ  # noqa: E402

FIXTURE = HERE / "fixtures_component_project" / "rw_comp_demo"
EX = SCRIPTS.parent / "examples" / "rewrite"

COMP_SPEC = {"dag_id": "comp_demo", "connections": [], "variables": [], "schedule": {"kind": "cron_trigger", "cron": "0 6 * * *", "timezone": "UTC", "assets": None},
             "tasks": [{"task_id": "export_raw", "operator": "BashOperator", "upstream": [], "downstream": ["notify"], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []},
                       {"task_id": "notify", "operator": "EmptyOperator", "upstream": ["export_raw"], "downstream": [], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []}]}


def comp_project(tmp_path):
    dst = tmp_path / "comp"
    shutil.copytree(FIXTURE, dst)
    return dst


# ---- a component project (defs.yaml, no module-level asset) passes every gate ---------------------------------------------------------------------
def test_component_project_is_loaded_through_the_projects_loader_and_found_by_key(tmp_path):
    p = comp_project(tmp_path)
    assert RPJ.package_of(p) == "rw_comp_demo" and RPJ.package_of(p, "RW_COMP_DEMO") == "rw_comp_demo"       # the folder on disk, whatever case the hint has
    s = RPJ.defs_summary(p)
    assert list(s["assets"]) == ["raw_data_export"] and s["jobs"] == ["comp_demo"]
    assert s["schedules"] == [{"name": "comp_demo_schedule", "cron": "0 6 * * *", "timezone": "UTC"}]
    use = RPJ.component_usage(p)
    assert use["registry"] == ["shell_command_asset"] and use["other"] == [] and use["component_folders"] == 1
    assert "export_raw: replaced by asset `raw_data_export`" in RPJ.header_text(p)                         # the header is the defs.yaml comment block
    lst = RPJ.dg_list_defs(p)                                                                                  # `dg list defs --json` agrees (skipped where dg is not installed)
    if lst is not None:
        assert [a["asset_key"] for a in lst["assets"]] == ["raw_data_export"]


def test_component_project_passes_every_gate_on_equal_terms(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERTED_TESTS_RUN_SHELL", "1")                                                      # the DAG is a shell command: let the generated test run it (`echo exported`)
    p = comp_project(tmp_path)
    res = A.run_gates(COMP_SPEC, None, p, None, None)
    by = {g["gate"]: g for g in res}
    assert all(g["passed"] for g in res), [(g["gate"], g["detail"]) for g in res if not g["passed"]]
    assert "3 passed, 1 skipped" in by["generated_tests"]["detail"] and "materialize skipped" not in by["generated_tests"]["detail"]          # the materialize test RAN through the loader
    assert "exists in the loaded definitions" in by["tasks_accounted_for"]["detail"] and by["no_invented_apis"]["passed"] and by["no_secret_values"]["passed"]


def test_the_header_naming_an_asset_that_is_not_loaded_fails_the_accounting_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERTED_TESTS_RUN_SHELL", "1")
    p = comp_project(tmp_path)
    y = p / "src" / "rw_comp_demo" / "defs" / "shell_command_asset" / "defs.yaml"
    y.write_text(y.read_text().replace("replaced by asset `raw_data_export`", "replaced by asset `no_such_asset`"))
    g = {x["gate"]: x for x in A.run_gates(COMP_SPEC, None, p, None, None)}["tasks_accounted_for"]
    assert not g["passed"] and "asset not found among the loaded asset keys" in g["detail"] and "export_raw" in g["detail"]


def test_a_broken_component_type_and_a_missing_header_are_caught(tmp_path):
    p = comp_project(tmp_path)
    y = p / "src" / "rw_comp_demo" / "defs" / "shell_command_asset" / "defs.yaml"
    y.write_text(y.read_text().replace("ShellCommandAssetComponent", "NoSuchComponent").replace("# notify: dropped", "# (gone): dropped"))
    by = {x["gate"]: x for x in A.run_gates(COMP_SPEC, None, p, None, None)}
    assert not by["no_invented_apis"]["passed"] and "NoSuchComponent" in by["no_invented_apis"]["detail"]
    assert not by["definitions_load"]["passed"] and not by["tasks_accounted_for"]["passed"] and "notify" in by["tasks_accounted_for"]["detail"]


def test_module_style_projects_still_pass_whatever_the_file_is_called(tmp_path):
    """the committed module-style rewrite, its file renamed with a leading underscore (a worker's choice that once made the harness look for the wrong module) and a stale --module hint"""
    proj = tmp_path / "proj"
    shutil.copytree(EX / "project", proj, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    d = proj / "src" / "rw_pkg" / "defs"
    (d / "partner_ingest.py").rename(d / "_partner_ingest.py")
    spec = json.loads((EX / "specs" / "partner_ingest.spec.json").read_text())
    res = A.run_gates(spec, "rw_pkg.defs.partner_ingest", proj, None, None)
    assert all(g["passed"] for g in res), [(g["gate"], g["detail"]) for g in res if not g["passed"]]


# ---- the generated test sets the sandbox's Variables and connections ----------------------------------------------------------------------------
VAR_MODULE = '''"""reader: replaced by asset `reader`

Decision record:
"""
import os

import dagster as dg


@dg.asset
def reader() -> None:
    if os.environ["AIRFLOW_VAR_MY_VARIABLE"] != "eval-variable" or not os.environ["AIRFLOW_CONN_MY_CONN"]:
        raise dg.Failure("the sandbox environment is missing")


job = dg.define_asset_job("vars", selection=[reader])
daily = dg.ScheduleDefinition(name="daily", job=job, cron_schedule="0 0 * * *", execution_timezone="UTC")
'''


def module_project(tmp_path, body, pkg="rw_vars", name="vars"):
    p = tmp_path / "mp"
    d = p / "src" / pkg / "defs"
    d.mkdir(parents=True)
    (p / "src" / pkg / "__init__.py").write_text("")
    (d / "__init__.py").write_text("")
    (d / f"{name}.py").write_text(body)
    (p / "pyproject.toml").write_text(f'[project]\nname = "{pkg}"\nversion = "0.0.1"\n\n[tool.dg]\ndirectory_type = "project"\n\n[tool.dg.project]\nroot_module = "{pkg}"\ndefs_module = "{pkg}.defs"\n')
    (p / "src" / pkg / "definitions.py").write_text("from pathlib import Path\n\nfrom dagster import definitions, load_from_defs_folder\n\n\n@definitions\ndef defs():\n    return load_from_defs_folder(path_within_project=Path(__file__).parent)\n")
    return p


VARS_SPEC = {"dag_id": "vars", "connections": ["my_conn"], "variables": ["my_variable"], "schedule": {"kind": "interval", "cron": None, "timezone": "UTC", "assets": None},
             "tasks": [{"task_id": "reader", "operator": "PythonOperator", "upstream": [], "downstream": [], "retries": 0, "trigger_rule": "all_success", "reads_value_from": []}]}


def test_generated_test_exports_the_variables_and_connections_the_sandbox_does(tmp_path):
    p = module_project(tmp_path, VAR_MODULE)
    g = A.g_tests(VARS_SPEC, "rw_vars.defs.vars", p, None)
    assert g["passed"], g["detail"]                                                                           # a DAG that needs AIRFLOW_VAR_X is tested, not failed for lack of it
    env = A.sandbox_env(VARS_SPEC)
    assert env["AIRFLOW_VAR_MY_VARIABLE"] == "eval-variable" and env["AIRFLOW_CONN_MY_CONN"].startswith("generic://") and "AIRFLOW_CONN_SQLITE_DEFAULT" in env


def test_an_interval_schedule_is_not_asserted_absent_and_the_loader_finds_a_schedule_in_a_sibling_file(tmp_path):
    p = module_project(tmp_path, VAR_MODULE)                                                                  # kind `interval`: no cron to compare, the schedule exists
    g = A.g_tests(VARS_SPEC, "rw_vars.defs.vars", p, None)
    assert g["passed"], g["detail"]
    manual = {**VARS_SPEC, "schedule": {"kind": "manual", "cron": None, "timezone": "UTC", "assets": None}}
    assert not A.g_tests(manual, "rw_vars.defs.vars", p, None)["passed"]                                      # a manual DAG with a schedule is still caught


def test_the_package_on_disk_is_used_not_the_name_computed_from_the_dag_id(tmp_path):
    p = module_project(tmp_path, VAR_MODULE, pkg="rw_vars_s3")                                                # `uv` lower-cases: the index says rw_vars_S3
    g = A.g_tests(VARS_SPEC, "rw_vars_S3.defs.vars", p, None)
    assert g["passed"], g["detail"]


def test_dg_check_defs_ignores_an_outer_virtualenv(tmp_path, monkeypatch):
    p = module_project(tmp_path, VAR_MODULE)
    monkeypatch.setenv("VIRTUAL_ENV", "/nonexistent/outer/venv")
    assert "VIRTUAL_ENV" not in RPJ.clean_env(p)
    assert A.g_load(p, "rw_vars", "rw_vars.defs.vars")["passed"]


# ---- printed output --------------------------------------------------------------------------------------------------------------------------------
def test_printed_lines_keep_prints_drop_log_records_and_mask_volatile_tokens():
    text = ("[2026-11-26 14:46:44,335] {python.py:240} INFO - Done. Returned value was: None\n"
            "2026-11-26T14:47:08.459181Z [info     ] Task instance state updated [airflow.api]\n"
            "INFO  [alembic.runtime.migration] Context impl SQLiteImpl.\n"
            "hello world\n"
            "Logical date:  2026-11-26 14:47:06.366151+00:00\n"
            "run manual__2026-11-26T14:47:06.371179+00:00 on <Hook object at 0x112004320> in /var/folders/ab/T/tmpx1/data.csv took 3.2s (pid: 4242)\n"
            "Traceback (most recent call last):\n  File \"x.py\", line 1\nValueError: boom\n"
            "/x/y/UserWarning: something\n")
    got = B.printed_lines(text)
    assert got[0] == "hello world" and got[1] == "Logical date:  <time>"
    assert got[2] == "run <run-id> on <Hook object at <addr>> in <path> took <duration> (pid <n>)" or "<run-id>" in got[2]
    assert len(got) == 3


def test_stdout_dimension_requires_what_airflow_printed_and_tolerates_extra_lines():
    a = {"stdout_lines": ["x 1", "y 2", "volatile 1"]}
    a2 = {"stdout_lines": ["x 1", "y 2", "volatile 2"]}
    ok = B.compare_stdout(a, a2, {"stdout_lines": ["$ echo", "y 2", "x 1", "extra"]})
    assert ok["verdict"] == "match" and ok["compared"] == 2 and ok["evidence"]["extra_in_route"] and ok["evidence"]["unstable_in_baseline"] == ["volatile 1"]
    bad = B.compare_stdout(a, a2, {"stdout_lines": ["x 1"]})
    assert bad["verdict"] == "differ" and bad["evidence"]["missing_in_route"] == ["y 2"]
    assert B.compare_stdout({"stdout_lines": ["only once"]}, {"stdout_lines": ["only once"]}, {"stdout_lines": []})["verdict"] == "differ"
    assert B.compare_stdout({"stdout_lines": []}, None, {"stdout_lines": ["x"]})["verdict"] == "not-compared"
    assert B.compare_stdout({}, None, {"stdout_lines": ["x"]})["verdict"] == "not-compared"                   # no record on the baseline side: not compared, never match


def test_stdout_requires_presence_not_the_multiplicity_of_airflows_repeated_continuation_lines():
    a = {"stdout_lines": ["0  1  3", "1  2  4", "0  1  3", "1  2  4", "a  b", "0  1  3", "1  2  4"]}
    assert B.compare_stdout(a, a, {"stdout_lines": ["a  b", "0  1  3", "1  2  4"]})["verdict"] == "match"
    assert B.compare_stdout(a, a, {"stdout_lines": ["a  b", "0  1  3"]})["verdict"] == "differ"


def test_a_printed_line_match_makes_an_equivalent_substantive_and_a_miss_never_makes_it_different():
    spec = {"dag_id": "d", "schedule": {"kind": "manual"}, "tasks": [{"task_id": "a", "upstream": [], "downstream": [], "retries": 0, "reads_value_from": [], "pool": None}]}
    structure = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"a": {"deps": [], "retry": [], "pool": [], "node": "a"}}}
    af = {"state": "success", "tasks": {"a": {"state": "success", "xcom": {}}}, "observed": {"files": {}, "sqlite": {}, "http": [], "smtp": [], "s3": {}}, "stdout_lines": ["hi"]}
    rt = {"state": "success", "assets": {"a": {"state": "success", "value": None}}, "structure": structure, "observed": af["observed"], "stdout_lines": ["hi"]}
    tm = {"a": {"mode": "replaced", "asset": "a"}}
    r = B.compare(spec, af, af, rt, tm, "rewrite")
    assert r["rollup"]["verdict"] == "equivalent" and r["rollup"]["thin"] is False
    r = B.compare(spec, {k: v for k, v in af.items() if k != "stdout_lines"}, None, {k: v for k, v in rt.items() if k != "stdout_lines"}, tm, "rewrite")
    assert r["rollup"]["verdict"] == "equivalent" and r["rollup"]["thin"] is True
    r = B.compare(spec, af, af, {**rt, "stdout_lines": []}, tm, "rewrite")
    assert r["rollup"]["verdict"] == "equivalent-with-differences" and "stdout" in r["rollup"]["why"]


# ---- a task nondeterministic in the baseline --------------------------------------------------------------------------------------------------------
def test_a_task_whose_state_differs_across_baseline_runs_is_not_compared_and_the_rest_still_is():
    spec = {"dag_id": "d", "schedule": {"kind": "manual"}, "tasks": [{"task_id": "flaky", "upstream": [], "downstream": [], "retries": 0, "reads_value_from": [], "pool": None},
                                                                       {"task_id": "steady", "upstream": [], "downstream": [], "retries": 0, "reads_value_from": [], "pool": None}]}
    structure = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"flaky": {"deps": [], "retry": [], "pool": [], "node": "flaky"}, "steady": {"deps": [], "retry": [], "pool": [], "node": "steady"}}}
    obs = {"files": {}, "sqlite": {}, "http": [], "smtp": [], "s3": {}}
    mk = lambda f: {"state": "failed", "tasks": {"flaky": {"state": f, "xcom": {}}, "steady": {"state": "success", "xcom": {}}}, "observed": obs}      # noqa: E731
    rt = {"state": "success", "assets": {"flaky": {"state": "success"}, "steady": {"state": "success"}}, "structure": structure, "observed": obs}
    tm = {"flaky": {"mode": "replaced", "asset": "flaky"}, "steady": {"mode": "replaced", "asset": "steady"}}
    # this measurement's two runs agree (failed, failed) but an EARLIER run of the same DAG succeeded
    res = B.compare(spec, mk("failed"), mk("failed"), rt, tm, "rewrite", extra_runs=[mk("failed"), mk("success")])
    t = res["dimensions"]["tasks"]
    assert t["verdict"] == "match" and t["compared"] == 1 and t["evidence"]["nondeterministic_baseline"] == {"flaky": ["failed", "success"]}
    assert any(x.startswith("flaky: nondeterministic: not compared") for x in t["evidence"]["not_compared"])
    assert res["dimensions"]["failure"]["verdict"] == "not-compared" or res["dimensions"]["failure"]["verdict"] == "match"
    # without the earlier run the same task IS compared and differs (the harness has no reason to excuse it)
    res2 = B.compare(spec, mk("failed"), mk("failed"), rt, tm, "rewrite")
    assert res2["dimensions"]["tasks"]["verdict"] == "differ"
    import eval_report as ER
    assert ER.baseline_nondeterministic([{"dag_id": "d", "behavior": {"routes": {"rewrite": res}}}]) == [("d", ["flaky"])]


# ---- report: thin split, registry first, arms ---------------------------------------------------------------------------------------------------------------
def fake_arm_run(tmp_path, run, rows):
    """rows: (arm, rewrite verdict, thin, registry components, searched)"""
    priv = tmp_path / f"{run}-private"
    (priv / "behavior").mkdir(parents=True)
    (priv / "gates").mkdir()
    index = {}
    for i, (arm, verdict, thin, comps, searched) in enumerate(rows):
        key, d = f"r::d{i}", f"d{i}"
        index[key] = {"dir": d, "dag_id": d, "stratum": "clean" if i % 2 else "degraded", "pattern": "python", "repo": "r", "project": f"rw_{d}", "arm": arm, "group": f"{arm}1"}
        (priv / "behavior" / d).mkdir()
        rw = {"rollup": {"verdict": verdict, "why": "x", "thin": thin}, "dimensions": {}}
        (priv / "behavior" / d / "behavior.json").write_text(json.dumps({"routes": {"rewrite": rw, "converter": {"rollup": {"verdict": "different", "why": "x", "thin": True}, "dimensions": {}}}, "baseline": {"class": ["ok", ""]}}))
        (priv / "gates" / f"{d}.report.json").write_text(json.dumps({"attempts": 3, "registry_components": comps, "searches_reported": searched, "components": {"registry": comps, "other": []}, "style": "component" if comps else "module"}))
    (priv / "kit_index.json").write_text(json.dumps(index))


def render(tmp_path, runs):
    a = argparse.Namespace(work=tmp_path, runs=runs, writers="cold", costs=None, variance_run=None, manifest=None, baselines=None, out=None)
    return R.render(a)


def test_report_separates_substantive_from_thin_and_counts_registry_use_per_arm(tmp_path):
    rows = ([("A", "equivalent", False, ["shell_command_asset"], True)] * 6 + [("A", "equivalent", True, [], True)] * 2 + [("A", "different", False, [], False)] * 2
            + [("B", "equivalent", False, [], True)] * 3 + [("B", "equivalent", True, [], False)] * 4 + [("B", "different", False, ["shell_command_asset"], True)] * 3)
    fake_arm_run(tmp_path, "s2", rows)
    t = render(tmp_path, ["s2"])
    assert "of which **with a substantive comparison**" in t and "of which **thin**" in t
    assert "| &nbsp;&nbsp;of which **thin** (only outcome, task states and structure were observable) | 6 |" in t
    assert "strictly equivalent **with a substantive comparison**" in t
    assert "| of which substantive | of which thin |" in t
    assert "## Registry first: component use and searches" in t and "`shell_command_asset` 9" in t
    assert "## Arm A against arm B" in t and "**This is a between-DAG comparison.**" in t and "Fisher exact p" in t
    assert "| **arm A** |" in t and "## Arm A (10 DAGs)" in t and "## Arm B (10 DAGs)" in t
    row = [ln for ln in t.splitlines() if ln.startswith("| strictly equivalent |")][0]
    assert "8/10" in row and "7/10" in row and "points" in row


def test_fisher_exact_and_newcombe_interval_match_known_values():
    assert abs(R.fisher_exact(3, 1, 1, 3) - 0.4857) < 1e-3
    assert abs(R.fisher_exact(8, 2, 1, 5) - 0.0350) < 1e-3
    assert R.fisher_exact(5, 5, 5, 5) == 1.0
    d, lo, hi = R.diff_ci(8, 10, 7, 10)
    assert abs(d - 0.1) < 1e-9 and lo < 0 < hi


# ---- fresh sample and two arms ------------------------------------------------------------------------------------------------------------------------
def test_exclude_removes_earlier_draws_and_arms_are_balanced_ungrouped_across_arms(tmp_path):
    import test_eval_harness as T
    m = T.synthetic_manifest(400)
    first = S.build_sample(m, None, 40, 7)
    fresh = S.build_sample(m, None, 60, 99, exclude={e["key"] for e in first["dags"]})
    assert not {e["key"] for e in fresh["dags"]} & {e["key"] for e in first["dags"]} and fresh["n"] == 60 and fresh["excluded_earlier_draws"] == 40
    S.assign_arms(fresh, 5, 4)
    arms = [e["arm"] for e in fresh["dags"]]
    assert arms.count("A") == 30 and arms.count("B") == 30
    cells = {}
    for e in fresh["dags"]:
        cells.setdefault((e["stratum"], e["pattern"]), []).append(e["arm"])
    assert all(abs(v.count("A") - v.count("B")) <= 1 for v in cells.values())                                   # balanced inside every (stratum, pattern) cell
    by_group = {}
    for e in fresh["dags"]:
        by_group.setdefault(e["group"], set()).add(e["arm"])
    assert all(len(a) == 1 for a in by_group.values())                                                          # a group never mixes arms
    assert sorted(len([e for e in fresh["dags"] if e["group"] == g]) for g in by_group if g.startswith("A")) == [2, 4, 4, 4, 4, 4, 4, 4]
    again = S.build_sample(m, None, 60, 99, exclude={e["key"] for e in first["dags"]})
    S.assign_arms(again, 5, 4)
    assert [(e["key"], e["arm"], e["group"]) for e in again["dags"]] == [(e["key"], e["arm"], e["group"]) for e in fresh["dags"]]      # deterministic
    names = {e["key"]: e["dag_id"] for e in fresh["dags"]}
    md = S.render_groups(fresh, names)
    assert "| A1 | A | 4 |" in md and "| B8 | B | 2 |" in md


@pytest.fixture()
def two_arm_work(tmp_path):
    import test_eval_harness as T
    w = tmp_path / "work"
    repo = w / "repos" / "fixrepo"
    (repo / "dags").mkdir(parents=True)
    for f in (T.FX / "dags").glob("*.py"):
        (repo / "dags" / f.name).write_text(f.read_text())
    (w / "irs").mkdir()
    (w / "irs" / "fixrepo.2.json").write_text((T.FX / "ir.json").read_text())
    rows = [{"key": f"fixrepo::{d['dag_id']}", "repo": "fixrepo", "dag_id": d["dag_id"], "file": d["file"].replace("dags/", "", 1), "airflow": "2", "eligible": True, "converter": "clean", "pattern": "python", "patterns": ["python"]} for d in T.IR["dags"]]
    (w / "manifest.json").write_text(json.dumps({"repos": {"fixrepo": {"dag_folders": ["dags"]}}, "dags": rows}))
    sample = {"seed": 1, "label": "test", "dags": [{"key": r["key"], "dag_id": r["dag_id"], "repo": "fixrepo", "stratum": "clean", "pattern": "python"} for r in rows]}
    S.assign_arms({**sample, "counts": {}}, 3, 4)
    for i, e in enumerate(sample["dags"]):
        e["arm"] = "A" if i % 2 == 0 else "B"
    (w / "sample.json").write_text(json.dumps(sample))
    base = w / "baselines"
    for r in rows:
        out = base / r["key"].replace("::", "__") / "out"
        out.mkdir(parents=True)
        for n in (1, 2):
            (out / f"airflow{n}.json").write_text(json.dumps({"dag_id": r["dag_id"], "state": "success", "tasks": {}, "observed": {}, "errors": [], "error": None}))
    return w


def test_two_arm_kits_differ_only_in_the_feedback_tool(two_arm_work):
    w = two_arm_work
    a = argparse.Namespace(work=w, manifest=w / "manifest.json", sample=w / "sample.json", run="kit", dcc_skill="/example/skills/dagster-community-components", baseline_dir=w / "baselines")
    assert K.make(a) == 0
    index = json.loads((w / "kit-private" / "kit_index.json").read_text())
    arms = {v["dir"]: v["arm"] for v in index.values()}
    assert set(arms.values()) == {"A", "B"}
    for name, arm in arms.items():
        d = w / "kit" / name
        task = (d / "TASK.md").read_text()
        assert (w / "kit-private" / "baseline" / name / "airflow1.json").exists()                              # the harness keeps the baseline for both arms
        if arm == "A":
            assert (d / "tools" / "compare_behavior.py").exists() and (d / "tools" / "baseline" / "airflow1.json").exists() and "Iterate until every DIFFERENT dimension" in task and "--baseline ../../tools/baseline" in task
        else:
            assert not (d / "tools" / "compare_behavior.py").exists() and not (d / "tools" / "baseline").exists()
            assert "compare_behavior" not in task and "baseline" not in task.lower().replace("the harness runs it against the original dag's airflow results", "") and "Iterate until" not in task
            assert "compare_behavior" not in (d / "WORKER_REPORT.md").read_text() and "compare_behavior" not in (d / "prompt.md").read_text()
        assert "Decision record:" in task and "registry first" in task and "component style" in task          # identical instructions otherwise
        assert (d / "tools" / "accept_rewrite.py").exists() and (d / "tools" / "rewrite_project.py").exists() and (d / "tools" / "behavior_compare.py").exists()
    assert K.check_all(w, "kit", index, "/example/skills/dagster-community-components") == []
    b = next(n for n, x in arms.items() if x == "B")
    (w / "kit" / b / "tools" / "baseline").mkdir()
    (w / "kit" / b / "tools" / "baseline" / "airflow1.json").write_text("{}")
    assert any("arm B kit holds tools/baseline" in p for p in K.check_all(w, "kit", index, "/example/skills/dagster-community-components"))
    shutil.rmtree(w / "kit" / b / "tools" / "baseline")
    shutil.copy2(K.HERE / "parity" / "compare_behavior.py", w / "kit" / b / "tools" / "compare_behavior.py")
    assert any("arm B kit holds tools/compare_behavior.py" in p for p in K.check_all(w, "kit", index, "/example/skills/dagster-community-components"))
