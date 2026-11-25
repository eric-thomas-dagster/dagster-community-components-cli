"""The rewrite route: dag_spec.py (facts only), rewrite_prompt.py, accept_rewrite.py (the gates and the manifest record), run on the two real DAGs in examples/rewrite."""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
import dag_spec as DS  # noqa: E402
import rewrite_prompt as RP  # noqa: E402

EX = SCRIPTS.parent / "examples" / "rewrite"
IR = json.loads((EX / "ir.json").read_text())
REPORT = json.loads((EX / "report.json").read_text())
PARITY = json.loads((EX / "parity_airflow.json").read_text())
dagster = pytest.importorskip("dagster")


def spec_of(dag_id, **kw):
    d = next(x for x in IR["dags"] if x["dag_id"] == dag_id)
    return DS.build(d, REPORT, None, PARITY, IR["airflow_version"], **kw) if False else DS.build(d, REPORT, None, PARITY, IR["airflow_version"])


def test_spec_is_deterministic_and_the_committed_one_is_current():
    for dag_id in ("simple_sequential_pipeline", "partner_ingest"):
        a, b = spec_of(dag_id), spec_of(dag_id)
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
        d = next(x for x in IR["dags"] if x["dag_id"] == dag_id)
        committed = json.loads((EX / "specs" / f"{dag_id}.spec.json").read_text())
        manifest = json.loads((EX / "manifest.json").read_text())
        assert committed == json.loads(json.dumps(DS.build(d, REPORT, manifest, PARITY, IR["airflow_version"]), sort_keys=True, default=str))
        assert (EX / "specs" / f"{dag_id}.spec.md").read_text() == DS.render_md(DS.build(d, REPORT, manifest, PARITY, IR["airflow_version"]))


def test_spec_holds_the_facts_the_rewrite_needs():
    s = spec_of("partner_ingest")
    assert s["schedule"]["cron"] == "0 */6 * * *" and s["schedule"]["catchup"] is False and s["connections"] == ["partner_api"]       # a connection read inside a callable is found, by name
    tasks = {t["task_id"]: t for t in s["tasks"]}
    assert "def _export_ready" in tasks["wait_for_export"]["callable_arg_sources"]["response_check"]["source"]             # the logic of a function argument is in the spec
    assert "BaseHook.get_connection" in tasks["fetch_export"]["callable"]["source"] and tasks["fetch_export"]["retries"] == 2
    assert sorted(s["converter"]["stubbed"]) == ["fetch_export", "wait_for_export"] and s["parity_baseline"] is None
    assert spec_of("simple_sequential_pipeline")["parity_baseline"]["tasks"]["save_results"]["value"] == "'Saved 5 items from api'"
    assert s["must_preserve"] and s["acceptable_differences"] and all("inferred" in p for p in s["purpose"])


def test_spec_contains_no_secret_value():
    d = copy.deepcopy(next(x for x in IR["dags"] if x["dag_id"] == "partner_ingest"))
    t = d["tasks"][0]
    t["args"] = {**(t.get("args") or {}), "password": "FAKE-SPEC-PASSWORD-123", "api_token": "FAKE-SPEC-TOKEN-456", "http_conn_id": "partner_api"}
    t["callable"] = {"name": "f", "params": [], "source": "def f():\n    api_key = 'FAKE-SOURCE-KEY-789'\n    return api_key\n"}
    s = DS.build(d, None, None, None, "2.10.5")
    text = json.dumps(s, default=str) + DS.render_md(s)
    for secret in ("FAKE-SPEC-PASSWORD-123", "FAKE-SPEC-TOKEN-456", "FAKE-SOURCE-KEY-789"):
        assert secret not in text
    assert "http_conn_id" in text and "partner_api" in text            # connection ids are names, kept


def test_prompt_tells_claude_to_load_both_skills_search_the_registry_and_pass_the_gates():
    spec = json.loads((EX / "specs" / "partner_ingest.spec.json").read_text())
    md = (EX / "specs" / "partner_ingest.spec.md").read_text()
    p = RP.render(spec, md, "converter_output/partner_ingest.py", "rw_project", "rw_pkg")
    assert p == RP.render(spec, md, "converter_output/partner_ingest.py", "rw_project", "rw_pkg")                      # deterministic
    for need in ("**`dagster-expert`**", "**`dagster-community-components`**", "skills/dagster-community-components/SKILL.md", "reference/demo-workflow.md",
                 'dagster-component search "<stage keywords>" --json', "dagster-component schema <id> --format json", "dg list components --json", "dg check defs", "validation tier",
                 "one asset per data product", "graph_multi_asset", "polars_pipeline", "run-status sensor", "accept_rewrite.py", "accounts for every Airflow task", "fallback", "model effort"):
        assert need in p, need
    assert md in p and "no registry skill" not in p.lower() and "FAKE" not in p
    assert "`replaced by` an asset" in p                                      # which task values parity compares


