"""Pattern: depends_on_past=True. Dagster has no per-task previous-run dependency; the closest in-run check reads this asset's own last event and refuses to run after a failed one.

Airflow: the task instance waits (it is not failed) until the previous scheduled run's instance of the same task succeeded or was skipped; the first run has no predecessor and runs.
Here: the asset raises `dg.Failure` when its latest step event is a failure newer than its latest materialization ("blocked"): the closest honest state is failed, not waiting. Say so in the header.
"""
import dagster as dg


FAIL_NOW = {"fail": False}                                          # demo switch for the test only


def _latest_id(instance, event_type, **flt) -> int:
    recs = instance.get_event_records(dg.EventRecordsFilter(event_type=event_type, **flt), limit=1, ascending=False)
    return recs[0].storage_id if recs else 0


def previous_run_failed(context: dg.AssetExecutionContext) -> bool:
    """True when this step's latest STEP_FAILURE is newer than the asset's latest materialization. (A plain asset step failure emits no asset-level failure event in this Dagster, so the step event is read.)"""
    step = context.op_def.name                                           # a one-asset step is named after the asset
    failures = context.instance.get_event_records(dg.EventRecordsFilter(event_type=dg.DagsterEventType.STEP_FAILURE), limit=200, ascending=False)
    last_fail = next((r.storage_id for r in failures if r.event_log_entry.step_key == step), 0)
    return last_fail > _latest_id(context.instance, dg.DagsterEventType.ASSET_MATERIALIZATION, asset_key=context.asset_key)


@dg.asset
def after_previous(context: dg.AssetExecutionContext) -> str:
    if previous_run_failed(context):
        raise dg.Failure("depends_on_past: the previous run of this task did not succeed")
    if FAIL_NOW["fail"]:
        raise RuntimeError("this run fails")
    return "ran"


# Partitioned variant (a time-partitioned asset): declare the previous partition as a dependency. Verified: Dagster accepts a self-dependency with a TimeWindowPartitionMapping(-1, -1), but a MANUALLY launched
# partition run is NOT blocked when the previous partition is missing (the run succeeded); only the dependency is declared. Whether an automation condition respects it was not tested: treat it as an approximation.
daily = dg.DailyPartitionsDefinition(start_date="2024-01-01")


@dg.asset(partitions_def=daily, deps=[dg.AssetDep("chain_after_previous", partition_mapping=dg.TimeWindowPartitionMapping(start_offset=-1, end_offset=-1))])
def chain_after_previous(context: dg.AssetExecutionContext) -> str:
    return context.partition_key


defs = dg.Definitions(assets=[after_previous, chain_after_previous])
