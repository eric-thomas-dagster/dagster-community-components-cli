"""The evaluation harness's own tests: the sampler, the cold-worker kit and its guard, the behaviour comparison (pure functions, then end to end against real Airflow on fixture DAGs and
deliberately broken rewrites) and the report.

The end-to-end tests need an Airflow 2 interpreter: set EVAL_AIRFLOW_PYTHON to one (for example `venvs/af210/bin/python` of the evaluation work directory). Without it they skip, and say so.
They run the original fixture DAG in real Airflow (`dag.test()`, twice), a rewrite in Dagster, and compare; then change a command, drop a side effect, change a schedule, and assert the harness flags each.
"""
import collections
import copy
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "parity"))
import behavior_compare as B  # noqa: E402
import eval_common as C  # noqa: E402
import eval_kit as K  # noqa: E402
import eval_report as R  # noqa: E402
import eval_sample as S  # noqa: E402

FX = HERE / "fixtures_behavior"
IR = json.loads((FX / "ir.json").read_text())


# ---- statistics -----------------------------------------------------------------------------------------------------------------------------------------
def test_wilson_interval_matches_known_values():
    lo, hi = R.wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.49, 0.943)
    assert R.wilson(0, 0) == (0.0, 0.0)
    lo, hi = R.wilson(0, 20)
    assert lo == 0.0 and 0.15 < hi < 0.17


def test_sign_test_is_exact():
    assert R.sign_test(0, 0) == 1.0
    assert R.sign_test(5, 5) == 1.0
    assert abs(R.sign_test(0, 8) - 2 / 256) < 1e-9


# ---- sampler --------------------------------------------------------------------------------------------------------------------------------------------
def synthetic_manifest(n=300):
    rows = []
    strata = ["clean"] * 6 + ["degraded"] + ["stubbed"] * 2 + ["deferred"]
    pats = ["python", "bash", "sql", "sensor", "branching", "mapping", "transfer"]
    for i in range(n):
        rows.append({"key": f"r{i % 7}::d{i}", "repo": f"r{i % 7}" if i % 5 else "r0", "dag_id": f"d{i}", "eligible": i % 11 != 0, "converter": strata[i % len(strata)], "pattern": pats[i % len(pats)],
                     "patterns": [pats[i % len(pats)]], "airflow": "2"})
    return {"dags": rows}


def test_sampler_is_deterministic_and_seed_sensitive():
    m = synthetic_manifest()
    a = S.build_sample(m, 1, None, 7)
    b = S.build_sample(m, 1, None, 7)
    c = S.build_sample(m, 1, None, 8)
    assert a == b
    assert [e["key"] for e in a["dags"]] != [e["key"] for e in c["dags"]]


def test_stage1_takes_every_hard_dag_up_to_20_and_spreads_repos():
    m = synthetic_manifest()
    s = S.build_sample(m, 1, None, 3)
    elig = [r for r in m["dags"] if r["eligible"]]
    hard_available = sum(1 for r in elig if r["converter"] in ("stubbed", "deferred"))
    assert s["n"] == 40
    hard = sum(1 for e in s["dags"] if e["hard"])
    assert hard == min(20, hard_available)
    assert max(s["counts"]["repo"].values()) <= int(40 * 0.25) + 1
    assert set(s["counts"]["stratum"]) <= {"clean", "degraded", "stubbed", "deferred"}
    assert len({e["pattern"] for e in s["dags"]}) >= 5
    assert all(r["eligible"] for r in elig if r["key"] in {e["key"] for e in s["dags"]})


def test_stage2_has_no_cap_and_continues_stage1():
    m = synthetic_manifest()
    s1 = S.build_sample(m, 1, None, 3)
    s2 = S.build_sample(m, 2, None, 3)
    elig = [r for r in m["dags"] if r["eligible"]]
    assert s2["n"] == len(elig) > 100                   # not capped at 20, or at 40
    assert [e["key"] for e in s2["dags"][:40]] == [e["key"] for e in s1["dags"]]
    assert len({e["key"] for e in s2["dags"]}) == s2["n"]
    s3 = S.build_sample(m, None, 120, 3)
    assert s3["n"] == 120 and len({e["key"] for e in s3["dags"]}) == 120


