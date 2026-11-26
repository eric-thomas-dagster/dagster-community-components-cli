# Timeouts and SLAs

`dagrun_timeout` / `execution_timeout`: the job tag `dagster/max_runtime` in seconds (`timeouts_sla.py`; what the converter does). Dagster terminates the run only when run monitoring is enabled in the deployment: say so in
the report. A timeout of one task inside a folded asset is a deadline in the code. `sla` / `sla_miss_callback`: `freshness_policy=dg.FreshnessPolicy.time_window(fail_window=...)` on the asset (exists on the installed
Dagster; verified to attach). An SLA measures time from the scheduled start, freshness measures the age of the data: not the same, and the callback itself (a notification) becomes an alert on the freshness state or a
run-failure sensor. Decision record (timeouts/sla): `preserved` or `not preserved: <why>`. Tested: the tag and the policy are present on the definitions; not tested: enforcement.
