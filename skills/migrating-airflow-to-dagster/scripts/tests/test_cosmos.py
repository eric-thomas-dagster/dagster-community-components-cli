"""Astronomer Cosmos conversion (cosmos_ops.py and its use in convert_ir.py).

`fixtures_cosmos/ir.json` was extracted by airflow_ir.py from `fixtures_cosmos/dags/cosmos_shop.py` with Airflow 2.10.5 and astronomer-cosmos 1.15.1
(`fixtures_cosmos/regenerate.sh`); the dbt project next to it is real and runs on DuckDB. The tests that load and run need dagster, dagster-dbt, dbt-core
and dbt-duckdb and skip without them. What they do not prove is stated in reference/conversion.md (no Cosmos was run against a warehouse other than DuckDB).
"""
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import cosmos_ops as K  # noqa: E402

FIX = HERE / "fixtures_cosmos"
# the committed IR names the DAG file as /opt/airflow/dags/...; the converter reads the callable's module from that path, so point it at the real file
IR = json.loads((FIX / "ir.json").read_text().replace("/opt/airflow/dags/", f"{FIX}/dags/"))
IR["dags"].sort(key=lambda d: ["shop_mixed", "shop_dbt_dag", "shop_operators"].index(d["dag_id"]))       # the first DAG to use a dbt project keeps the plain asset keys


def ir_with(**changes):
    return copy.deepcopy(IR) | changes


def convert(tmp_path, ir, *extra, pkg="pkg"):
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "{pkg}"\nversion = "0.0.1"\n[tool.dg]\ndirectory_type = "project"\n'
                                             f'[tool.dg.project]\nroot_module = "{pkg}"\ndefs_module = "{pkg}.defs"\n')
    (tmp_path / pkg).mkdir(exist_ok=True)
    (tmp_path / pkg / "__init__.py").write_text("")
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"),
                        "--report", str(tmp_path / "r.json"), *extra], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads((tmp_path / "r.json").read_text())


def dag_of(name, ir=IR):
    return next(d for d in ir["dags"] if d["dag_id"] == name)


# ---- the IR -------------------------------------------------------------------------------------------------------------------------

def test_fixture_ir_holds_cosmos_settings_and_no_paths_or_secrets():
    text = (FIX / "ir.json").read_text()
    assert "/Users/" not in text and "/private/" not in text and "password" not in text.lower()
    mixed = dag_of("shop_mixed")
    g = mixed["cosmos_groups"][0]
    assert g["group"] == "transform" and g["cosmos_type"] == "DbtTaskGroup"
    assert g["render"]["select"] == ["tag:staging", "customer_totals"] and g["render"]["exclude"] == ["stg_customers"] and g["render"]["test_behavior"] == "after_all"
    assert g["dbt_vars"] == {"min_amount": 5} and g["env_names"] == ["SHOP_DUCKDB_PATH"]          # the variable NAME, never its value
    assert g["profile"]["mapping"] is None and g["profile"]["profiles_yml_filepath"].endswith("profiles.yml")
    run = next(t for t in mixed["tasks"] if t["task_id"] == "transform.customer_totals_run")
    assert run["cosmos"]["kind"] == "run" and run["cosmos"]["group_root"] == "transform" and run["cosmos"]["node"]["unique_id"] == "model.shop.customer_totals"
    assert run["operator_class"] is None                                   # Cosmos operators are mapped, not copied
    assert dag_of("shop_dbt_dag")["cosmos_groups"][0]["group"] == ""


def test_node_keys_follow_dagster_dbt_defaults():
    assert K.node_key("model.shop.customer_totals") == ["customer_totals"]
    assert K.node_key("model.shop.orders.v2") == ["orders_v2"]
    assert K.node_key("seed.shop.raw_orders") == ["raw_orders"]
    assert K.node_key("source.shop.raw.orders") == ["raw", "orders"]
    assert K.node_key("test.shop.not_null_x.abc") is None


# ---- planning ----------------------------------------------------------------------------------------------------------------------

def test_plan_splits_group_out_and_keeps_dependencies():
    dag = dag_of("shop_mixed")
    new, clusters, stubs = K.plan(dag, {}, "dbt")
    assert stubs == {} and len(clusters) == 1
    c = clusters[0]
    assert sorted(c["keys"]) == [["customer_totals"], ["stg_orders"]] and c["sink_keys"] == [["customer_totals"]]
    assert c["roots"] == ["model.shop.stg_orders"] and c["ext_up_tasks"] == ["extract"]
    assert [t["task_id"] for t in new["tasks"]] == ["extract", "publish"]
    publish = next(t for t in new["tasks"] if t["task_id"] == "publish")
    assert publish["upstream"] == [] and publish["inlets"] == ["keyparts:customer_totals"]      # the dependency moved onto the dbt asset


