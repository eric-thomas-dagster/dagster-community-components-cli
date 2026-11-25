"""RunMyJobs process definition PURGE_TEMP, rewritten from its spec (rewrite route).

- PURGE_TEMP: replaced by `purge_temp` (ShellCommandAssetComponent, command kept verbatim); run_as svc_batch is not enforceable in Dagster and is dropped: the code location's user runs it
- PURGE_HOURLY: replaced by schedule `purge_hourly` (CronScheduleComponent, `*/15 8-17 * * 1,2,3,4,5` America/Chicago; the 08:00-18:00 window is kept at hour granularity, 17:45 is the last tick)
  and by schedule function `purge_month_end` (cron `0 23 28-31 * *`, a custom schedule function that only requests a run when tomorrow is the 1st, the "last day of month" element the converter cannot express)

Registry component used: shell_command_asset, cron_schedule. Both schedules are stopped by default.
"""