def project(tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(EX / "project", dst, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    return dst


def accept(tmp_path, dag_id, proj, *extra):
    return subprocess.run([sys.executable, str(SCRIPTS / "accept_rewrite.py"), "--spec", str(EX / "specs" / f"{dag_id}.spec.json"), "--module", f"rw_pkg.defs.{dag_id}", "--project", str(proj),
                           "--out", str(tmp_path / f"{dag_id}.gates.json"), *extra], capture_output=True, text=True)


def test_both_committed_rewrites_pass_every_gate_and_the_manifest_records_the_route(tmp_path):
    proj = project(tmp_path)
    man = json.loads((EX / "manifest.json").read_text())
    (tmp_path / "m.json").write_text(json.dumps(man))
    for dag_id, extra in (("simple_sequential_pipeline", ["--parity", str(EX / "parity_airflow.json")]), ("partner_ingest", [])):
        r = accept(tmp_path, dag_id, proj, "--manifest", str(tmp_path / "m.json"), *extra)
        assert r.returncode == 0, r.stdout + r.stderr
        gates = json.loads((tmp_path / f"{dag_id}.gates.json").read_text())
        assert gates["passed"] and [g["gate"] for g in gates["gates"]] == ["definitions_load", "generated_tests", "asset_checks_present", "parity", "no_secret_values", "no_invented_apis", "tasks_accounted_for"]
    assert "outcome and 1 value(s) match Airflow" in (tmp_path / "simple_sequential_pipeline.gates.json").read_text()
    after = json.loads((tmp_path / "m.json").read_text())
    assert {(u["dag_id"], u.get("route"), u["status"]) for u in after["units"]} == {("simple_sequential_pipeline", "rewrite", "complete"), ("partner_ingest", "rewrite", "complete")}
    assert all(u["gates"]["passed"] for u in after["units"])


def test_a_failing_rewrite_stays_deferred_with_the_failures_and_nothing_is_marked_complete(tmp_path):
    proj = project(tmp_path)
    mod = proj / "src" / "rw_pkg" / "defs" / "partner_ingest.py"
    src = (EX / "project" / "src" / "rw_pkg" / "defs" / "partner_ingest.py").read_text()
    src = src.replace("- remove_tmp: dropped:", "- (not mentioned):").replace('cron_schedule="0 */6 * * *"', 'cron_schedule="0 1 * * *"')
    src = src.replace("import requests", "import requests\nfrom dagster import ThisAssetDoesNotExist  # noqa: F401\n\nAPI_TOKEN = 'FAKE-LITERAL-TOKEN-0000'")
    mod.write_text(src)
    man = json.loads((EX / "manifest.json").read_text())
    (tmp_path / "m.json").write_text(json.dumps(man))
    r = accept(tmp_path, "partner_ingest", proj, "--manifest", str(tmp_path / "m.json"))
    assert r.returncode == 1
    gates = {g["gate"]: g for g in json.loads((tmp_path / "partner_ingest.gates.json").read_text())["gates"]}
    assert not gates["definitions_load"]["passed"] and not gates["no_secret_values"]["passed"] and not gates["no_invented_apis"]["passed"] and not gates["tasks_accounted_for"]["passed"]
    assert "ThisAssetDoesNotExist" in gates["no_invented_apis"]["detail"] and "remove_tmp" in gates["tasks_accounted_for"]["detail"] and "FAKE-LITERAL" not in r.stdout        # failures never echo the secret
    units = [u for u in json.loads((tmp_path / "m.json").read_text())["units"] if u["dag_id"] == "partner_ingest"]
    assert units and all(u["status"] == "deferred" and u["route"] == "rewrite" and "rewrite failed its gates" in u["reason"] and "converter output remains the fallback" in u["reason"] for u in units)


def test_a_rewrite_that_fails_does_not_undo_a_unit_the_converter_completed(tmp_path):
    import accept_rewrite as AR
    man = {"units": [{"id": "A1", "kind": "task", "dag_id": "d", "task_id": "t", "status": "complete", "route": "converter", "evidence": "gates passed", "reason": ""}]}
    AR.record(man, "d", [{"gate": "definitions_load", "passed": False, "detail": "boom"}])
    u = man["units"][0]
    assert u["status"] == "complete" and u["route"] == "converter" and u["rewrite_attempt"]["passed"] is False and "definitions_load: boom" in u["rewrite_attempt"]["failures"][0]


def test_apply_conversion_marks_converter_units_with_their_route():
    import apply_conversion
    man = json.loads((EX / "manifest.json").read_text())
    assert {u.get("route") for u in man["units"]} == {"converter"}