def test_per_task_operators_become_steps_not_components():
    new, clusters, stubs = K.plan(dag_of("shop_operators"), {}, "dbt")
    assert clusters == [] and stubs == {}
    steps = {t["task_id"]: t for t in new["tasks"]}
    assert {t["type"] for t in steps.values()} == {"CosmosDbtStep"}
    assert steps["run_staging"]["cosmos_step"]["args"] == ["run", "--select", "tag:staging", "--vars", '{"min_amount": 1}', "--full-refresh"]
    assert steps["build_totals"]["cosmos_step"]["args"] == ["build", "--select", "customer_totals", "--exclude", "tag:slow"]
    assert steps["seed"]["cosmos_step"]["args"] == ["seed"] and steps["test_all"]["cosmos_step"]["args"] == ["test"]
    assert steps["run_staging"]["upstream"] == ["seed"]                    # the chain is kept


POD = ["image", "namespace", "node_selector", "resources", "secrets", "service_account_name"]


def container_ir(mode="kubernetes", only=None, override=None):
    """A synthetic edit of the real IR: every Cosmos task and group as if it ran in `mode`, with pod settings named (values are never in the IR)."""
    ir = ir_with()
    for d in ir["dags"]:
        if only and d["dag_id"] not in only:
            continue
        for g in d["cosmos_groups"]:
            g["execution"]["mode"] = mode
        for t in d["tasks"]:
            if t.get("cosmos"):
                t["cosmos"]["mode"] = mode.replace("_", "")
                t["cosmos"]["image"] = "registry.example/dbt:1"
                t["cosmos"]["container"] = {"settings": POD, "command_override": override}
    return ir


MODES = ["docker", "kubernetes", "aws_eks", "aws_ecs", "azure_container_instance", "gcp_cloud_run_job", "virtualenv", "watcher", "airflow_async"]


@pytest.mark.parametrize("mode", MODES)
def test_container_modes_convert_exactly_like_local_with_one_informational_note(mode):
    local = K.plan(dag_of("shop_mixed"), {}, "dbt")[1][0]
    d = dag_of("shop_mixed", container_ir(mode))
    new, clusters, stubs = K.plan(d, {}, "dbt")
    assert stubs == {} and len(clusters) == 1
    c = clusters[0]
    assert K.component_yaml(c, [["x"]]) == K.component_yaml(local, [["x"]])           # the same component, field for field
    assert c["cli_args"] == local["cli_args"] and c["keys"] == local["keys"] and c["caveats"] == local["caveats"]       # nothing degraded, nothing extra to review
    note = [n for n in c["notes"] if f"was {K.norm_mode(mode)} in Cosmos" in n]
    assert len(note) == 1 and K.norm_mode(mode) in note[0]
    if K.is_variant(K.norm_mode(mode)):
        assert "engine variant of the same dbt run" in note[0]
    else:
        assert "in Dagster dbt runs in the code location's environment, so pin dbt-core and the adapter" in note[0] and "image-only dependencies" in note[0]
        assert "image, namespace" in note[0] and "registry.example" not in note[0]            # pod settings dropped: their names listed, never a value


def test_per_task_operators_in_container_modes_are_steps_with_the_note():
    d = dag_of("shop_operators", container_ir("kubernetes"))
    new, clusters, stubs = K.plan(d, {}, "dbt")
    assert stubs == {} and clusters == []
    local = {t["task_id"]: t for t in K.plan(dag_of("shop_operators"), {}, "dbt")[0]["tasks"]}
    for t in new["tasks"]:
        assert t["type"] == "CosmosDbtStep" and t["cosmos_step"]["args"] == local[t["task_id"]]["cosmos_step"]["args"]
        assert "was kubernetes in Cosmos" in t["cosmos_step"]["mode_note"] and "image, namespace" in t["cosmos_step"]["mode_note"]
    assert local["seed"]["cosmos_step"]["mode_note"] is None


def test_a_container_that_runs_something_other_than_dbt_stays_a_loud_stub():
    d = dag_of("shop_dbt_dag", container_ir("docker", override=["/bin/bash", "-c", "make nightly"]))
    new, clusters, stubs = K.plan(d, {}, "dbt")
    assert clusters == [] and set(stubs) == {t["task_id"] for t in d["tasks"]}
    assert all("custom entrypoint or command that is not dbt" in r and "make nightly" in r for r in stubs.values())
    d2 = dag_of("shop_operators", container_ir("docker", override=["dbt"]))                    # an entrypoint that is dbt is fine
    assert K.plan(d2, {}, "dbt")[2] == {}
    d3 = dag_of("shop_operators", container_ir("kubernetes", override=["python", "run.py"]))
    assert len(K.plan(d3, {}, "dbt")[2]) == 4


