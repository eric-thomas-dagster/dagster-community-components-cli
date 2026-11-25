"""RunMyJobs chain FIN_NIGHTLY (nightly finance publish), rewritten from its spec (rewrite route).

Three steps become three assets in order, so each keeps its own retry, timeout and lineage (the converter makes ONE task-chain asset with a summed 900 s timeout).

- FIN_NIGHTLY/pull: replaced by `fin_pull` (ShellCommandAssetComponent, command verbatim, run_date passed as an environment variable, exit code 3 counts as success, timeout 600 s)
- FIN_NIGHTLY/transform: replaced by `fin_transform` (reimplemented as a Python asset: the script print('transform') is a function body; no timeout in the source)
- FIN_NIGHTLY/publish: replaced by `fin_publish` (SqlAssetComponent, connection from env RMJ_CONN_WAREHOUSE, timeout 300 s)
- FIN_SCHED: replaced by schedule `fin_sched` (CronScheduleComponent `30 22 * * *` America/New_York, stopped by default) on job `fin_nightly`
- BATCH_QUEUE: replaced by pool `batch_queue` on all three assets; run `dagster instance concurrency set batch_queue 3` (the queue is shared with FIN_MIXED_RETRY: one pool, one limit)
- FIN_FILE_ARRIVED: replaced by sensor `fin_file_arrived` (FilesystemMonitorSensorComponent: min age 120 s, min size 1, stable-size check, poll 60 s, starts stopped) that runs job `fin_nightly`
- FIN_NIGHTLY#run_window: replaced by `run_window` "22:00-23:59 America/New_York mon,tue" on each of the three assets (a run outside it is skipped, not failed)
- Retries: Maximum Starts 2 gives retry_policy_max_retries 1 on each asset (the meaning of Maximum Starts as total attempts is unconfirmed).

Registry components: shell_command_asset, sql_asset-style SqlAssetComponent, cron_schedule, filesystem_monitor sensor. Not preserved: nothing known; the run window is applied per asset, so a chain that starts at 23:58 may skip its last step.
"""
