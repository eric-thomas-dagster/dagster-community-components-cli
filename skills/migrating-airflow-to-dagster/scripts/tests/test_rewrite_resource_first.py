"""The rewrite prompt after stage 2a: resource-first guidance (a compact registry-resource table only for the service-operator families the DAG uses), the registry-search checklist, the optional converter
starting point, the spec-by-reference option, and the 0.10.194 `conditional_asset` facts the pattern docs state (run against the registry venv when one is available)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import rewrite_patterns as RPAT  # noqa: E402
import rewrite_prompt as RP  # noqa: E402
import rewrite_resources as RR  # noqa: E402


def task(tid, op, **kw):
    return {"task_id": tid, "operator": op, "upstream": [], "downstream": [], "retries": 0, "trigger_rule": "all_success", "reads_value_from": [], **kw}


SERVICE = {"dag_id": "svc", "schedule": {}, "converter": None, "tasks": [
    task("call_api", "HttpOperator"), task("mail", "EmailOperator"), task("pay", "FivetranOperator"), task("run", "BashOperator"), task("cloud", "DbtCloudRunJobOperator"), task("done", "EmptyOperator")]}
PLAIN = {"dag_id": "plain", "schedule": {}, "converter": None, "tasks": [task("a", "PythonOperator"), task("b", "EmptyOperator")]}
TABLE_HEAD = "Registry resources for this DAG's service operators"


def prompt(spec, **kw):
    return RP.render(spec, "SPEC-MD-BODY\n", None, "p", "p", **kw)


def test_the_resource_table_names_only_the_families_the_dag_uses():
    assert set(RR.families(SERVICE)) == {"http", "smtp", "dbt_cloud", "fivetran"}
    p = prompt(SERVICE)
    assert TABLE_HEAD in p and "`http_resource`" in p and "`smtp_resource`" in p and "`dbt_cloud_resource`" in p
    for absent in ("slack_resource", "ssh_resource", "lambda_resource", "databricks_jobs_resource", "athena_resource", "s3_bucket_resource", "dbt_command_resource"):
        assert absent not in p, absent
    # the document's warnings are carried
    assert "do not map yet" in p and "endpoint mismatch" in p                                   # Fivetran
    assert "disables the job's cron schedule" in p                                              # dbt Cloud
    assert "dagster-component info" in p and "fall back to plain Python" in p                   # the installed templates may be older than 0.10.194
    q = prompt(PLAIN)
    assert TABLE_HEAD not in q and "http_resource" not in q and "smtp_resource" not in q


def test_the_resource_first_rule_is_in_every_prompt():
    for spec in (SERVICE, PLAIN):
        p = prompt(spec)
        assert "Service calls are resources, not assets" in p and "do not hand-roll a client" in p and "ConfigurableResource" in p
        assert "native Dagster forms first" in p and "Decision record:" in p                     # the safety rules stay
        assert "no secret" in p.lower() or "Secrets: environment variables" in p


def test_hook_use_in_a_callable_also_selects_the_family():
    spec = {"dag_id": "h", "schedule": {}, "tasks": [task("t", "_PythonDecoratedOperator", callable={"name": "f", "source": "def f():\n    h = SlackWebhookHook(slack_webhook_conn_id='x')\n"})]}
    assert RR.families(spec) == ["slack"] and "`slack_resource`" in prompt(spec)


def test_every_resource_id_in_the_table_is_in_the_registry_reference():
    doc = SKILL / "reference" / "registry-resources-0.10.194.md"
    if not doc.exists():
        pytest.skip("reference/registry-resources-0.10.194.md is not present")
    text = doc.read_text()
    for fid, f in RR.FAMILIES.items():
        for rid in re.findall(r"[a-z0-9_]{6,}", f["resource"]):
            assert rid in text, (fid, rid)
        if f.get("no_map"):
            assert "do not map yet" in text and "do not map yet" in f["gap"]


def test_the_registry_search_checklist_is_one_line_per_stage_and_keeps_the_decision_record_gate_happy():
    p = prompt(SERVICE)
    assert "Registry search lines" in p and 'searched "<terms>"' in p and "fallback to plain Python" in p
    ck = RR.checklist_text(SERVICE, RPAT.detect(SERVICE))
    assert "HTTP (resource)" in ck and "`http_resource`" in ck and "shell/bash command" in ck and "`shell_command_asset`" in ck and "Fivetran" in ck
    assert "EmptyOperator" not in ck                                                              # a no-op has nothing to search
    spec = {"dag_id": "b", "schedule": {}, "tasks": [task("pick", "BranchPythonOperator"), task("go", "BashOperator", retries=2)]}
    ck2 = RR.checklist_text(spec, RPAT.detect(spec))
    assert "branch / short circuit" in ck2 and "`conditional_asset`" in ck2
    # a header with the decision record and the registry lines as a separate paragraph passes the existing gate unchanged
    doc = ('"""b.\n\nDecision record:\n- skip/branch: preserved\n- retries: preserved\n- failure semantics: preserved\n\n'
           'Registry search:\n- HTTP (resource): searched "http request": found http_resource; used\n- branch: searched "conditional branch": found conditional_asset; not used: native form keeps the branch states\n"""')
    ok, detail, declared = RPAT.check_record(spec, doc)
    assert ok, detail
    assert "http" not in " ".join(declared["preserved"]).lower()