def test_sampler_never_picks_an_ineligible_dag():
    m = synthetic_manifest()
    ok = {r["key"] for r in m["dags"] if r["eligible"]}
    for stage in (1, 2):
        assert {e["key"] for e in S.build_sample(m, stage, None, 1)["dags"]} <= ok


# ---- cold-worker kit ----------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def work(tmp_path):
    w = tmp_path / "work"
    repo = w / "repos" / "fixrepo"
    (repo / "dags").mkdir(parents=True)
    for f in (FX / "dags").glob("*.py"):
        (repo / "dags" / f.name).write_text(f.read_text())
    (w / "irs").mkdir()
    (w / "irs" / "fixrepo.2.json").write_text((FX / "ir.json").read_text())
    rows = []
    for d in IR["dags"]:
        rows.append({"key": f"fixrepo::{d['dag_id']}", "repo": "fixrepo", "dag_id": d["dag_id"], "file": d["file"].replace("dags/", "", 1), "airflow": "2", "eligible": True, "converter": "clean", "pattern": "python", "patterns": ["python"]})
    manifest = {"repos": {"fixrepo": {"dag_folders": ["dags"]}}, "dags": rows}
    (w / "manifest.json").write_text(json.dumps(manifest))
    sample = {"seed": 1, "label": "test", "dags": [{"key": r["key"], "dag_id": r["dag_id"], "repo": "fixrepo", "stratum": "clean", "pattern": "python"} for r in rows]}
    (w / "sample.json").write_text(json.dumps(sample))
    return w


def make_kit(w):
    import argparse
    a = argparse.Namespace(work=w, manifest=w / "manifest.json", sample=w / "sample.json", run="kit", dcc_skill="/example/skills/dagster-community-components")
    return K.make(a)


def test_kit_has_what_a_worker_needs_and_nothing_else(work):
    assert make_kit(work) == 0
    d = work / "kit" / "fixture_pipeline"
    names = {p.name for p in d.iterdir()}
    assert {"TASK.md", "WORKER_REPORT.md", "spec.md", "spec.json", "prompt.md", "source", "tools", "out"} <= names
    task = (d / "TASK.md").read_text()
    for must in ("dagster-expert", "dagster-community-components", "Do NOT read", "Count your attempts", "AIRFLOW_CONN_", "out/rw_fixture_pipeline"):
        assert must in task
    assert "not run here: no Airflow baseline is given to you" in task                          # the baseline is not given to workers
    assert (d / "source" / "dags" / "fixture_pipeline.py").exists()
    assert not (d / "source" / "dags" / "fixture_fails.py").exists()                           # no other DAG's source
    spec = json.loads((d / "spec.json").read_text())
    assert spec["converter"] is None and spec["parity_baseline"] is None                         # no converter data, no baseline
    assert (d / "tools" / "accept_rewrite.py").exists() and (d / "tools" / "run_in_sandbox.py").exists()
    rep = (d / "WORKER_REPORT.md").read_text()
    for must in ("Route", "Number of attempts", "Registry components used", "Assumptions", "could not preserve"):
        assert must in rep
    assert (work / "kit-private" / "kit_index.json").exists() and not (d / "kit_index.json").exists()
    assert K.check_all(work, "kit", json.loads((work / "kit-private" / "kit_index.json").read_text()), "/example/skills/dagster-community-components") == []


def test_kit_guard_catches_converter_output_other_dags_and_local_paths(work):
    make_kit(work)
    index = json.loads((work / "kit-private" / "kit_index.json").read_text())
    dcc = "/example/skills/dagster-community-components"
    d = work / "kit" / "fixture_pipeline"
    (d / "runtime.py").write_text("# the converter's shared runtime\n")
    assert any("converter output" in p for p in K.check_all(work, "kit", index, dcc))
    (d / "runtime.py").unlink()
    (d / "notes.md").write_text('def f():\n    raise RuntimeError("AIRFLOW-CAVEAT: unsupported")\n')
    assert any("converter marker" in p for p in K.check_all(work, "kit", index, dcc))
    (d / "notes.md").write_text("see also fixture_fails for the failing case\n")
    assert any("another sampled DAG" in p for p in K.check_all(work, "kit", index, dcc))
    (d / "notes.md").write_text(f"data is in {work}/somewhere\n")
    assert any("local path" in p for p in K.check_all(work, "kit", index, dcc))
    (d / "notes.md").unlink()
    (d / "source" / "dags" / "fixture_fails.py").write_text((FX / "dags" / "fixture_fails.py").read_text())
    assert any("file of another sampled DAG" in p for p in K.check_all(work, "kit", index, dcc))


