# depends_on_past

Dagster has no per-task previous-run dependency. `depends_on_past.py` reads the asset's own history: the latest STEP_FAILURE newer than the latest materialization means the previous run did not
succeed, and the asset raises `dg.Failure` (Airflow leaves the task waiting, not failed). The first run has no predecessor and runs. Tested with a persistent instance: first run succeeds, a failed
run, then blocked, with the exact events of the installed Dagster (a plain asset failure emits no asset-level failed-to-materialize event there; the step event is used).

Header: `<task>: replaced by asset `<key>`` plus a line in the decision record (depends_on_past): `preserved as a check that fails when the previous run failed` or `not preserved: <why>`.
Multi-asset steps need the step name instead of the asset name (`context.op_def.name` is the step; pass the step key explicitly).

Partitioned variant: a self-dependency on the previous partition (`TimeWindowPartitionMapping(start_offset=-1, end_offset=-1)`) is accepted by Dagster, but a manually launched partition run is NOT blocked when the previous partition is missing (verified); it only declares the dependency. Treat as an approximation and record `not preserved` unless automation is the only way runs start.
