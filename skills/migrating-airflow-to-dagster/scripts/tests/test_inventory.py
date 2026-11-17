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
    assert ids == {"nightly_etl", "clean_pipeline", "branchy", "daily_report", "loopy", "groups_and_suffixes", "second", "my_asset"}


def test_classic_dag_schedule_edges_and_signals(manifest):
    d = dag(manifest, "nightly_etl")
    assert d["schedule"]["cron"] == "0 0 * * *" and d["catchup"] is False
    assert d["default_args"]["retries"] == 2
    assert d["edges"] == [["extract", "transform"], ["start", "extract"], ["transform", "load"]]
    assert d["signals"]["airflow_variable"] == 1 and d["signals"]["jinja_template"] >= 1
    assert d["classification"] == "JUDG" and "1 JUDG" in d["classification_reason"]   # the all_done task


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
    assert r.returncode == 0 and "8 DAGs" in r.stdout
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


def test_loop_generated_task_ids_are_kept_and_flagged(manifest):
    t = task(manifest, "loopy", "f'step_{i}'")
    assert t["dynamic_id"] and t["classification"] == "JUDG" and "computed at parse time" in t["classification_reason"]


def test_branch_variants_are_redesign_and_marker_is_a_task(manifest):
    assert task(manifest, "loopy", "pick")["classification"] == "REDESIGN"
    assert task(manifest, "loopy", "marker")["operator"] == "ExternalTaskMarker"


def test_taskflow_functions_defined_outside_dag_attach_to_calling_dag(manifest):
    ids = {u["task_id"] for u in manifest["units"] if u["kind"] == "task" and u["dag_id"] == "loopy"}
    assert {"produce", "consume"} <= ids
    assert ["produce", "consume"] in dag(manifest, "loopy")["edges"]


def test_dag_reflects_its_tasks(manifest):
    d = dag(manifest, "loopy")
    assert d["classification"] == "JUDG" and d["task_classes"]["REDESIGN"] == 1
    assert dag(manifest, "nightly_etl")["task_classes"]["JUDG"] == 1


def test_taskflow_argument_passing_is_implicit_xcom(manifest):
    t = task(manifest, "clean_pipeline", "summarize")
    assert t["implicit_xcom"] and t["classification"] == "JUDG" and "implicit XCom" in t["classification_reason"]
    assert not task(manifest, "clean_pipeline", "clean_rows")["implicit_xcom"]


