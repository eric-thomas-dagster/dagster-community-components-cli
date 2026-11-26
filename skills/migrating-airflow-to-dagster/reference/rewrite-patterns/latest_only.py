"""Pattern: LatestOnlyOperator (skip downstream unless this is the latest interval).
Dagster has no equivalent skip. The closest: keep the downstream asset UNPARTITIONED with `AutomationCondition.eager()` so a backfill of old partitions does not re-run it each time, and run backfills with
`BackfillPolicy.single_run()` so one run covers the range. NOT equivalent: Airflow runs the task graph per interval and skips downstream for old intervals; here old partitions still materialize their partitioned
upstream, and the unpartitioned downstream runs when upstream updates, including after a backfill. Notifications that must only fire for the latest interval move to a sensor.
"""
import dagster as dg

daily = dg.DailyPartitionsDefinition(start_date="2024-01-01")


@dg.asset(partitions_def=daily, backfill_policy=dg.BackfillPolicy.single_run())
def partitioned_load(context: dg.AssetExecutionContext) -> str:
    return context.partition_key


@dg.asset(deps=[partitioned_load], automation_condition=dg.AutomationCondition.eager())
def latest_only_downstream() -> str:
    return "unpartitioned downstream"


defs = dg.Definitions(assets=[partitioned_load, latest_only_downstream])
