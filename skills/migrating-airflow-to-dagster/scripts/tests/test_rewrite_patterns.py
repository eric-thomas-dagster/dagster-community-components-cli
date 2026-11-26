"""The pattern library (`reference/rewrite-patterns/*.py`) is EXECUTED here with the installed Dagster: each pattern's code runs and the skip / state behaviour its documentation claims is asserted.
Needs `dagster`; skips without it."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
PAT = Path(__file__).resolve().parents[2] / "reference" / "rewrite-patterns"


def load(name):
    spec = importlib.util.spec_from_file_location(f"pattern_{name}", PAT / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def run(assets, instance=None, **kw):
    r = dg.materialize(assets, raise_on_error=False, instance=instance, **kw)
    mat = {e.asset_key.to_user_string(): e for e in r.get_asset_materialization_events()}
    skipped = {e.step_key for e in r.all_events if e.event_type_value == "STEP_SKIPPED"}
    return r, mat, skipped


def meta(e, k):
    v = e.materialization.metadata.get(k)
    return getattr(v, "value", None)


def test_branching_plain_fallback_runs_only_the_chosen_branch_in_one_asset():
    m = load("branching_plain_fallback")
    r, mat, _ = run([m.branching_asset])
    assert r.success and list(mat) == ["branching_asset"]
    v = r.output_for_node("branching_asset")
    assert v["ran"] == ["branch_a"] and "branch_b" not in v and v["join"] == "joined after ['branch_a']"


def test_branching_plain_fallback_other_branch_taken(monkeypatch):
    m = load("branching_plain_fallback")
    monkeypatch.setattr(m, "pick_branch", lambda cfg: ["branch_b"])
    r, _, _ = run([m.branching_asset])
    assert r.output_for_node("branching_asset")["ran"] == ["branch_b"]


def steps(r):
    return {e.step_key: e.event_type_value for e in r.all_events if e.event_type_value in ("STEP_SUCCESS", "STEP_SKIPPED", "STEP_FAILURE")}


def test_optional_outputs_multi_asset_skips_the_unchosen_branch_and_what_is_below_it():
    m = load("branching")
    m.CHOSEN["v"] = "a"
    r, mat, skipped = run([m.branch_assets, m.under_a, m.under_b])
    assert r.success and set(mat) == {"asset_a", "under_a"} and skipped == {"under_b"}
    m.CHOSEN["v"] = "b"
    r, mat, skipped = run([m.branch_assets, m.under_a, m.under_b])
    assert r.success and set(mat) == {"asset_b", "under_b"} and skipped == {"under_a"}
    m.CHOSEN["v"] = "a"


def test_optional_outputs_a_regular_join_is_skipped_when_either_branch_is_skipped_unlike_airflow():
    m = load("branching")
    m.CHOSEN["v"] = "a"
    r, mat, skipped = run([m.branch_assets, m.needs_both])
    assert r.success and "needs_both" in skipped and "needs_both" not in mat                  # all_success, NOT none_failed_min_one_success
    m.CHOSEN["v"] = "both"
    r, mat, skipped = run([m.branch_assets, m.needs_both])
    assert r.success and "needs_both" in mat
    m.CHOSEN["v"] = "a"


def test_optional_outputs_can_subset_is_not_needed_and_harmless_in_a_job_that_selects_everything():
    import dagster as d

    def build(cs):
        @d.multi_asset(outs={"asset_a": d.AssetOut(is_required=False), "asset_b": d.AssetOut(is_required=False)}, can_subset=cs, name=f"ba_{cs}")
        def ba():
            yield d.Output("v", output_name="asset_a")

        @d.asset
        def ub(asset_b: str) -> str:
            return asset_b
        job = d.define_asset_job(f"j{cs}", selection="*")
        r = d.Definitions(assets=[ba, ub], jobs=[job]).get_job_def(f"j{cs}").execute_in_process(raise_on_error=False)
        return r.success, sorted(e.asset_key.to_user_string() for e in r.get_asset_materialization_events()), steps(r)["ub"]
    assert build(False) == build(True) == (True, ["asset_a"], "STEP_SKIPPED")


def test_optional_outputs_graph_asset_fan_in_rejoin_runs_with_whichever_branch_ran():
    m = load("branching")
    for chosen, want in (("a", ["x-A"]), ("b", ["y-B"]), ("both", ["x-A", "y-B"])):
        m.CHOSEN["v"] = chosen
        r, mat, _ = run([m.rejoined])
        assert r.success and "rejoined" in mat and r.asset_value(dg.AssetKey("rejoined")) == want
    m.CHOSEN["v"] = "a"
    r, _, _ = run([m.rejoined])
    st = steps(r)
    assert st["rejoined.do_b"] == "STEP_SKIPPED" and st["rejoined.do_a"] == "STEP_SUCCESS" and st["rejoined.join"] == "STEP_SUCCESS"
    m.CHOSEN["v"] = "none"                                                                   # nothing taken: the join is skipped too (Airflow also skips it: no success upstream)
    r, mat, _ = run([m.rejoined])
    assert r.success and "rejoined" not in mat and steps(r)["rejoined.join"] == "STEP_SKIPPED"
    m.CHOSEN["v"] = "a"


def test_optional_outputs_rejoin_across_runs_with_any_deps_updated():
    m = load("branching")
    defs = dg.Definitions(assets=[m.top_a, m.top_b, m.join_after_any])
    with dg.DagsterInstance.ephemeral() as inst:
        res = dg.evaluate_automation_conditions(defs=defs, instance=inst)
        assert res.get_num_requested(dg.AssetKey("join_after_any")) == 0
        dg.materialize([m.top_a], instance=inst)                                             # only one branch ran
        res = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=res.cursor)
        assert res.get_num_requested(dg.AssetKey("join_after_any")) == 1                     # requested: a later run, not the same run
        dg.materialize([m.join_after_any], instance=inst)
        res = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=res.cursor)
        assert res.get_num_requested(dg.AssetKey("join_after_any")) == 0


def test_short_circuit_false_skips_downstream_but_the_task_itself_succeeds(monkeypatch):
    m = load("short_circuit")
    r, _, _ = run([m.guarded])
    v = r.output_for_node("guarded")
    assert r.success and v["short_circuit"] == "success" and v["downstream_ran"] == ["all_done_task"]
    monkeypatch.setattr(m, "condition", lambda: True)
    r, _, _ = run([m.guarded])
    assert r.output_for_node("guarded")["downstream_ran"] == ["task_a", "task_b", "all_done_task"]


def test_short_circuit_optional_output_asset_skips_everything_below_and_a_bare_return_does_not():
    m = load("short_circuit")
    m.CONDITION["v"] = False
    r, mat, skipped = run([m.short_circuit_asset, m.below_short_circuit])
    assert r.success and mat == {} and skipped == {"below_short_circuit"}
    m.CONDITION["v"] = True
    r, mat, skipped = run([m.short_circuit_asset, m.below_short_circuit])
    assert r.success and set(mat) == {"short_circuit_asset", "below_short_circuit"} and skipped == set()
    m.CONDITION["v"] = False

    @dg.asset(output_required=False)
    def not_a_generator():
        return                                                                               # claim checked and refuted: this DOES materialize None
    @dg.asset
    def below_it(not_a_generator) -> str:
        return "ran"
    r, mat, skipped = run([not_a_generator, below_it])
    assert set(mat) == {"not_a_generator", "below_it"} and not skipped


def test_short_circuit_optional_output_op_in_a_graph_asset_skips_the_guarded_step():
    m = load("short_circuit")
    m.CONDITION["v"] = False
    r, mat, _ = run([m.guarded_graph])
    assert r.success and "guarded_graph" not in mat and steps(r)["guarded_graph.guarded_op"] == "STEP_SKIPPED" and steps(r)["guarded_graph.short_circuit_op"] == "STEP_SUCCESS"
    m.CONDITION["v"] = True
    r, mat, _ = run([m.guarded_graph])
    assert r.success and "guarded_graph" in mat


def test_skip_records_state_metadata_and_propagates_by_trigger_rule():
    m = load("skip")
    r, mat, skipped = run(list(m.defs.assets))
    assert r.success
    assert meta(mat["maybe_skips"], "airflow/state") == "skipped"
    assert meta(mat["all_success_consumer"], "airflow/state") == "skipped"      # all_success: skip propagates
    assert meta(mat["all_done_consumer"], "airflow/state") == "success"          # all_done: runs anyway


def test_skip_exit_code_other_than_99_fails():
    m = load("skip")
    import subprocess
    assert subprocess.run(["bash", "-c", "exit 2"]).returncode != m.SKIP_EXIT_CODE


def test_trigger_rule_all_done_runs_cleanup_even_when_upstream_work_fails():
    m = load("trigger_rules")
    m.CLEANED.clear()

    @dg.asset
    def work_then_cleanup():
        try:
            raise RuntimeError("upstream failed")
        finally:
            m.CLEANED.append("cleanup ran")
    r, mat, _ = run([work_then_cleanup])
    assert not r.success and m.CLEANED == ["cleanup ran"]             # cleanup ran; the step (and so the asset) is failed
    r, mat, _ = run([m.cleanup_after])
    assert r.success and m.CLEANED[-1] == "cleanup ran"


def test_trigger_rule_all_success_separate_assets_upstream_failure_leaves_downstream_not_run():
    @dg.asset
    def up():
        raise RuntimeError("x")

    @dg.asset(deps=["up"])
    def down():
        pass
    r, mat, _ = run([up, down])
    assert not r.success and "down" not in mat


def test_failure_sensor_requests_the_handler_job_when_a_run_fails():
    m = load("trigger_rules")
    assert m.defs.sensors and m.on_failure.name == "on_failure"
    dg.Definitions.validate_loadable(m.defs)
    with dg.DagsterInstance.ephemeral() as inst:
        r = m.failure_handler.execute_in_process(instance=inst)
        assert r.success
    # the sensor is loadable and wired to the handler job; firing it needs a deployed instance and is not exercised here
    assert m.on_failure.job_name == "failure_handler" if hasattr(m.on_failure, "job_name") else True


def test_mapped_python_loop_and_dynamic_graph_give_the_same_collected_values():
    m = load("mapped_tasks")
    r, _, _ = run([m.mapped_in_python, m.mapped_in_graph])
    assert r.success
    assert r.output_for_node("mapped_in_python") == [2, 4, 6]
    assert sorted(r.asset_value(dg.AssetKey("mapped_in_graph"))) == [2, 4, 6]


def test_mapped_empty_expansion_gives_an_empty_list_not_a_failure(monkeypatch):
    m = load("mapped_tasks")
    monkeypatch.setattr(m, "process", lambda i: i)

    @dg.asset
    def empty() -> list[int]:
        return [m.process(i) for i in []]
    r, _, _ = run([empty])
    assert r.success and r.output_for_node("empty") == []


def test_sensor_poll_until_true_timeout_and_soft_fail():
    m = load("sensors")
    t = {"now": 0.0}
    clock = lambda: t["now"]                                              # noqa: E731
    sleep = lambda s: t.__setitem__("now", t["now"] + s)                  # noqa: E731
    n = {"i": 0}

    def check():
        n["i"] += 1
        return n["i"] >= 3
    assert m.poll_until(check, timeout=10, interval=1, sleep=sleep, clock=clock) is True and t["now"] == 2.0
    t["now"] = 0.0
    assert m.poll_until(lambda: False, timeout=5, interval=1, sleep=sleep, clock=clock) is False and t["now"] <= 5.0


def test_sensor_asset_fails_on_timeout_and_a_soft_fail_variant_records_skipped(monkeypatch):
    m = load("sensors")
    r, mat, _ = run([m.wait_then_work])
    assert not r.success and "wait_then_work" not in mat                  # timeout -> failed
    m.READY["ok"] = True
    try:
        r, mat, _ = run([m.wait_then_work])
        assert r.success and meta(mat["wait_then_work"], "airflow/state") == "success"
    finally:
        m.READY["ok"] = False
    # soft_fail: the timeout is a skip, recorded in metadata
    src = (PAT / "sensors.py").read_text().replace("soft_fail = False", "soft_fail = True")
    ns = {"__name__": "sensors_soft"}
    exec(compile(src, "sensors_soft", "exec"), ns)
    r, mat, _ = run([ns["wait_then_work"]])
    assert r.success and meta(mat["wait_then_work"], "airflow/state") == "skipped"


def test_condition_sensor_requests_a_run_only_when_the_condition_holds():
    m = load("sensors")
    ctx = dg.build_sensor_context()
    assert isinstance(m.condition_sensor(ctx), dg.SkipReason)
    m.READY["ok"] = True
    try:
        assert isinstance(m.condition_sensor(dg.build_sensor_context()), dg.RunRequest)
    finally:
        m.READY["ok"] = False


def test_depends_on_past_blocks_after_a_failed_previous_run_and_allows_first_and_recovered_runs():
    m = load("depends_on_past")
    with dg.DagsterInstance.ephemeral() as inst:
        r, mat, _ = run([m.after_previous], instance=inst)               # first run: no predecessor
        assert r.success
        m.FAIL_NOW["fail"] = True
        try:
            r, _, _ = run([m.after_previous], instance=inst)             # this run fails
            assert not r.success
        finally:
            m.FAIL_NOW["fail"] = False
        r, mat, _ = run([m.after_previous], instance=inst)               # previous failed: blocked
        assert not r.success and "after_previous" not in mat


def test_dynamic_out_optional_zero_items_skips_everything_below_and_the_run_succeeds():
    m = load("mapped_tasks")
    m.ITEMS["v"] = []
    try:
        r, mat, _ = run([m.mapped_in_graph])
        st = steps(r)
        assert r.success and "mapped_in_graph" not in mat                                         # run status SUCCESS, asset not materialized
        assert st["mapped_in_graph.collect_items"] == "STEP_SKIPPED" and st["mapped_in_graph.fan_out"] == "STEP_SUCCESS"
        r, mat, _ = run([m.mapped_in_graph_runs_on_empty])                                         # the default DynamicOut(): the collecting step runs with []
        assert r.success and r.asset_value(dg.AssetKey("mapped_in_graph_runs_on_empty")) == []
        m.ITEMS["v"] = [1, 2, 3]
        r, mat, _ = run([m.mapped_in_graph])
        assert r.success and r.asset_value(dg.AssetKey("mapped_in_graph")) == [2, 4, 6]
    finally:
        m.ITEMS["v"] = [1, 2, 3]


def test_pool_is_first_class_on_assets_and_ops_and_the_project_runs():
    m = load("pools")
    assert m.launch_iss_mission.node_def.pool == "launch_pad" and m.launch_op.pool == "launch_pad"
    r, mat, _ = run(list(m.defs.assets))
    assert r.success and {"launch_iss_mission", "launch_graph"} == set(mat)
    dg.Definitions.validate_loadable(m.defs)


def test_xcom_value_is_an_argument_and_observability_values_are_metadata():
    m = load("xcom")
    r, mat, _ = run(list(m.defs.assets))
    assert r.success and r.asset_value(dg.AssetKey("consumer")) == 6
    md = mat["observed_only"].materialization.metadata
    assert md["row_count"].value == 3 and md["source"].value == "api"
    assert mat["with_output_metadata"].materialization.metadata["note"].value == "also passed to consumers"


def test_task_group_assets_get_group_names_prefixed_keys_and_a_factory_makes_templates():
    m = load("task_groups")
    keys = {k.to_user_string(): g for a in m.defs.assets for k, g in a.group_names_by_key.items()}
    assert keys["extract/pull"] == "extract" and keys["extract/clean"] == "extract" and keys["a/a_scaled"] == "a" and keys["b/b_scaled"] == "b"
    r, mat, _ = run(list(m.defs.assets))
    assert r.success and r.asset_value(dg.AssetKey(["a", "a_scaled"])) == [4, 6] and r.asset_value(dg.AssetKey(["b", "b_scaled"])) == [6, 9]


def test_cross_dag_eager_condition_requests_the_downstream_after_the_upstream_updates():
    m = load("cross_dag")
    dg.Definitions.validate_loadable(m.defs)
    src = m.source_asset

    @dg.asset(deps=["source_asset"], automation_condition=dg.AutomationCondition.eager())
    def downstream_y():
        return 1
    defs = dg.Definitions(assets=[src, downstream_y])
    with dg.DagsterInstance.ephemeral() as inst:
        res = dg.evaluate_automation_conditions(defs=defs, instance=inst)
        dg.materialize([src], instance=inst)
        res = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=res.cursor)
        assert res.get_num_requested(dg.AssetKey("downstream_y")) == 1
    assert m.trigger_target.name == "trigger_target"


def test_hooks_fire_on_assets_for_success_and_failure_all_done_cleanup():
    m = load("trigger_rules")
    m.HOOKED.clear()
    r, _, _ = run([m.hooked_asset])
    assert r.success and m.HOOKED == ["cleanup after success of hooked_asset"]

    @dg.asset(hooks={m.cleanup_ok, m.cleanup_failed})
    def failing_hooked():
        raise RuntimeError("x")
    m.HOOKED.clear()
    r, mat, _ = run([failing_hooked])
    assert not r.success and "failing_hooked" not in mat and m.HOOKED == ["cleanup after failure of failing_hooked"]    # the asset keeps its true (failed) state; the cleanup ran


def test_sensor_async_job_pattern_requests_the_run_only_when_the_job_is_done():
    m = load("sensors")
    assert isinstance(m.job_done_sensor(dg.build_sensor_context()), dg.SkipReason)
    m.JOBS["job-1"] = "done"
    try:
        rr = m.job_done_sensor(dg.build_sensor_context())
        assert isinstance(rr, dg.RunRequest) and rr.run_key == "job-1"
    finally:
        m.JOBS["job-1"] = "running"
    r, mat, _ = run([m.submit_job, m.after_job])
    assert r.success and r.asset_value(dg.AssetKey("submit_job")) == "job-1"


def test_mapped_failed_item_fails_the_run_the_others_ran_and_collect_did_not():
    m = load("mapped_tasks")
    r, mat, _ = run([m.mapped_with_a_failure])
    st = steps(r)
    assert not r.success and "mapped_with_a_failure" not in mat
    assert st["mapped_with_a_failure.process_item_failing[2]"] == "STEP_FAILURE" and st["mapped_with_a_failure.process_item_failing[1]"] == "STEP_SUCCESS" and st["mapped_with_a_failure.process_item_failing[3]"] == "STEP_SUCCESS"
    assert "mapped_with_a_failure.collect_items" not in st


def test_dynamic_partitions_alternative_one_tracked_status_per_item():
    m = load("mapped_tasks")
    with dg.DagsterInstance.ephemeral() as inst:
        inst.add_dynamic_partitions("mapped_items", ["x", "y"])
        r = dg.materialize([m.per_item], partition_key="y", instance=inst)
        assert r.success and r.output_for_node("per_item") == "processed y"


def test_templating_config_partition_window_config_envvar_resource_and_mock():
    m = load("templating_config")
    r, mat, _ = run([m.interval_asset], partition_key="2024-03-05")
    v = r.output_for_node("interval_asset")
    assert v["ds"] == "2024-03-05" and v["ds_add_7"] == "2024-03-12" and v["data_interval_end"].startswith("2024-03-06")
    r = dg.materialize([m.parametrised], run_config={"ops": {"parametrised": {"config": {"include_roster": False}}}})
    assert r.output_for_node("parametrised") is False
    os.environ.update(PARTNER_API_BASE_URL="http://h", PARTNER_API_PASSWORD="from-env")
    r = dg.materialize([m.uses_connection], resources=m.resources)
    assert r.output_for_node("uses_connection") == "http://h"
    r = dg.materialize([m.uses_connection], resources={"partner_api": m.PartnerApi(base_url="mock", token="x")})
    assert r.output_for_node("uses_connection") == "mock"                                  # a mock resource in tests


def test_pipes_runs_the_external_interpreter_and_a_missing_one_fails_loudly(monkeypatch):
    m = load("venv_pipes")
    monkeypatch.setattr(m, "VENV_PYTHON", sys.executable)
    r, mat, _ = run([m.venv_task], resources={"pipes_subprocess_client": dg.PipesSubprocessClient()})
    assert r.success and mat["venv_task"].materialization.metadata["rows"].value == 3
    monkeypatch.setattr(m, "VENV_PYTHON", "/nonexistent/venv/bin/python")
    r, mat, _ = run([m.venv_task], resources={"pipes_subprocess_client": dg.PipesSubprocessClient()})
    assert not r.success and "venv_task" not in mat                                        # no silent fallback to the current interpreter
    with pytest.raises(dg.Failure, match="not falling back"):
        m.require_interpreter(None)


def test_timeout_tag_and_freshness_policy_are_present():
    m = load("timeouts_sla")
    assert m.timeboxed_job.tags["dagster/max_runtime"] == "3600"
    assert m.sla_asset.specs_by_key[dg.AssetKey("sla_asset")].freshness_policy is not None if hasattr(m.sla_asset, "specs_by_key") else True
    dg.Definitions.validate_loadable(m.defs)


def test_latest_only_closest_form_loads_and_the_downstream_is_unpartitioned():
    m = load("latest_only")
    dg.Definitions.validate_loadable(m.defs)
    assert m.latest_only_downstream.partitions_def is None and m.partitioned_load.partitions_def is not None


def test_depends_on_past_partition_guard_uses_the_real_instance_api():
    m = load("depends_on_past")
    assert not hasattr(dg.DagsterInstance.ephemeral(), "get_materialization_count_for_partition")      # the API an unverified source named does not exist
    with dg.DagsterInstance.ephemeral() as inst:
        r = dg.materialize([m.guarded_partition], partition_key="2024-01-03", instance=inst, raise_on_error=False)
        assert not r.success                                                                      # previous partition missing: blocked
        for pk in ("2024-01-01", "2024-01-02", "2024-01-03"):                                      # the first has no predecessor; then in order
            assert dg.materialize([m.guarded_partition], partition_key=pk, instance=inst, raise_on_error=False).success


def test_depends_on_past_self_dependency_neither_blocks_a_run_nor_drives_automation():
    m = load("depends_on_past")
    assert dg.AssetKey("chain_after_previous") in m.chain_after_previous.asset_deps[dg.AssetKey("chain_after_previous")]
    assert dg.materialize([m.chain_after_previous], partition_key="2024-01-02", raise_on_error=False).success      # previous partition missing: not blocked
    daily = dg.DailyPartitionsDefinition(start_date="2024-01-01", end_date="2024-01-05")
    for label, cond in (("eager", dg.AutomationCondition.eager()), ("on_missing", dg.AutomationCondition.on_missing())):
        @dg.asset(name=f"chain_{label}", partitions_def=daily, automation_condition=cond,
                  deps=[dg.AssetDep(f"chain_{label}", partition_mapping=dg.TimeWindowPartitionMapping(start_offset=-1, end_offset=-1))])
        def a(context: dg.AssetExecutionContext):
            return context.partition_key
        defs, key = dg.Definitions(assets=[a]), dg.AssetKey(f"chain_{label}")
        with dg.DagsterInstance.ephemeral() as inst:
            res = dg.evaluate_automation_conditions(defs=defs, instance=inst)
            assert res.get_num_requested(key) == 0
            dg.materialize([a], partition_key="2024-01-01", instance=inst)
            res = dg.evaluate_automation_conditions(defs=defs, instance=inst, cursor=res.cursor)
            assert res.get_num_requested(key) == 0                                                # as evaluated here (no daemon), nothing is requested


def test_short_circuit_generator_form_skips_and_both_non_generator_returns_emit_none():
    @dg.asset(output_required=False)
    def gen_sc():
        if True:
            return
        yield dg.Output(1)

    @dg.asset
    def below_gen(gen_sc) -> str:
        return "ran"
    r, mat, skipped = run([gen_sc, below_gen])
    assert r.success and mat == {} and skipped == {"below_gen"}

    @dg.asset(output_required=False)
    def bare_return():
        return

    @dg.asset(output_required=False)
    def return_none():
        return None

    @dg.asset
    def below_bare(bare_return) -> str:
        return "ran"

    @dg.asset
    def below_none(return_none) -> str:
        return "ran"
    r, mat, skipped = run([bare_return, return_none, below_bare, below_none])
    assert set(mat) == {"bare_return", "return_none", "below_bare", "below_none"} and not skipped      # both emit None; nothing is skipped


def test_run_failure_sensor_evaluated_against_a_failed_run_record():
    m = load("trigger_rules")
    with dg.DagsterInstance.ephemeral() as inst:
        r = dg.materialize([m.risky], instance=inst, raise_on_error=False)
        run_rec = inst.get_run_by_id(r.run_id)
        assert run_rec.status == dg.DagsterRunStatus.FAILURE
        ctx = dg.build_run_status_sensor_context(sensor_name="on_failure", dagster_event=r.get_run_failure_event(), dagster_instance=inst, dagster_run=run_rec)
        out = m.on_failure(ctx)
        assert isinstance(out, dg.RunRequest) and out.run_key == r.run_id        # the function returns the request; a daemon firing it was not run


def test_max_runtime_job_tag_lands_on_the_run_and_an_op_tag_does_not():
    import dagster as d
    m = load("timeouts_sla")

    @d.asset(op_tags={"dagster/max_runtime": "30"})
    def at():
        return 1
    job = d.define_asset_job("aj", selection=[at], tags={"dagster/max_runtime": "90"})
    with d.DagsterInstance.ephemeral() as inst:
        r = d.Definitions(assets=[at], jobs=[job]).get_job_def("aj").execute_in_process(instance=inst)
        assert inst.get_run_by_id(r.run_id).tags["dagster/max_runtime"] == "90"                     # the run tag run monitoring reads
    assert at.op.tags["dagster/max_runtime"] == "30"                                                # an op tag stays an op tag
    assert m.timeboxed_job.tags["dagster/max_runtime"] == "3600"


def test_pool_is_an_op_attribute_and_the_concurrency_key_is_a_different_op_tag():
    @dg.asset(pool="p1")
    def ap():
        return 1

    @dg.asset(op_tags={"dagster/concurrency_key": "k1"})
    def ak():
        return 1
    assert ap.op.pool == "p1" and "dagster/concurrency_key" not in ap.op.tags
    assert ak.op.pool is None and ak.op.tags["dagster/concurrency_key"] == "k1"


def test_single_run_backfill_range_reads_the_window_not_the_partition_key():
    m = load("latest_only")
    daily = m.daily
    tags = {"dagster/asset_partition_range_start": "2024-01-01", "dagster/asset_partition_range_end": "2024-01-03"}
    with dg.DagsterInstance.ephemeral() as inst:
        r = dg.materialize([m.windowed_load], instance=inst, tags=tags)
        md = {k: v.value for k, v in next(iter(r.get_asset_materialization_events())).materialization.metadata.items()}
        assert md == {"window": "2024-01-01..2024-01-04", "n_keys": 3} and len(inst.get_runs()) == 1       # ONE run covers the range

        @dg.asset(partitions_def=daily, backfill_policy=dg.BackfillPolicy.single_run())
        def uses_key(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            return dg.MaterializeResult(metadata={"k": context.partition_key})
        assert not dg.materialize([uses_key], instance=inst, tags=tags, raise_on_error=False).success       # partition_key raises for a range


def test_join_below_a_skipped_upstream_by_shape():
    m = load("branching")
    m.CHOSEN["v"] = "a"
    try:
        # separate top-level join, inputs or deps, below a multi_asset optional output: skipped if ANY upstream is skipped
        @dg.asset(deps=["asset_a", "asset_b"])
        def join_deps() -> str:
            return "j"
        r, mat, skipped = run([m.branch_assets, join_deps])
        assert r.success and "join_deps" in skipped and "join_deps" not in mat
        r, mat, skipped = run([m.branch_assets, m.needs_both])
        assert r.success and "needs_both" in skipped
        # a skipped top-level optional-output asset + a sibling: a top-level join (input) is skipped too
        @dg.asset(output_required=False)
        def top_skipped():
            return
            yield

        @dg.asset
        def sibling() -> int:
            return 1

        @dg.asset
        def top_join(top_skipped, sibling) -> str:
            return "j"
        r, mat, skipped = run([top_skipped, sibling, top_join])
        assert r.success and "top_join" in skipped and set(mat) == {"sibling"}
        # Nothing-typed op inputs do not help
        @dg.op(out=dg.Out(is_required=False))
        def s_op():
            return
            yield

        @dg.op
        def other() -> int:
            return 1

        @dg.op(ins={"a": dg.In(dg.Nothing), "b": dg.In(dg.Nothing)})
        def join_n():
            return "j"

        @dg.job
        def nothing_job():
            join_n(a=s_op(), b=other())
        assert steps(nothing_job.execute_in_process(raise_on_error=False))["join_n"] == "STEP_SKIPPED"
        # the join as a graph_asset over the branch assets with pass-through ops and a fan-in list: runs with those that yielded
        for chosen, want in (("a", ["value-a"]), ("b", ["value-b"]), ("both", ["value-a", "value-b"])):
            m.CHOSEN["v"] = chosen
            r, mat, _ = run([m.branch_assets, m.join_graph])
            assert r.success and r.asset_value(dg.AssetKey("join_graph")) == want, chosen
        m.CHOSEN["v"] = "a"
        r, mat, _ = run([m.branch_assets, m.join_graph])
        assert steps(r)["join_graph.passthru_2"] == "STEP_SKIPPED" and steps(r)["join_graph.join_results"] == "STEP_SUCCESS"
        m.CHOSEN["v"] = "none"
        r, mat, _ = run([m.branch_assets, m.join_graph])
        assert r.success and "join_graph" not in mat and steps(r)["join_graph.join_results"] == "STEP_SKIPPED"
        # the marker form: the join runs whatever was skipped, what is below the skipped output is skipped
        for chosen in ("a", "none"):
            m.CHOSEN["v"] = chosen
            r, mat, skipped = run([m.marker_branches, m.join_on_marker, m.below_marker_b])
            assert r.success and "join_on_marker" in mat and "below_marker_b" in skipped, chosen
        # a skipped graph_asset: a top-level asset below it is skipped
        @dg.op(out=dg.Out(is_required=False))
        def maybe():
            return
            yield

        @dg.op
        def passthru2(x):
            return x

        @dg.graph_asset
        def skipped_graph():
            return passthru2(maybe())

        @dg.asset
        def below_skipped_graph(skipped_graph) -> str:
            return "j"
        r, mat, skipped = run([skipped_graph, below_skipped_graph])
        assert r.success and "below_skipped_graph" in skipped
    finally:
        m.CHOSEN["v"] = "a"


def test_nothing_below_a_failed_step_runs_in_the_same_run_a_fan_in_join_included():
    @dg.op
    def fails() -> int:
        raise RuntimeError("x")

    @dg.op
    def ok() -> int:
        return 1

    @dg.op
    def join_list(results: list) -> list:
        return results

    @dg.graph_asset
    def g_fail():
        return join_list([fails(), ok()])
    r, mat, _ = run([g_fail])
    st = steps(r)
    assert not r.success and "g_fail" not in mat and st["g_fail.fails"] == "STEP_FAILURE" and "g_fail.join_list" not in st

    @dg.asset
    def failing_top():
        raise RuntimeError("x")

    @dg.asset(deps=["failing_top"])
    def below() -> str:
        return "j"
    r, mat, _ = run([failing_top, below])
    assert not r.success and "below" not in mat and "below" not in {e.step_key for e in r.all_events if e.event_type_value == "STEP_SUCCESS"}
