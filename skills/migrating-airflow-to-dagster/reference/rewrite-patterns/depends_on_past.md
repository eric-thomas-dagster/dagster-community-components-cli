# depends_on_past

Dagster has no per-task previous-run dependency. `depends_on_past.py` reads the asset's own history: the latest STEP_FAILURE newer than the latest materialization means the previous run did not
succeed, and the asset raises `dg.Failure` (Airflow leaves the task waiting, not failed). The first run has no predecessor and runs. Tested with a persistent instance: first run succeeds, a failed
run, then blocked, with the exact events of the installed Dagster (a plain asset failure emits no asset-level failed-to-materialize event there; the step event is used).

Header: `<task>: replaced by asset `<key>`` plus a line in the decision record (depends_on_past): `preserved as a check that fails when the previous run failed` or `not preserved: <why>`.
Multi-asset steps need the step name instead of the asset name (`context.op_def.name` is the step; pass the step key explicitly).

Partitioned assets (`depends_on_past.py`):
- **Guard in the asset (verified for real):** the previous partition must be in `context.instance.get_materialized_partitions(context.asset_key)`, else `dg.Failure`. A first partition has no predecessor and runs; running 2024-01-03 first fails, then 01-01, 01-02, 01-03 in order succeed. The real API is `get_materialized_partitions`; `get_materialization_count_for_partition` does not exist on the installed version.
- **Self-dependency `TimeWindowPartitionMapping(start_offset=-1, end_offset=-1)`:** accepted by Dagster, but it does not block a manual or scheduled run (verified: a run with the previous partition missing succeeded). With `AutomationCondition.eager()` or `on_missing()` the evaluation (against an ephemeral instance, no daemon) requested no partition at all, before or after the first partition was materialized, so it does not drive the chain as tested. What a running daemon would do was not run. Treat it as lineage only; use the guard.
Record: `preserved` as a guard that fails when the previous partition is missing (Airflow waits, not fails), or `not preserved: <why>`.