def test_prompt_is_unchanged_generator_output_without_converter_text(work):
    import rewrite_prompt
    make_kit(work)
    d = work / "kit" / "fixture_pipeline"
    spec = json.loads((d / "spec.json").read_text())
    want = rewrite_prompt.render(spec, (d / "spec.md").read_text(), None, "rw_fixture_pipeline", "rw_fixture_pipeline")
    assert (d / "prompt.md").read_text() == want
    assert "AIRFLOW-CAVEAT" not in want and "No converter output is given" in want


# ---- behaviour comparison: pure functions -----------------------------------------------------------------------------------------------------
def af(state="success", tasks=None, observed=None, errors=None, error=None):
    return {"state": state, "tasks": tasks or {}, "observed": observed or {"files": {}, "sqlite": {}, "http": [], "smtp": [], "s3": {}}, "errors": errors or [], "error": error}


def route(state="success", assets=None, structure=None, observed=None, error=None):
    return {"state": state, "assets": assets or {}, "structure": structure or {"jobs": [], "schedules": [], "sensors": [], "assets": {}}, "observed": observed or {"files": {}, "sqlite": {}, "http": [], "smtp": [], "s3": {}}, "error": error}


SPEC = {"dag_id": "d", "schedule": {"kind": "manual"}, "tasks": [
    {"task_id": "a", "upstream": [], "downstream": ["b"], "retries": 0, "reads_value_from": [], "pool": None}, {"task_id": "b", "upstream": ["a"], "downstream": [], "retries": 0, "reads_value_from": ["a"], "pool": None}]}


def test_baseline_classification_environmental_versus_by_design():
    assert B.baseline_class(af("success"), af("success"))[0] == "ok"
    assert B.baseline_class(af("failed", errors=["ModuleNotFoundError: No module named 'x'"]), None)[0] == "baseline-failed"
    assert B.baseline_class(af("failed", errors=["ConnectionRefusedError: [Errno 111] Connection refused"]), None)[0] == "baseline-failed"
    assert B.baseline_class(af("failed", errors=["ValueError: by design"]), af("failed"))[0] == "by-design-failure"
    assert B.baseline_class(af(error="timeout 300s"), None)[0] == "baseline-failed"
    assert B.baseline_class(af("success"), af("failed", errors=["ValueError"]))[0] == "unstable"


def test_empty_comparison_is_not_compared_never_match():
    d = B.compare_maps("files", {}, {}, {})
    assert d["verdict"] == B.NOT
    assert B.compare_maps("files", {"x": "1"}, {"x": "1"}, {"x": "1"})["verdict"] == B.MATCH
    assert B.compare_maps("files", {"x": "1"}, {"x": "1"}, {})["verdict"] == B.DIFFER
    assert B.compare_maps("files", {}, {}, {"y": "1"})["verdict"] == B.DIFFER


def test_nondeterministic_fields_are_excluded_not_counted():
    d = B.compare_maps("files", {"f": "1", "t": "aaa"}, {"f": "1", "t": "bbb"}, {"f": "1", "t": "ccc"})
    assert d["verdict"] == B.MATCH and d["evidence"]["nondeterministic_excluded"] == ["t"]
    d = B.compare_maps("files", {"t": "aaa"}, {"t": "bbb"}, {"t": "ccc"})
    assert d["verdict"] == B.NOT


def test_rollup_rules():
    tasks = {"a": {"state": "success", "xcom": {"return_value": "1"}}, "b": {"state": "success", "xcom": {}}}
    tm = {"a": {"mode": "replaced", "asset": "d/a"}, "b": {"mode": "replaced", "asset": "d/b"}}
    st = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"d/a": {"deps": [], "retry": [], "pool": []}, "d/b": {"deps": ["d/a"], "retry": [], "pool": []}}}
    good = route(assets={"d/a": {"state": "success", "value": "1"}, "d/b": {"state": "success", "value": None}}, structure=st)
    r = B.compare(SPEC, af(tasks=tasks), af(tasks=tasks), good, tm, "rewrite")
    assert r["rollup"]["verdict"] == "equivalent" and r["rollup"]["thin"] is False      # a value was compared
    bad_value = copy.deepcopy(good)
    bad_value["assets"]["d/a"]["value"] = "2"
    r = B.compare(SPEC, af(tasks=tasks), af(tasks=tasks), bad_value, tm, "rewrite")
    assert r["dimensions"]["values"]["verdict"] == B.DIFFER
    assert r["dimensions"]["values"]["any_consumed"] is True          # b reads a's value, so a wrong value for a is a behavioural difference
    assert r["rollup"]["verdict"] == "different"