def test_dag_level_outlets_are_flagged(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("from airflow import Dataset\nfrom airflow.decorators import dag, task\n"
                 "@dag(schedule='@daily', outlets=[Dataset('s3://b/x')])\ndef d():\n    @task\n    def t():\n        return 1\n    t()\n")
    m = inventory.build_manifest(tmp_path, set())
    d = next(u for u in m["units"] if u["kind"] == "dag")
    assert d["classification"] == "JUDG" and "Airflow rejects this" in d["classification_reason"]


def _tasks(m, dag_id):
    return {u["task_id"]: u for u in m["units"] if u["kind"] == "task" and u["dag_id"] == dag_id}


def test_dag_id_from_file_stem_and_reused_variable_names(manifest):
    ids = {u["dag_id"] for u in manifest["units"] if u["kind"] == "dag"}
    assert "groups_and_suffixes" in ids and "second" in ids          # dag_id=Path(__file__).stem; both DAGs bound to `dag`
    assert set(_tasks(manifest, "second")) == {"only"}


def test_task_group_prefixes_and_call_suffixes(manifest):
    t = _tasks(manifest, "groups_and_suffixes")
    assert {"section.t1", "section.t2", "say", "say__1", "renamed"} <= set(t)
    assert {"grp.a", "grp.b", "grp__1.a", "grp__1.b"} <= set(t)       # Airflow's naming for repeated groups
    d = dag(manifest, "groups_and_suffixes")
    assert ["section.t1", "section.t2"] in d["edges"] and ["grp.a", "grp.b"] in d["edges"]


def test_comprehension_and_loop_created_tasks_are_flagged(manifest):
    t = _tasks(manifest, "groups_and_suffixes")
    flag = next(v for k, v in t.items() if k.startswith("f'flag_"))
    assert flag["dynamic_id"] and flag["classification"] == "JUDG"
    assert t["in_loop"]["in_loop"] and "inside a loop" in t["in_loop"]["classification_reason"]
    assert any(e[0] == "section.t2" and e[1].startswith("f'flag_") for e in dag(manifest, "groups_and_suffixes")["edges"])   # chain(t2, *flags)


def test_airflow3_asset_decorator_is_a_dag_with_one_task(manifest):
    assert set(_tasks(manifest, "my_asset")) == {"my_asset"}


def test_reconcile_adds_what_only_airflow_knows():
    import reconcile

    m = {"units": [
        {"id": "A001", "kind": "dag", "dag_id": "d", "file": "d.py"},
        {"id": "A002", "kind": "task", "dag_id": "d", "task_id": "t1", "dynamic_id": False, "in_loop": False, "mapped": False},
        {"id": "A003", "kind": "dag", "dag_id": "ghost", "file": "g.py"},
    ]}
    truth = {"dags": {"d": {"file": "d.py", "tasks": ["t1", "t2"], "schedule": "None"},
                      "factory_made": {"file": "f.py", "tasks": ["x"], "schedule": "@daily"}}, "import_errors": {}}
    stats = reconcile.reconcile(m, truth)
    assert stats["tasks_exact"] == 1 and stats["tasks_added"] == 2 and stats["dags_added"] == 1
    assert stats["static_dags_unconfirmed"] == ["ghost"]
    added = [u for u in m["units"] if u.get("origin") == "airflow_runtime"]
    assert {(u["kind"], u.get("task_id") or u["dag_id"]) for u in added} == {("dag", "factory_made"), ("task", "t2"), ("task", "x")}
    assert all(u["classification"] == "JUDG" and u["status"] == "pending" for u in added)


FACT = HERE / "fixtures_factory"


@pytest.fixture(scope="module")
def fmanifest():
    return inventory.build_manifest(FACT, set())


def test_factory_function_is_a_template_not_a_dag(fmanifest):
    t = next(u for u in fmanifest["units"] if u["kind"] == "dag" and u.get("factory"))
    assert t["factory"] == "build_load_dag" and t["classification"] == "REDESIGN" and "component" in t["classification_reason"]
    assert any(u["kind"] == "taskgroup_template" and u["name"] == "load_group" for u in fmanifest["units"])


def test_factory_calls_are_listed_and_loop_over_dict_is_expanded(fmanifest):
    calls = {u["dag_id"]: u for u in fmanifest["units"] if u["kind"] == "factory_call"}
    assert set(calls) == {"one_off", "gen_customers", "gen_orders"}
    assert calls["one_off"]["config"]["table"] == "T1" and not calls["one_off"]["in_loop"]
    g = calls["gen_orders"]
    assert g["config"] == {"dag_id": "gen_orders", "warehouse": "WH", "schedule": "0 * * * *", "table": "ORDERS"}
    assert "LOADS" in g["expanded_from"]


def test_blueprint_and_dag_factory_yaml(fmanifest):
    kinds = {(u["kind"], u.get("dag_id") or u.get("name")) for u in fmanifest["units"]}
    assert ("blueprint_template", "S3Load") in kinds and ("blueprint_dag", "report_bp") in kinds
    assert ("dag_factory", "orders_dag") in kinds                         # tasks given as a list
    ts = {u["task_id"]: u for u in fmanifest["units"] if u["kind"] == "task" and u["dag_id"] == "orders_dag"}
    assert ts["load"]["upstream"] == ["list_files"]
    bp = {u["task_id"]: u for u in fmanifest["units"] if u["kind"] == "task" and u["dag_id"] == "report_bp"}
    assert bp["publish"]["upstream"] == ["load"] and bp["load"]["operator"] == "blueprint:s3_load"


def test_globals_registration_is_signalled(fmanifest):
    sigs = {k for u in fmanifest["units"] for k in u.get("signals", {})}
    assert "dynamic_dag_generation" in sigs or any(u["kind"] == "factory_call" for u in fmanifest["units"])


def test_dag_id_from_a_variable_is_resolved(fmanifest):
    ids = {u["dag_id"] for u in fmanifest["units"] if u["kind"] == "dag"}
    assert {"stem_var", "named_constant_dag"} <= ids and "dag_id" not in ids      # file-stem variable and a string constant


DFX = HERE / "fixtures_dagfactory"


@pytest.fixture(scope="module")
def dfm():
    return inventory.build_manifest(DFX, set())


def test_dagfactory_default_block_and_defaults_file_are_not_dags(dfm):
    ids = {u["dag_id"] for u in dfm["units"] if u["kind"] == "dag_factory"}
    assert "default" not in ids and "defaults" not in ids
    assert {"module3_dynamic_dag_Australia", "module3_dynamic_dag_Japan", "machine_learning"} <= ids


def test_dagfactory_dict_tasks_use_the_key_as_task_id(dfm):
    ts = {u["task_id"] for u in dfm["units"] if u["kind"] == "task" and u["dag_id"] == "module3_dynamic_dag_Brazil"}
    assert ts == {"task_process_s3_data"}          # Airflow's id is the dict key, not the inner task_id (verified against dag-factory 1.1.0)


def test_identical_dags_form_a_family(dfm):
    fam = [u for u in dfm["units"] if u["kind"] == "dag_family"]
    countries = next(f for f in fam if "module3_dynamic_dag_India" in f["members"])
    assert len(countries["members"]) == 4 and countries["classification"] == "REDESIGN"
    assert "partitioned" in countries["classification_reason"]


def test_unbuildable_dagfactory_yaml_is_flagged(dfm):
    d = next(u for u in dfm["units"] if u["kind"] == "dag_factory" and u["dag_id"] == "machine_learning")
    assert d["unbuildable_tasks"] == ["load"] and "skips the whole file" in d["classification_reason"]


def test_loader_call_is_recorded(dfm):
    ld = next(u for u in dfm["units"] if u["kind"] == "dagfactory_loader")
    assert ld["call"] == "load_yaml_dags" and "dags_folder" in ld["config"]