def test_node_converters_and_other_operators_still_stub():
    d2 = dag_of("shop_dbt_dag", ir_with())
    d2["cosmos_groups"][0]["render"]["node_converters"] = True
    assert K.plan(d2, {}, "dbt")[2] and "node_converters" in next(iter(K.plan(d2, {}, "dbt")[2].values()))
    d3 = dag_of("shop_operators", ir_with())
    d3["tasks"][0]["cosmos"]["kind"] = "run_operation"
    assert "no conversion yet" in K.plan(d3, {}, "dbt")[2]["seed"]


# ---- the converter -----------------------------------------------------------------------------------------------------------------

def test_conversion_report_and_component_yaml(tmp_path):
    rep = convert(tmp_path, IR)
    dags = rep["dags"]
    assert {k: v["status"] for k, v in dags.items()} == {"shop_operators": "converted", "shop_dbt_dag": "converted", "shop_mixed": "converted"}
    mixed = dags["shop_mixed"]
    assert mixed["keys"] == [["shop_mixed", "extract"], ["shop_mixed", "publish"], ["customer_totals"], ["stg_orders"]]
    assert mixed["cosmos"][0]["keys"] == ["customer_totals", "stg_orders"] and mixed["cosmos"][0]["upstream_keys"] == ["shop_mixed/extract"]
    assert set(mixed["cosmos_tasks"]) == {"transform.customer_totals_run", "transform.stg_orders_run", "transform.dbt_shop_test"}
    text = (tmp_path / "pkg" / "defs" / "shop_mixed_dbt_transform" / "defs.yaml").read_text()
    assert "type: dagster_dbt.DbtProjectComponent" in text
    assert 'select: "tag:staging customer_totals"' in text and 'exclude: "stg_customers"' in text
    assert 'cli_args: ["build", {"--vars": {"min_amount": 5}}]' in text
    assert 'deps: ["raw_orders", "shop_mixed/extract"]' in text            # post_processing replaces deps, so the dbt parent is listed again
    assert 'project_dir: "../../dbt/dbt_shop"' in text
    # a second Cosmos group over the same project would collide on asset keys: it is keyed under by_<dag>/
    assert dags["shop_dbt_dag"]["cosmos"][0]["prefix"] == "by_shop_dbt_dag"
    assert 'key_prefix: "by_shop_dbt_dag"' in (tmp_path / "pkg" / "defs" / "shop_dbt_dag_dbt" / "defs.yaml").read_text()
    mixed_src = (tmp_path / "pkg" / "defs" / "shop_mixed.py").read_text()
    reasons = " ".join(c["reason"] for c in mixed["caveats"])
    assert "AFTER_ALL" in reasons                                          # the behaviour difference is stated
    assert "retries" not in " ".join(c["reason"] for c in dags["shop_dbt_dag"]["caveats"])        # retries are a behaviour change stated in a note, not a degraded unit
    dag_src = (tmp_path / "pkg" / "defs" / "shop_dbt_dag.py").read_text()
    assert "'dagster/max_retries': '2'" in dag_src and "dbt retry" in dag_src and "run_retries.enabled" in dag_src       # the DAG's retries became run retries on the job
    assert dags["shop_dbt_dag"]["cosmos"][0]["retries"] == 2
    assert "model-level lineage" in " ".join(c["reason"] for c in dags["shop_operators"]["caveats"])
    assert "dagster-dbt" in mixed["requirements"]
    assert (tmp_path / "pkg" / "converted_requirements.txt").read_text().split() == ["dagster-dbt"]
    proj = rep["dbt"]["projects"]["dbt_shop"]
    assert proj["used_unchanged"] is True and proj["converter_changes"] == ["asset keys are prefixed `by_shop_dbt_dag/` (the component's key_prefix; no model file changes)"]       # only what the converter changes
    assert "used unchanged" in mixed_src
    assert rep["dbt"]["projects"]["dbt_shop"]["environment"] == ["SHOP_DUCKDB_PATH"] and rep["dbt"]["projects"]["dbt_shop"]["expected_path"] == "pkg/dbt/dbt_shop"
    assert "dbt_shop" in (tmp_path / "pkg" / "defs" / "shop_operators.py").read_text()


