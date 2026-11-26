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


# Partitioned variants.
# (1) NATIVE guard, in the asset (verified): the previous partition must be among `instance.get_materialized_partitions(asset_key)`. The real instance API is `get_materialized_partitions`;
#     `get_materialization_count_for_partition` (named by an unverified source) does NOT exist on this version. A failed guard raises `dg.Failure` (Airflow waits instead of failing).
daily = dg.DailyPartitionsDefinition(start_date="2024-01-01")


@dg.asset(partitions_def=daily)
def guarded_partition(context: dg.AssetExecutionContext) -> str:
    keys = context.assets_def.partitions_def.get_partition_keys()
    i = keys.index(context.partition_key)
    if i > 0 and keys[i - 1] not in context.instance.get_materialized_partitions(context.asset_key):
        raise dg.Failure(f"depends_on_past: partition {keys[i - 1]} has not been materialized")
    return context.partition_key


# (2) a self-dependency on the previous partition. Verified: accepted, but it does NOT block a manual or scheduled run (a missing previous partition did not stop the run), and
#     `AutomationCondition.eager()` / `on_missing()` on it requested NO partition in the evaluation (0 requested at the first tick and after 2024-01-01 was materialized): it does not drive the chain
#     forward as tested. It shapes lineage only here; do not rely on it.
@dg.asset(partitions_def=daily, deps=[dg.AssetDep("chain_after_previous", partition_mapping=dg.TimeWindowPartitionMapping(start_offset=-1, end_offset=-1))])
def chain_after_previous(context: dg.AssetExecutionContext) -> str:
    return context.partition_key


defs = dg.Definitions(assets=[after_previous, guarded_partition, chain_after_previous])
