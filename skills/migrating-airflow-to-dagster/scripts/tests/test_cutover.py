"""cutover.py: one checklist per converted DAG, from the manifest, the report, the IR and the parity state. The script must never act: it only writes files."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import cutover as CU  # noqa: E402

EXAMPLE = HERE.parents[1] / "examples" / "migration_plan" / "with_conversion"


def unit(i, kind, dag, task=None, status="complete", reason=""):
    u = {"id": f"U{i}", "kind": kind, "dag_id": dag, "file": f"{dag}.py", "status": status, "reason": reason, "classification": "MECH", "signals": {}}
    if task:
        u["task_id"] = task
    return u


def manifest(dag="orders", status="complete", task_status="complete"):
    return {"version": 1, "root": ".", "units": [unit(1, "dag", dag, status=status), unit(2, "task", dag, "extract", task_status, "needs a custom operator" if task_status == "deferred" else ""),
                                                 unit(3, "task", dag, "load")]}


def report(dag="orders", **over):
    rep = {"status": "converted", "assets": 2, "schedule": "30 6 * * *", "timezone": "Europe/Paris", "keys": [[dag, "extract"], [dag, "load"]], "notes": [], "caveats": [], "pools": {},
           "resources": ["aws_default"], "snowflake_resources": ["warehouse"], "provider_connections": {"partner_api": "http"}, "cosmos": [], "max_active_runs": None, **over}
    return {"dags": {dag: rep}, "connections": {"partner_api": {"kind": "http", "environment": ["PARTNER_API_BASE_URL", "PARTNER_API_PASSWORD"], "read_by": "the step, when it runs"}}}


def ir_dag(dag="orders", **over):
    d = {"dag_id": dag, "file": "dags/orders.py", "catchup": True, "start_date": "2023-05-01 00:00:00+00:00", "tags": ["finance"], "has_callbacks": True,
         "default_args": {"owner": "data-eng", "email_on_failure": True},
         "timetable": {"kind": "cron_data_interval", "cron": "30 6 * * *", "timezone": "Europe/Paris"},
         "tasks": [{"task_id": "extract", "retries": 3, "owner": "data-eng", "callbacks": ["on_failure_callback"], "sla_s": 3600, "template_fields": {"sql": "select {{ var.value.region }}"},
                    "callable": {"source": "def f():\n    return Variable.get('api_token')"}},
                   {"task_id": "load", "retries": 0, "owner": "data-eng", "callbacks": [], "sla_s": None, "template_fields": {}}], **over}
    return {"dags": [d]}


def run(tmp_path, man, rep, ir=None, *extra):
    for name, obj in (("m.json", man), ("r.json", rep), ("ir.json", ir)):
        if obj is not None:
            (tmp_path / name).write_text(json.dumps(obj))
    args = [sys.executable, str(HERE.parent / "cutover.py"), str(tmp_path / "m.json"), str(tmp_path / "r.json"), "--out-dir", str(tmp_path / "out")]
    if ir is not None:
        args += ["--ir", str(tmp_path / "ir.json")]
    r = subprocess.run([*args, *extra], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_checklist_has_every_section_in_order_and_names_only(tmp_path):
    out = run(tmp_path, manifest(), report(), ir_dag())
    text = (tmp_path / "out" / "cutover-orders.md").read_text()
    heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert [h.split(". ", 1)[1].split(" (")[0] for h in heads if h[3].isdigit()] == [
        "Pause plan", "Catchup and backfill decision", "Secrets and connections moved", "Schedule and timezone check", "Retries, pools and concurrency to apply",
        "NOT converted: needs a manual replacement", "Freshness expectations", "Parity evidence", "On-call handoff", "Rollback", "Decommission"]
    assert "airflow dags pause orders" in text and "dagster schedule start orders_schedule" in text
    assert "`PARTNER_API_BASE_URL`" in text and "`AIRFLOW_VAR_REGION`" in text and "`AIRFLOW_VAR_API_TOKEN`" in text        # names, from templates and from callables
    assert "Variable `region`" in text and "Variable `api_token`" in text
    assert "NOTHING was paused" in out and "airflow dags delete orders" in out


def test_catchup_true_and_timezone_and_callbacks_are_called_out(tmp_path):
    run(tmp_path, manifest(), report(notes=["catchup=True: Airflow runs every interval missed since the start date (2023-05-01); the converted schedule runs from now on."]), ir_dag())
    text = (tmp_path / "out" / "cutover-orders.md").read_text()
    assert "replayed every interval" in text and "does not replay history" in text and "2023-05-01" in text
    assert "Europe/Paris" in text and "| timezone | Europe/Paris | Europe/Paris |" in text
    assert "DAG-level callbacks" in text and "SLA 3600 s" in text and "alerts nobody" in text and "email_on_failure" in text
    assert "`extract` x3" in text and "daily" in text


def test_deferred_units_and_stubs_are_blockers_and_deferred_dags_get_no_checklist(tmp_path):
    rep = report(caveats=[{"task": "extract", "kind": "stub", "reason": "operator X has no conversion yet"}])
    rep["dags"]["gone"] = {"status": "deferred", "reason": "custom timetable"}
    run(tmp_path, manifest(task_status="deferred"), rep, ir_dag())
    text = (tmp_path / "out" / "cutover-orders.md").read_text()
    assert text.index("## BLOCKERS") < text.index("## 1. Pause plan")
    assert "needs a custom operator" in text and "loud stub" in text and "operator X has no conversion yet" in text
    assert not (tmp_path / "out" / "cutover-gone.md").exists()
    index = (tmp_path / "out" / "cutover-index.md").read_text()
    assert "custom timetable" in index and "cutover-orders.md" in index


def test_pools_become_commands_to_print_not_to_run(tmp_path):
    out = run(tmp_path, manifest(), report(pools={"warehouse": 4, "shared": None}, max_active_runs=2), ir_dag())
    text = (tmp_path / "out" / "cutover-orders.md").read_text()
    assert "dagster instance concurrency set warehouse 4" in out and "dagster instance concurrency set shared <slot count of the Airflow pool>" in text
    assert "`airflow/dag_id` = `orders`" in text


def test_parity_evidence_from_a_parity_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    t = lambda s, v="None": {"state": s, "value": v}          # noqa: E731
    (state / "af_x.json").write_text(json.dumps({"orders": {"state": "success", "tasks": {"extract": t("success", "1")}}, "other": {"state": "success", "tasks": {"extract": t("success", "1")}}}))
    (state / "dg_x.json").write_text(json.dumps({"orders": {"state": "success", "tasks": {"extract": t("success", "1")}}, "other": {"state": "failure", "tasks": {"extract": t("failed")}}}))
    rep = report()
    rep["dags"]["orders"]["task_keys"] = {"extract": "orders/extract"}
    rep["dags"]["other"] = {**rep["dags"]["orders"], "task_keys": {"extract": "other/extract"}}
    (state / "reports.json").write_text(json.dumps({"x": str(tmp_path / "r.json")}))
    man = manifest()
    man["units"] += [unit(9, "dag", "other")]
    run(tmp_path, man, rep, None, "--parity-state", str(state))
    assert "**identical outcome**" in (tmp_path / "out" / "cutover-orders.md").read_text()
    other = (tmp_path / "out" / "cutover-other.md").read_text()
    assert "**differs**" in other and "airflow success vs dagster failure" in other
    none = run(tmp_path, man, rep)
    assert "**not given**" in (tmp_path / "out" / "cutover-orders.md").read_text() and none


def test_hosted_airflow_command_prefix(tmp_path):
    out = run(tmp_path, manifest(), report(), ir_dag(), "--airflow-cmd", "aws-mwaa-cli --env prod", "--dagster-cmd", "dg")
    assert "aws-mwaa-cli --env prod dags pause orders" in out and "dg schedule start orders_schedule" in out


def test_the_script_acts_on_nothing(tmp_path, monkeypatch):
    man, rep, ir = manifest(), report(), ir_dag()
    for name, obj in (("m.json", man), ("r.json", rep), ("ir.json", ir)):
        (tmp_path / name).write_text(json.dumps(obj))
    before = {n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest() for n in ("m.json", "r.json", "ir.json")}

    def boom(*a, **k):
        raise AssertionError("cutover.py must not run a process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    assert CU.main([str(tmp_path / "m.json"), str(tmp_path / "r.json"), "--ir", str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["cutover-index.md", "cutover-orders.md"]
    assert {n: hashlib.sha256((tmp_path / n).read_bytes()).hexdigest() for n in before} == before
    src = (HERE.parent / "cutover.py").read_text()
    assert "subprocess" not in src and "os.system" not in src and "shutil" not in src and ".unlink(" not in src and ".rmdir(" not in src        # nothing in the module can act


def test_committed_example_is_current(tmp_path):
    out = tmp_path / "o"
    CU.main([str(EXAMPLE / "manifest.json"), str(EXAMPLE / "report.json"), "--ir", str(EXAMPLE / "ir.json"), "--out-dir", str(out)])
    committed = EXAMPLE / "cutover"
    assert sorted(p.name for p in committed.iterdir()) == sorted(p.name for p in out.iterdir())
    for p in out.iterdir():
        assert (committed / p.name).read_text() == p.read_text()
