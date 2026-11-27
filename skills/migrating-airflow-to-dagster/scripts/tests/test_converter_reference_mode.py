"""Converter output as reference: the prompt modes, the arm C kit, the by-design ranking, the XCom-serialization baseline class."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent / "parity"), str(HERE)]

import eval_kit as K  # noqa: E402
import rewrite_prompt as RP  # noqa: E402
import verdict_snapshot as V  # noqa: E402
from parity import behavior_compare as B  # noqa: E402
from test_eval_harness import make_kit, work  # noqa: E402,F401  (the kit fixture)

SPEC = {"dag_id": "partner_ingest", "tasks": [{"task_id": "fetch_export", "operator": "PythonOperator"}, {"task_id": "load", "operator": "PythonOperator"}, {"task_id": "wait_for_export", "operator": "S3KeySensor"}],
        "converter": {"status": "partial", "stubbed": ["fetch_export"], "asset_checks": []}, "schedule": {}, "retries": {}}
MD = "# spec\n\n## What the converter did (reference draft, not a requirement)\n\n- no conversion report was given\n\n## Parity baseline\n\nnone\n"


def conv_project(root: Path, dag_id="partner_ingest", other=None) -> Path:
    (root / "src" / "pkg" / "defs").mkdir(parents=True)
    dags = {dag_id: {"status": "partial", "reason": "one stub", "shims": ["S3KeySensorShim"], "notes": ["n1"],
                     "caveats": [{"kind": "stub", "task": "fetch_export", "reason": "custom operator"}, {"kind": "degraded", "task": "wait_for_export", "reason": "poke interval ignored"}]}}
    if other:
        dags[other] = {"status": "converted", "caveats": []}
    (root / "report.json").write_text(json.dumps({"dags": dags}))
    (root / "src" / "pkg" / "defs" / f"{dag_id}.py").write_text("# Converted from Airflow DAG\nx = 1\n")
    (root / "src" / "pkg" / "runtime.py").write_text("# runtime\n")
    return root


def test_prompt_modes_differ_default_is_reference(tmp_path):
    cp = RP.converter_start(SPEC, conv_project(tmp_path / "cp"))
    none = RP.render(SPEC, MD, None, "p", "p")
    default = RP.render(SPEC, MD, None, "p", "p", converter_project=cp)
    ref = RP.render(SPEC, MD, None, "p", "p", converter_project=cp, converter_mode="reference")
    start = RP.render(SPEC, MD, None, "p", "p", converter_project=cp, converter_mode="start")
    assert default == ref and len({none, ref, start}) == 3
    assert "read-only reference" in ref and "do not inherit its structure" in ref.lower().replace("do not copy the converter's project into yours and do not inherit its structure", "do not inherit its structure")
    assert "no shims everywhere, no task-per-asset" in ref and "list of hard parts" in ref and "STUB `fetch_export`" in ref and "what you took from the converter" in ref
    assert "tasks the converter converted without a stub or caveat: load" in ref
    assert "**Start from it**" in start and "**Start from it**" not in ref and "TODO list" in start
    assert "Start from it" not in none and "No converter output is given" in none
    assert RP.render(SPEC, MD, None, "p", "p") == none                                   # no converter project: unchanged
    print(f"prompt sizes: none {len(none)}, reference {len(ref)}, start {len(start)} characters")


def test_cli_converter_mode_default(tmp_path):
    (tmp_path / "s.spec.json").write_text(json.dumps(SPEC))
    (tmp_path / "s.spec.md").write_text(MD)
    conv_project(tmp_path / "cp")
    RP.main([str(tmp_path / "s.spec.json"), "--converter-project", str(tmp_path / "cp"), "--out-dir", str(tmp_path / "o1")])
    RP.main([str(tmp_path / "s.spec.json"), "--converter-project", str(tmp_path / "cp"), "--converter-mode", "start", "--out-dir", str(tmp_path / "o2")])
    assert "read-only reference" in (tmp_path / "o1" / "partner_ingest.prompt.md").read_text()
    assert "**Start from it**" in (tmp_path / "o2" / "partner_ingest.prompt.md").read_text()


# ---- arm C kit ---------------------------------------------------------------------------------------------------------------------------------------
def kit_c(work, tmp_path, arm_of=lambda dag: "C"):
    import eval_common as C
    sample = json.loads((work / "sample.json").read_text())
    for e in sample["dags"]:
        e["arm"] = arm_of(e["dag_id"])
    (work / "sample.json").write_text(json.dumps(sample))
    conv = tmp_path / "conv"
    for e in sample["dags"]:
        conv_project(conv / f"fixrepo.2.ra_{e['dag_id']}", e["dag_id"], other="some_other_dag")
        (conv / f"fixrepo.2.ra_{e['dag_id']}" / "report.json").rename(conv / f"fixrepo.2.ra_{e['dag_id']}" / "report.json")
    a = argparse.Namespace(work=work, manifest=work / "manifest.json", sample=work / "sample.json", run="kit", dcc_skill="/example/skills/dagster-community-components", converter_dir=conv)
    return K.make(a), sample


def test_arm_c_kit_holds_the_converter_reference_and_checks_clean(work, tmp_path):
    rc, sample = kit_c(work, tmp_path, lambda d: "C" if d == "fixture_pipeline" else "A")
    d = work / "kit" / "fixture_pipeline"
    assert rc == 0
    ref = d / "reference" / "converter"
    assert json.loads((ref / "report.json").read_text())["dags"].keys() == {"fixture_pipeline"}        # only its own DAG's entry
    assert (ref / "src" / "pkg" / "defs" / "fixture_pipeline.py").exists() and (ref / "src" / "pkg" / "runtime.py").exists()
    assert not any(f.stat().st_mode & 0o222 for f in ref.rglob("*") if f.is_file())
    task = (d / "TASK.md").read_text()
    assert "This arm (C) differs" in task and "reference/converter/" in task and "there IS converter output" in task and "Do NOT read" not in task.split("2. ")[1].split("3. ")[0][:200]
    assert "read-only reference (section 6)" in (d / "prompt.md").read_text() and "`reference/converter`" in (d / "prompt.md").read_text()
    other = work / "kit" / "fixture_fails"                                                              # the cold arm A kit: default text, no reference
    assert not (other / "reference").exists() and "any converter output" in (other / "TASK.md").read_text() and "There is no converter output to read" in (other / "TASK.md").read_text()
    index = json.loads((work / "kit-private" / "kit_index.json").read_text())
    dcc = "/example/skills/dagster-community-components"
    assert K.check_all(work, "kit", index, dcc) == []
    assert K.check(argparse.Namespace(work=work, run="kit")) == 0


def test_kit_check_still_rejects_converter_output_in_cold_arms(work, tmp_path):
    kit_c(work, tmp_path, lambda d: "C" if d == "fixture_pipeline" else "A")
    index = json.loads((work / "kit-private" / "kit_index.json").read_text())
    dcc = "/example/skills/dagster-community-components"
    cold = work / "kit" / "fixture_fails"
    (cold / "reference" / "converter").mkdir(parents=True)
    (cold / "reference" / "converter" / "report.json").write_text("{}")
    probs = K.check_all(work, "kit", index, dcc)
    assert any("cold arm A" in p for p in probs) and any("converter output" in p for p in probs)
    # and arm C itself is rejected when it holds another DAG's entry or is writable
    import shutil
    shutil.rmtree(cold / "reference")
    c = work / "kit" / "fixture_pipeline" / "reference" / "converter"
    (c / "report.json").chmod(0o644)
    (c / "report.json").write_text(json.dumps({"dags": {"fixture_pipeline": {}, "fixture_fails": {}}}))
    probs = K.check_all(work, "kit", index, dcc)
    assert any("exactly this DAG" in p for p in probs) and any("not read-only" in p for p in probs)


# ---- ranking and baseline class ----------------------------------------------------------------------------------------------------------------------
def test_matches_baseline_failure_tops_the_ranking_when_the_baseline_fails_by_design():
    assert V.rank("matches-baseline-failure", "by-design-failure") > V.rank("equivalent")
    assert V.rank("matches-baseline-failure", "ok") == V.RANK["matches-baseline-failure"] < V.rank("equivalent")
    assert V.rank("stub-matches-failure", "by-design-failure") < V.rank("matches-baseline-failure", "by-design-failure")
    snap = {"d": {"verdict": "equivalent-with-differences", "thin": False, "baseline": "by-design-failure", "converter": None}}
    cur = {"d": {"verdict": "matches-baseline-failure", "thin": False, "baseline": "by-design-failure", "converter": None}}
    r = V.compare(snap, cur)
    assert not r["regressions"] and len(r["improvements"]) == 1
    r = V.compare(cur, snap)
    assert len(r["regressions"]) == 1
    cur2 = {"d": {"verdict": "stub-matches-failure", "thin": False, "baseline": "by-design-failure", "converter": None}}
    assert len(V.compare(cur, cur2)["regressions"]) == 1


def test_xcom_serialization_failure_on_the_airflow_side_is_a_baseline_problem():
    err = "xcom read failed: AttributeError: module 'airflow.serialization.serializers.deltalake' has no attribute 'stringify'"
    for state in ("failed", "success"):
        cls, why = B.baseline_class({"state": state, "errors": [err], "tasks": {"t": {"state": state}}}, None, {"tasks": [{"task_id": "t", "operator": "PythonOperator"}]})
        assert cls == "baseline-failed" and "xcom-serialization" in why and "deltalake" in why
    assert B.baseline_class({"state": "failed", "errors": ["xcom read failed: ValueError: boom"], "tasks": {}}, None)[0] == "by-design-failure"
