# LatestOnlyOperator

No equivalent skip exists (`latest_only.py`: unpartitioned downstream + `AutomationCondition.eager()` + `BackfillPolicy.single_run()` loads and is the closest). It is NOT equivalent: old partitions still
materialize upstream, and the unpartitioned downstream runs after an upstream update including a backfill. What a multi-partition backfill does end to end was NOT run here (it needs a deployed instance and daemon).
Decision record (latest only): `not preserved: <why>` unless the DAG is not backfilled. Notifications guarded by it move to a sensor.

Backfill policy (verified in-process, not as a daemon-launched backfill): one run per partition unless `BackfillPolicy.single_run()`; with `single_run` one run covers the range and the asset must read `context.partition_time_window` / `context.partition_keys`, not `context.partition_key` (which raises for a range), and must not return a value through the default IO manager (return `MaterializeResult`). See `windowed_load`.