@pytest.mark.parametrize("behavior,expect_cli,expect_exclude,degraded", [
    ("after_each", ["build"], "", False), ("build", ["build"], "", False),
    ("after_all", ["build"], "", True), ("none", ["build"], "resource_type:test", False)])
def test_test_behavior_mapping(behavior, expect_cli, expect_exclude, degraded):
    d = dag_of("shop_dbt_dag", ir_with())
    d["cosmos_groups"][0]["render"]["test_behavior"] = behavior
    c = K.plan(d, {}, "dbt")[1][0]
    assert c["cli_args"][0] == expect_cli[0] and expect_exclude in c["exclude"]
    assert any("AFTER_ALL" in x["reason"] for x in c["caveats"]) == degraded


def test_profile_mapping_becomes_env_var_profiles_and_never_copies_values():
    d = dag_of("shop_dbt_dag", ir_with())
    d["cosmos_groups"][0]["profile"] = {"profile_name": "wh", "target_name": "prod", "profiles_yml_filepath": None, "mapping": {
        "class": "SnowflakePasswordProfileMapping", "conn_id": "snowflake_default", "dbt_profile_type": "snowflake",
        "dbt_fields": ["account", "database", "password", "user", "warehouse"], "secret_fields": ["password"],
        "profile_args": {"schema": "analytics", "threads": 8, "private_key_passphrase": "<redacted>"}}}
    c = K.plan(d, {}, "dbt")[1][0]
    text = c["profile"]["generated"]["dbt_profiles/wh/profiles.yml"]
    assert "type: snowflake" in text and "\"{{ env_var('SNOWFLAKE_DEFAULT_PASSWORD') }}\"" in text and "schema: \"analytics\"" in text and "threads: 8" in text
    assert "private_key_passphrase: \"{{ env_var('SNOWFLAKE_DEFAULT_PRIVATE_KEY_PASSPHRASE') }}\"" in text
    assert "<redacted>" not in text
    assert "SNOWFLAKE_DEFAULT_ACCOUNT" in c["profile"]["env"] and "SNOWFLAKE_DEFAULT_PRIVATE_KEY_PASSPHRASE" in c["profile"]["env"]
    assert any("generated profiles.yml" in x["reason"] for x in c["caveats"])


def test_profiles_file_outside_the_project_is_not_copied():
    d = dag_of("shop_dbt_dag", ir_with())
    d["cosmos_groups"][0]["profile"]["profiles_yml_filepath"] = "/secrets/profiles.yml"
    c = K.plan(d, {}, "dbt")[1][0]
    assert c["profile"]["generated"] == {} and c["profile"]["profiles_dir"] == "dbt_profiles/shop"
    assert any("NOT copied" in x["reason"] and "env_var()" in x["reason"] for x in c["caveats"])


def test_container_mode_dag_converts_in_every_caveats_mode_with_a_note_and_no_review_flag(tmp_path):
    for flag in ([], ["--caveats", "off"]):
        rep = convert(tmp_path, container_ir("kubernetes", only={"shop_mixed"}) | {"dags": [d for d in container_ir("kubernetes", only={"shop_mixed"})["dags"] if d["dag_id"] == "shop_mixed"]}, *flag)["dags"]["shop_mixed"]
        assert rep["status"] == "converted" and rep["cosmos"] and not [c for c in rep["caveats"] if c["kind"] == "stub"]
        assert not any("kubernetes" in c["reason"] or "container" in c["reason"] for c in rep["caveats"])          # not a caveat: nothing to review
    src = (tmp_path / "pkg" / "defs" / "shop_mixed.py").read_text()
    assert "was kubernetes in Cosmos; in Dagster dbt runs in the code location's environment" in src and "image, namespace" in src and "registry.example" not in src
    assert 'select: "tag:staging customer_totals"' in (tmp_path / "pkg" / "defs" / "shop_mixed_dbt_transform" / "defs.yaml").read_text()


def test_apply_conversion_plans_cosmos_units(tmp_path):
    import apply_conversion
    import inventory
    rep = convert(tmp_path, IR)
    man = inventory.build_manifest(FIX / "dags", set())
    stats = apply_conversion.apply(man, IR, rep)                           # a Cosmos task has no key of its own: this used to raise KeyError
    assert stats["tasks_planned"] > 0
    by = {(u["dag_id"], u["task_id"]): u for u in man["units"] if u["kind"] == "task"}
    assert by[("shop_mixed", "publish")]["target"]["deps"] == {"shop_mixed/publish": ["customer_totals"]}      # the dependency on the group became one on its dbt asset
    assert by[("shop_operators", "run_staging")]["target"]["assets"] == ["shop_operators/run_staging"]