def test_consumed_value_difference_is_behavioural_unconsumed_is_not():
    tasks = {"a": {"state": "success", "xcom": {"return_value": "1"}}, "b": {"state": "success", "xcom": {"return_value": "5"}}}
    tm = {"a": {"mode": "replaced", "asset": "d/a"}, "b": {"mode": "replaced", "asset": "d/b"}}
    st = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"d/a": {"deps": [], "retry": [], "pool": []}, "d/b": {"deps": ["d/a"], "retry": [], "pool": []}}}
    only_b = route(assets={"d/a": {"state": "success", "value": "1"}, "d/b": {"state": "success", "value": "6"}}, structure=st)
    r = B.compare(SPEC, af(tasks=tasks), af(tasks=tasks), only_b, tm, "rewrite")
    assert r["dimensions"]["values"]["verdict"] == B.DIFFER and r["rollup"]["verdict"] == "equivalent-with-differences"      # b's value is consumed by nothing
    a_wrong = route(assets={"d/a": {"state": "success", "value": "9"}, "d/b": {"state": "success", "value": "5"}}, structure=st)
    r = B.compare(SPEC, af(tasks=tasks), af(tasks=tasks), a_wrong, tm, "rewrite")
    assert r["rollup"]["verdict"] == "different"                                            # b reads a


def test_baseline_failed_and_matches_baseline_failure_are_separate_verdicts():
    tasks = {"a": {"state": "failed", "xcom": {}}, "b": {"state": "upstream_failed", "xcom": {}}}
    tm = {"a": {"mode": "replaced", "asset": "d/a"}, "b": {"mode": "replaced", "asset": "d/b"}}
    st = {"jobs": ["d"], "schedules": [], "sensors": [], "assets": {"d/a": {"deps": [], "retry": [], "pool": []}, "d/b": {"deps": ["d/a"], "retry": [], "pool": []}}}
    same = route(state="failed", assets={"d/a": {"state": "failed"}, "d/b": {"state": "not_run"}}, structure=st)
    r = B.compare(SPEC, af("failed", tasks, errors=["ValueError: by design"]), af("failed", tasks), same, tm, "rewrite")
    assert r["rollup"]["verdict"] == "matches-baseline-failure"
    r = B.compare(SPEC, af("failed", tasks, errors=["ModuleNotFoundError: No module named 'x'"]), af("failed", tasks), same, tm, "rewrite")
    assert r["rollup"]["verdict"] == "baseline-failed"
    swallowed = route(state="success", assets={"d/a": {"state": "success"}, "d/b": {"state": "success"}}, structure=st)
    r = B.compare(SPEC, af("failed", tasks, errors=["ValueError: by design"]), af("failed", tasks), swallowed, tm, "rewrite")
    assert r["rollup"]["verdict"] == "different" and r["dimensions"]["failure"]["verdict"] == B.DIFFER


def test_header_accounting_resolves_assets_and_modes():
    doc = "a: replaced by asset `d/alpha`.\nb: folded into `alpha`.\nc: dropped: an EmptyOperator.\n"
    m = B.parse_header(doc, ["a", "b", "c", "x"], ["d/alpha", "d/beta"], {"d/alpha": "alpha_fn"})
    assert m["a"] == {"mode": "replaced", "asset": "d/alpha"}
    assert m["b"] == {"mode": "folded", "asset": "d/alpha"}
    assert m["c"]["mode"] == "dropped" and m["x"] == {"mode": None, "asset": None}


# ---- end to end, against real Airflow ------------------------------------------------------------------------------------------------------
AF_PY = os.environ.get("EVAL_AIRFLOW_PYTHON")
needs_airflow = pytest.mark.skipif(not AF_PY, reason="set EVAL_AIRFLOW_PYTHON to an interpreter with Airflow 2 (and the sqlite, http and amazon providers, moto) to run the end-to-end harness tests")