def converter_dir(tmp_path):
    d = tmp_path / "conv"
    (d / "src" / "pkg" / "defs").mkdir(parents=True)
    (d / "src" / "pkg" / "defs" / "svc.py").write_text("# converter output\n")
    (d / "src" / "pkg" / "runtime.py").write_text("# runtime\n")
    (d / "report.json").write_text(json.dumps({"dags": {"svc": {"status": "converted", "caveats": [
        {"task": "pay", "kind": "stub", "reason": "FivetranOperator has no translation"}, {"task": "call_api", "kind": "degraded", "reason": "response_check was not mapped"}],
        "notes": ["the copied code imports the project's own module(s) ['include']"], "shims": ["EmailOperator"], "local_modules": ["include"], "requirements": ["requests"]}}}))
    return d


def test_the_converter_starting_point_is_optional_and_lists_the_report_as_the_todo(tmp_path):
    default = prompt(SERVICE)
    assert default == RP.render(SERVICE, "SPEC-MD-BODY\n", None, "p", "p", converter_project=None)          # default off: nothing changes
    assert "TODO from the converter's report" not in default and "Work from the spec alone" in default
    cdir = converter_dir(tmp_path)
    cp = RP.converter_start(SERVICE, cdir)
    assert cp["module"].endswith("defs/svc.py") and [c["task"] for c in cp["stubs"]] == ["pay"] and [c["task"] for c in cp["degraded"]] == ["call_api"] and cp["runtime"]
    p = prompt(SERVICE, converter_project=cp)
    assert "TODO from the converter's report" in p and "STUB `pay`: FivetranOperator has no translation" in p and "DEGRADED `call_api`: response_check was not mapped" in p
    assert "Start from it" in p and "starting point" in p and "Work from the spec alone" not in p
    assert "include" in p and "gates and the behaviour comparison" in p
    assert "Registry resources for this DAG's service operators" in p and "Service calls are resources, not assets" in p      # the rules stay in this variant
    assert RP.converter_start({"dag_id": "other"}, cdir) is None


def test_cli_writes_both_variants(tmp_path):
    spec = tmp_path / "svc.spec.json"
    spec.write_text(json.dumps(SERVICE))
    (tmp_path / "svc.spec.md").write_text("# Specification\n\n## What the converter did (reference draft, not a requirement)\n\n- no conversion report was given\n\n## Parity baseline\n\n- none\n")
    out1, out2 = tmp_path / "a", tmp_path / "b"
    assert RP.main([str(spec), "--out-dir", str(out1)]) == 0
    assert RP.main([str(spec), "--out-dir", str(out2), "--converter-project", str(converter_dir(tmp_path)), "--spec-ref", "spec.md"]) == 0
    a, b = (out1 / "svc.prompt.md").read_text(), (out2 / "svc.prompt.md").read_text()
    assert "no conversion report was given" not in a                                                  # the empty stub is not repeated
    assert "TODO from the converter's report" in b and "the file `spec.md` next to this prompt" in b and "# Specification" not in b and "# Specification" in a


