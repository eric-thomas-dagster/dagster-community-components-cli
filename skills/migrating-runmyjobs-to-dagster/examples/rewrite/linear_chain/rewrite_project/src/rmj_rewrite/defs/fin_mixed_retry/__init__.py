"""RunMyJobs chain FIN_MIXED_RETRY, rewritten from its spec (rewrite route).

- FIN_MIXED_RETRY/pull: replaced by `mixed_pull` (ShellCommandAssetComponent, command verbatim, exit code 3 counts as success, timeout 600 s, 1 retry)
- FIN_MIXED_RETRY/alone: replaced by `mixed_alone` (SqlAssetComponent select 2, timeout 120 s, 2 retries, depends on `mixed_pull`)
- BATCH_QUEUE: replaced by pool `batch_queue` (shared with FIN_NIGHTLY; run `dagster instance concurrency set batch_queue 3`)
- FIN_FILE_NO_CHECKS: replaced by sensor `fin_file_no_checks` (FilesystemMonitorSensorComponent, poll 30 s, one run per poll, no size, age or stability check, as in the source; starts stopped) that runs job `fin_mixed_retry`
- FIN_MIXED_RETRY/alone#run_window: replaced by `run_window` "22:00-23:59 America/New_York mon,tue" on `mixed_alone` (outside it the run is skipped, not failed)

No schedule exists in the export for this chain. Registry components: shell_command_asset, SqlAssetComponent, filesystem_monitor sensor.
"""