def spec_of(dag_id):
    import dag_spec
    d = next(x for x in IR["dags"] if x["dag_id"] == dag_id)
    return json.loads(json.dumps(dag_spec.build(d, None, None, None, IR["airflow_version"]), default=str))


def write_project(root, name, template, **vals):
    text = (FX / template).read_text()
    for k, v in vals.items():
        text = text.replace(f"@{k}@", v)
    pkg = root / "src" / name
    (pkg / "defs").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "defs" / "__init__.py").write_text("")
    mod = "fixture_fails" if "fails" in template else "fixture_pipeline"
    (pkg / "defs" / f"{mod}.py").write_text(text)
    jobs = "[m.fixture_fails]" if mod == "fixture_fails" else "[m.fixture_pipeline]"
    scheds = "[]" if mod == "fixture_fails" else "[m.fixture_pipeline_schedule]"
    (pkg / "definitions.py").write_text(f"import dagster as dg\nfrom {name}.defs import {mod} as m\ndefs = dg.Definitions(assets=m.ASSETS, jobs={jobs}, schedules={scheds})\n")
    return root


GOOD = {"RETRIES": "2", "ROWS": "[[1, 'a'], [2, 'b']]", "COMMAND": "echo hello > report.txt", "INSERTS": "[(1, 'a'), (2, 'b')]", "HTTP": "True", "CRON": "0 6 * * *"}


def run_fixture(tmp_path, tag, dag_id="fixture_pipeline", template="rewrite_template.py", routes=("rewrite",), conv=None, **over):
    import eval_behavior as E
    vals = {**GOOD, **over}
    project = write_project(tmp_path / f"rw_{tag}", "rw_fixture", template, **vals)
    return E.execute(tmp_path / f"out_{tag}", tmp_path / "sandboxes" / tag, spec_of(dag_id), dag_id, FX / "dags" / f"{dag_id}.py", FX, AF_PY, sys.executable, conv, project, list(routes), 120)


@needs_airflow
def test_a_faithful_rewrite_is_equivalent_in_every_dimension_that_can_be_observed(tmp_path):
    res = run_fixture(tmp_path, "good")
    x = res["routes"]["rewrite"]
    assert res["baseline"]["class"][0] == "ok"
    assert x["rollup"]["verdict"] == "equivalent", json.dumps({k: v for k, v in x["dimensions"].items() if v["verdict"] != "match"}, default=str)[:1500]
    d = x["dimensions"]
    for name in ("outcome", "tasks", "values", "files", "sqlite", "http", "smtp", "s3", "schedule", "retries", "dependencies", "job"):
        assert d[name]["verdict"] == "match", (name, d[name])
    assert "cwd/stamp.txt" in d["files"]["evidence"]["nondeterministic_excluded"]           # the timestamp file differs between the baseline's two runs and is excluded
    assert d["files"]["compared_items"] >= 1 and d["pools"]["verdict"] == "not-compared"


@needs_airflow
def test_the_converter_route_runs_through_the_same_harness(tmp_path):
    import subprocess
    out = tmp_path / "conv"
    (out / "src" / "pkg" / "defs").mkdir(parents=True)
    rep = out / "report.json"
    subprocess.run([sys.executable, str(SCRIPTS / "convert_ir.py"), str(FX / "ir.json"), "--out-dir", str(out / "src" / "pkg" / "defs"), "--report", str(rep), "--return-all"], check=True, capture_output=True, cwd=FX)      # the IR names the DAG file relative to the fixtures folder
    entry = json.loads(rep.read_text())["dags"]["fixture_pipeline"]
    res = run_fixture(tmp_path, "conv", routes=("converter",), conv={"src": out / "src", "report": entry})
    x = res["routes"]["converter"]
    assert x["rollup"]["verdict"] in ("equivalent", "equivalent-with-differences"), x["rollup"]
    assert x["dimensions"]["http"]["verdict"] == "match" and x["dimensions"]["sqlite"]["verdict"] == "match"


