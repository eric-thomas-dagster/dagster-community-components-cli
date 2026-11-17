import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import inventory  # noqa: E402
import status  # noqa: E402

FIX = HERE / "fixtures"


@pytest.fixture(scope="module")
def manifest():
    return inventory.build_manifest(FIX, set())


def dag(m, dag_id):
    return next(u for u in m["units"] if u["kind"] in ("dag", "cosmos", "dag_factory") and u["dag_id"] == dag_id)


def task(m, dag_id, task_id):
    return next(u for u in m["units"] if u["kind"] == "task" and u["dag_id"] == dag_id and u["task_id"] == task_id)


def test_finds_all_dags_and_ignores_non_airflow(manifest):
    ids = {u["dag_id"] for u in manifest["units"] if u["kind"] in ("dag", "cosmos", "dag_factory")}
    assert ids == {"nightly_etl", "clean_pipeline", "branchy", "daily_report"}


def test_classic_dag_schedule_edges_and_signals(manifest):
    d = dag(manifest, "nightly_etl")
    assert d["schedule"]["cron"] == "0 0 * * *" and d["catchup"] is False
    assert d["default_args"]["retries"] == 2
    assert d["edges"] == [["extract", "transform"], ["start", "extract"], ["transform", "load"]]
    assert d["signals"]["airflow_variable"] == 1 and d["signals"]["jinja_template"] >= 1
    assert d["classification"] == "MECH"


def test_classic_task_rules(manifest):
    assert task(manifest, "nightly_etl", "extract")["pool"] == "batch"
    assert task(manifest, "nightly_etl", "extract")["classification"] == "MECH"
    assert task(manifest, "nightly_etl", "start")["classification"] == "MECH"
    load = task(manifest, "nightly_etl", "load")
    assert load["classification"] == "JUDG" and "trigger_rule=all_done" in load["classification_reason"]


def test_taskflow_dataset_dag(manifest):
    d = dag(manifest, "clean_pipeline")
    assert d["schedule"]["kind"] == "dataset" and d["catchup"] is True
    assert d["classification"] == "JUDG"
    assert d["edges"] == [["clean_rows", "summarize"]]
    assert task(manifest, "clean_pipeline", "clean_rows")["retries"] == 3


def test_branching_dag(manifest):
    d = dag(manifest, "branchy")
    assert d["schedule"]["cron"] == "0 6 * * 1-5"
    assert ["wait_for_file", "choose"] in d["edges"] and ["choose", "a"] in d["edges"] and ["b", "fire"] in d["edges"]
    assert task(manifest, "branchy", "choose")["classification"] == "REDESIGN"
    assert task(manifest, "branchy", "per_item")["classification"] == "REDESIGN"
    assert task(manifest, "branchy", "wait_for_file")["classification"] == "JUDG"
    assert task(manifest, "branchy", "fire")["classification"] == "JUDG"


def test_dag_factory_yaml(manifest):
    d = dag(manifest, "daily_report")
    assert d["kind"] == "dag_factory"
    assert task(manifest, "daily_report", "publish")["upstream"] == ["build"]


def test_status_gate(manifest, tmp_path):
    m = json.loads(json.dumps(manifest))
    assert not status.summary(m)[1]
    for u in m["units"]:
        status.advance(m, u["id"], "test") if u["classification"] == "MECH" else status.defer(m, u["id"], "later")
    assert status.summary(m)[1]
    with pytest.raises(ValueError):
        status.defer(m, m["units"][0]["id"], " ")


def test_cli_writes_manifest(tmp_path):
    out = tmp_path / "m.json"
    r = subprocess.run([sys.executable, str(HERE.parent / "inventory.py"), str(FIX), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and "4 DAGs" in r.stdout
    assert json.loads(out.read_text())["units"]


def test_validate_defs_matches_plan():
    import validate_defs

    m = {"units": [
        {"id": "A1", "target": {"assets": ["a", "b"], "deps": {"b": ["a"]}, "jobs": ["j"], "schedules": [{"name": "s", "cron": "0 0 * * *"}]}},
        {"id": "A2", "target": {"none": "no-op join"}},
        {"id": "A3", "target": {}},
        {"id": "A4", "target": {"assets": ["a"], "deps": {"a": ["zzz"]}, "schedules": [{"name": "s", "cron": "1 1 * * *"}]}},
    ]}
    defs = {"assets": [{"asset_key": "a", "dependency_keys": []}, {"asset_key": "b", "dependency_keys": ["a"]}],
            "jobs": [{"name": "j"}], "schedules": [{"name": "s", "cron_schedule": "0 0 * * *"}]}
    passed, unplanned, problems = validate_defs.check(m, defs)
    assert passed == ["A1", "A2"] and unplanned == ["A3"]
    assert any("deps" in p for p in problems) and any("cron" in p for p in problems)