# ---- Dagster, dbt and DuckDB, for real ----------------------------------------------------------------------------------------------

def _project(tmp_path, monkeypatch, ir):
    dg = pytest.importorskip("dagster")
    pytest.importorskip("dagster_dbt")
    pytest.importorskip("duckdb")
    if not (Path(sys.executable).parent / "dbt").exists() and not shutil.which("dbt"):
        pytest.skip("dbt is not installed")
    rep = convert(tmp_path, ir)
    shutil.copytree(FIX / "dbt_shop", tmp_path / "pkg" / "dbt" / "dbt_shop", ignore=shutil.ignore_patterns("target", "logs", "*.duckdb"))
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SHOP_DUCKDB_PATH", str(tmp_path / "shop.duckdb"))
    monkeypatch.setenv("DAGSTER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DAGSTER_IS_DEV_CLI", "1")                              # the dbt component refreshes its manifest when it loads in dev
    (tmp_path / "home").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    from dagster.components import load_defs
    import importlib
    for m in [m for m in sys.modules if m == "pkg" or m.startswith("pkg.")]:
        del sys.modules[m]
    defs = load_defs(importlib.import_module("pkg.defs"), project_root=tmp_path)
    return defs, rep


@pytest.fixture
def project(tmp_path, monkeypatch):
    return _project(tmp_path, monkeypatch, IR)


def test_container_mode_dags_load_with_the_same_asset_graph_as_local(tmp_path, monkeypatch):
    defs, _ = _project(tmp_path, monkeypatch, container_ir("kubernetes"))            # no container runs: the DAG is only converted and loaded
    specs = {s.key.to_user_string(): sorted(d.asset_key.to_user_string() for d in s.deps) for s in defs.resolve_all_asset_specs()}
    assert specs["customer_totals"] == ["stg_customers", "stg_orders"] and specs["stg_orders"] == ["raw_orders", "shop_mixed/extract"] and specs["shop_mixed/publish"] == ["customer_totals"]
    assert len(list(defs.resolve_asset_graph().asset_check_keys)) == 6


def test_converted_cosmos_dags_load_with_model_level_lineage(project):
    defs, _ = project
    specs = {s.key.to_user_string(): s for s in defs.resolve_all_asset_specs()}
    deps = lambda k: sorted(d.asset_key.to_user_string() for d in specs[k].deps)       # noqa: E731
    assert deps("customer_totals") == ["stg_customers", "stg_orders"]                  # the model graph Cosmos had as tasks
    assert deps("stg_orders") == ["raw_orders", "shop_mixed/extract"]                  # the upstream Airflow task is kept next to the dbt parent
    assert deps("shop_mixed/publish") == ["customer_totals"]                           # and so is the task after the group
    assert deps("by_shop_dbt_dag/customer_totals") == ["by_shop_dbt_dag/stg_customers", "by_shop_dbt_dag/stg_orders"]
    assert deps("shop_operators/build_totals") == ["shop_operators/run_staging"]
    assert len(list(defs.resolve_asset_graph().asset_check_keys)) == 6                 # dbt tests are asset checks
    assert {j.name for j in defs.jobs} == {"shop_dbt_dag", "shop_mixed", "shop_operators"}
    assert {s.name for s in defs.schedules} == {"shop_dbt_dag_schedule", "shop_mixed_schedule"}


def test_converted_cosmos_dags_run_dbt_against_duckdb(project, tmp_path):
    import duckdb
    defs, _ = project
    first = defs.get_job_def("shop_operators").execute_in_process(raise_on_error=False)       # seed, run, build with --vars, test: Cosmos's per-task operators
    assert first.success, [str(e.event_specific_data.error)[:500] for e in first.get_step_failure_events()]
    con = duckdb.connect(str(tmp_path / "shop.duckdb"), read_only=True)
    assert con.execute("select customer_id, total, orders from customer_totals order by 1").fetchall() == [(10, 39.5, 2), (11, 99.99, 1), (12, 5.0, 1)]
    con.close()
    for name in ("shop_mixed", "shop_dbt_dag"):                                            # the groups: dbt build through the component
        r = defs.get_job_def(name).execute_in_process(raise_on_error=False)
        assert r.success, (name, [str(e.event_specific_data.error)[:500] for e in r.get_step_failure_events()])
    ran = {e.step_key for e in r.all_events if e.step_key and e.is_step_success}
    assert "shop_dbt_dag_dbt" in ran
    checks = [e for e in r.all_events if e.event_type_value == "ASSET_CHECK_EVALUATION"]
    assert len(checks) == 3 and all(e.event_specific_data.passed for e in checks)             # dbt tests ran as asset checks and passed