@needs_airflow
@pytest.mark.parametrize("tag,over,dim,verdict", [
    ("command", {"COMMAND": "echo goodbye > report.txt"}, "files", "different"),                         # a changed command: the file's content differs
    ("nofile", {"COMMAND": "true"}, "files", "different"),                                                # a dropped side effect: the file is not written
    ("nohttp", {"HTTP": "False"}, "http", "different"),                                                   # a dropped request
    ("norows", {"INSERTS": "[]"}, "sqlite", "different"),                                                 # a dropped insert
    ("schedule", {"CRON": "0 7 * * *"}, "schedule", "equivalent-with-differences"),                       # a changed schedule: metadata only
    ("retries", {"RETRIES": "0"}, "retries", "equivalent-with-differences"),                              # lost retries: metadata only
    ("value", {"ROWS": "[[1, 'a']]"}, "values", "equivalent-with-differences"),                           # a different value that nothing consumes
])
def test_broken_variants_of_a_rewrite_are_flagged(tmp_path, tag, over, dim, verdict):
    x = run_fixture(tmp_path, tag, **over)["routes"]["rewrite"]
    assert x["dimensions"][dim]["verdict"] == "differ", x["dimensions"][dim]
    assert x["rollup"]["verdict"] == verdict, x["rollup"]
    assert x["rollup"]["differing"] == [dim]                                                             # and nothing else is blamed


@needs_airflow
def test_a_task_the_header_does_not_account_for_is_flagged(tmp_path):
    import eval_behavior as E
    project = write_project(tmp_path / "rw_hdr", "rw_fixture", "rewrite_template.py", **GOOD)
    f = project / "src" / "rw_fixture" / "defs" / "fixture_pipeline.py"
    f.write_text(f.read_text().replace("t_load: replaced by asset `fixture_pipeline/loaded`.\n", ""))
    res = E.execute(tmp_path / "o", tmp_path / "sandboxes" / "hdr", spec_of("fixture_pipeline"), "fixture_pipeline", FX / "dags" / "fixture_pipeline.py", FX, AF_PY, sys.executable, None, project, ["rewrite"], 120)
    t = res["routes"]["rewrite"]["dimensions"]["tasks"]
    assert t["verdict"] == "differ" and any(d["task"] == "t_load" for d in t["evidence"]["differences"])


@needs_airflow
def test_failure_by_design_matches_and_a_swallowed_failure_is_different(tmp_path):
    kw = dict(dag_id="fixture_fails", template="rewrite_fails_template.py")
    same = run_fixture(tmp_path, "fails_same", FAIL="True", **kw)
    assert same["baseline"]["class"][0] == "by-design-failure"
    x = same["routes"]["rewrite"]
    assert x["rollup"]["verdict"] == "matches-baseline-failure", json.dumps({k: v for k, v in x["dimensions"].items() if v["verdict"] == "differ"}, default=str)[:800]
    assert x["dimensions"]["failure"]["verdict"] == "match" and x["dimensions"]["files"]["verdict"] == "match"      # side.txt written, after.txt not
    swallowed = run_fixture(tmp_path, "fails_swallowed", FAIL="False", **kw)["routes"]["rewrite"]
    assert swallowed["rollup"]["verdict"] == "different" and swallowed["dimensions"]["outcome"]["verdict"] == "differ"
    assert swallowed["dimensions"]["files"]["verdict"] == "differ"                                      # after.txt now exists


@needs_airflow
def test_an_environmental_baseline_failure_is_excluded_not_blamed_on_the_route(tmp_path):
    import eval_behavior as E
    res = E.execute(tmp_path / "o", tmp_path / "sandboxes" / "env", spec_of("fixture_env"), "fixture_env", FX / "dags" / "fixture_env.py", FX, AF_PY, sys.executable, None, tmp_path / "none", ["rewrite"], 120)
    assert res["baseline"]["class"][0] == "baseline-failed"
    assert res["routes"]["rewrite"]["rollup"]["verdict"] == "no-output"                                  # no project given