def test_the_converter_section_of_a_real_spec_is_not_repeated_next_to_the_todo():
    md = "# S\n\n## What the converter did (reference draft, not a requirement)\n\n- status: converted\n- tasks it stubbed: pay\n\n## Parity baseline\n\n- x\n"
    assert "tasks it stubbed: pay" in RP.trim_spec(md)
    assert "tasks it stubbed" not in RP.trim_spec(md, drop_converter=True) and "## Parity baseline" in RP.trim_spec(md, drop_converter=True)


def test_the_long_native_forms_paragraph_and_the_state_table_follow_the_detected_patterns():
    assert "can_subset" in prompt({"dag_id": "s", "schedule": {}, "tasks": [task("s", "ShortCircuitOperator")]}) and "can_subset" not in prompt(PLAIN)
    xcom = {"dag_id": "x", "schedule": {}, "tasks": [task("p", "PythonOperator", reads_value_from=["q"])]}
    p = prompt(xcom)
    assert "| XCom |" in p and "mapped task with 0 instances" not in p and "ShortCircuit task itself" not in p
    q = prompt({"dag_id": "m", "schedule": {}, "tasks": [task("m", "PythonOperator", is_mapped=True)]})
    assert "mapped task with 0 instances" in q


def test_pattern_docs_state_the_conditional_asset_decision_assets_and_index_marks_them():
    b, s = [(SKILL / "reference" / "rewrite-patterns" / n).read_text() for n in ("branching.md", "short_circuit.md")]
    assert "<common parent of the branch keys>/decision" in b and "decision_asset_key" in b and "Duplicate asset key" in b and "native forms above stay the choice" in b
    assert "g/gate_decision" in s and "`passed`" in s and "default when the guarded work needs ops" in s
    idx = json.loads((SKILL / "reference" / "rewrite-patterns" / "index.json").read_text())
    claims = [c["claim"] for p in idx["patterns"] for c in p["claims"] if "conditional_asset" in c["claim"]]
    assert len(claims) == 3 and all(c["status"] == "VERIFIED" for p in idx["patterns"] for c in p["claims"] if "conditional_asset" in c["claim"])


def registry_python():
    cand = os.environ.get("EVAL_REGISTRY194_PYTHON") or str(Path.home() / "airflow-eval-scratch" / "venvs" / "reg194" / "bin" / "python")
    return cand if Path(cand).exists() else None


def test_conditional_asset_0_10_194_decision_assets_run_on_dagster():
    """The claims behind the index.json entries: run `conditional_asset` from dagster-community-components 0.10.194 (skipped without that venv: set EVAL_REGISTRY194_PYTHON)."""
    py = registry_python()
    if not py:
        pytest.skip("no interpreter with dagster-community-components 0.10.194 (set EVAL_REGISTRY194_PYTHON)")
    r = subprocess.run([py, str(HERE / "registry_conditional_asset_probe.py")], capture_output=True, text=True, cwd=str(HERE.parent), timeout=300)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["branch"]["success"] and out["branch"]["materialized"] == ["x/a", "x/decision"] and out["branch"]["metadata"]["x/decision"]["chosen_branch"] == "a"
    assert out["short_false"]["success"] and out["short_false"]["materialized"] == ["g/gate_decision"] and out["short_false"]["metadata"]["g/gate_decision"]["passed"] is False
    assert out["short_true"]["materialized"] == ["g/gate", "g/gate_decision"] and out["short_true"]["metadata"]["g/gate_decision"]["passed"] is True
    assert out["two_branch_explicit"]["success"] and out["two_branch_explicit"]["materialized"] == ["x/a", "x/c", "x/decision1", "x/decision2"]
    assert "Duplicate asset key" in out["two_branch_default"]["error"] and "decision" in out["two_branch_default"]["error"]
