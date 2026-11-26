"""Pattern: execution_timeout, dagrun_timeout, sla / sla_miss_callback.
  dagrun_timeout / execution_timeout -> the tag `dagster/max_runtime` (seconds) on the job (the converter sets it that way). It is ENFORCED only when run monitoring is enabled in the deployment
                                        (`run_monitoring` in dagster.yaml): checked against the converter and its docs, NOT enforced in the tests here. A per-task timeout inside an asset: a plain deadline in the code.
  sla / sla_miss_callback            -> `freshness_policy=dg.FreshnessPolicy.time_window(fail_window=...)` on the asset (verified to exist on the installed Dagster: `FreshnessPolicy.time_window` and `.cron`;
                                        `build_last_update_freshness_checks` is the older check route). An Airflow SLA is measured from the task's scheduled START; freshness is measured from the data's last update: NOT the same, say so.
"""
import datetime as dt

import dagster as dg


@dg.asset(freshness_policy=dg.FreshnessPolicy.time_window(fail_window=dt.timedelta(hours=2)))
def sla_asset() -> int:
    return 1


@dg.asset
def long_task() -> int:
    return 1


timeboxed_job = dg.define_asset_job("timeboxed_job", selection=[long_task], tags={"dagster/max_runtime": "3600"})
defs = dg.Definitions(assets=[sla_asset, long_task], jobs=[timeboxed_job])