# ---- report ---------------------------------------------------------------------------------------------------------------------------------------------
def fake_run(work, run, verdicts, writers_dirs=None):
    priv = work / f"{run}-private"
    (priv / "behavior").mkdir(parents=True, exist_ok=True)
    (priv / "gates").mkdir(exist_ok=True)
    index = {}
    for i, (conv_v, rew_v, stratum) in enumerate(verdicts):
        name = f"d{i}"
        index[f"r::{name}"] = {"dir": name, "dag_id": name, "project": f"rw_{name}", "module": name, "stratum": stratum, "pattern": "python" if i % 2 else "bash", "repo": "r", "source_file": "x", "local_deps": []}
        base = ["baseline-failed", "env"] if "baseline-failed" in (conv_v, rew_v) else ["ok", "ok"]
        routes = {}
        for rt, v in (("converter", conv_v), ("rewrite", rew_v)):
            if v == "baseline-failed":
                v = "equivalent"
            dims = {"files": {"verdict": "differ", "evidence": {"extra_in_route": ["cwd/x"]}}} if v == "different" else {"files": {"verdict": "match"}}
            routes[rt] = {"rollup": {"verdict": v, "why": "w", "thin": False}, "dimensions": dims}
        (priv / "behavior" / name).mkdir(exist_ok=True)
        (priv / "behavior" / name / "behavior.json").write_text(json.dumps({"routes": routes, "baseline": {"class": base}}))
        (priv / "gates" / f"{name}.gates.json").write_text(json.dumps({"passed": rew_v != "different", "gates": [{"gate": "definitions_load", "passed": True}, {"gate": "generated_tests", "passed": rew_v != "different"}]}))
        (priv / "gates" / f"{name}.report.json").write_text(json.dumps({"attempts": 1 + i % 3, "registry_components": ["x"] if i % 2 else []}))
    (priv / "kit_index.json").write_text(json.dumps(index))


def test_report_counts_intervals_pairs_and_provenance(tmp_path):
    import argparse
    v = ([("equivalent", "equivalent", "clean")] * 5 + [("different", "equivalent", "stubbed")] * 4 + [("no-output", "equivalent-with-differences", "deferred")] * 2
         + [("equivalent", "different", "clean")] * 2 + [("baseline-failed", "baseline-failed", "clean")] * 2 + [("different", "different", "degraded")] * 1)
    fake_run(tmp_path, "r1", v)
    (tmp_path / "costs.json").write_text(json.dumps({"d0": {"tokens": 1000, "tool_uses": 10}, "d1": {"tokens": 3000, "tool_uses": 30}}))
    out = {}
    for writers in ("cold", "designer"):
        a = argparse.Namespace(work=tmp_path, runs=["r1"], writers=writers, costs=tmp_path / "costs.json", variance_run=None, manifest=None, out=None)
        out[writers] = R.render(a)
    t = out["cold"]
    assert "written by fresh agents that did not design the prompts" in t and "NOT cold-worker numbers" in out["designer"]
    assert "written by fresh agents" not in out["designer"].split("## Earlier measurement")[0]
    assert "Evaluated DAGs (sampled minus baseline-failed minus not run): **14** of 16" in t
    assert "| equivalent | 7 | 7/14 = 50% (95% CI 27-73%) |" in t
    assert "| baseline-failed (excluded from the shares) | 2 | |" in t
    assert "| both routes | rewrite only | converter only | neither |" in t
    assert "Discordant pairs: 8" in t                                                          # rewrite-only: 4 (diff conv) + 2 (no-output conv); converter-only: 2
    assert "`d15`" in t.split("## Every `different`")[1].split("## Baseline-failed")[0]
    assert "No second pass was run: variance is **not measured**" in t
    assert "2000.0 / 2000.0" in t                                                              # tokens mean / median of the two supplied costs
    assert "baseline-failed" in t and "## What was compared, and what could not be" in t


def test_report_with_no_behaviour_results_says_nothing_was_measured(tmp_path):
    import argparse
    fake_run(tmp_path, "r1", [])
    a = argparse.Namespace(work=tmp_path, runs=["r1"], writers="cold", costs=None, variance_run=None, manifest=None, out=None)
    assert "nothing below has been measured (not run)" in R.render(a)


def test_report_variance_section_pairs_two_independent_rewrites(tmp_path):
    import argparse
    fake_run(tmp_path, "r1", [("equivalent", "equivalent", "clean"), ("equivalent", "different", "clean")])
    fake_run(tmp_path, "r1b", [("equivalent", "equivalent", "clean"), ("equivalent", "equivalent", "clean")])
    a = argparse.Namespace(work=tmp_path, runs=["r1"], writers="cold", costs=None, variance_run="r1b", manifest=None, out=None)
    t = R.render(a)
    assert "2 DAGs have a second independent rewrite" in t and "Same behaviour verdict in both passes: 1/2 = 50%" in t
