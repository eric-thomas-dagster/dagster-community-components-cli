"""RunMyJobs chain NIGHTLY (from the data-model fixture), rewritten from its spec (rewrite route).

- NIGHTLY/extract_1: replaced by `extract` (ShellCommandAssetComponent `echo hi`, 2 retries from the step's restart handler)
- NIGHTLY/load_1: replaced by `load` (ShellCommandAssetComponent `echo load`, depends on `extract`)
- JG: replaced by schedule `nightly` (CronScheduleComponent `30 2 * * *`, execution_timezone UTC, stopped by default)
- NIGHTLY#precondition1: dropped: time window BIZ is not defined in the export, so it cannot be enforced; needs its hours from the owner
- NIGHTLY#handler1: replaced by retry_policy_max_retries 2 on `extract`
- NIGHTLY#note1: dropped: the chain-level restart count has no confirmed meaning (the converter drops it too); needs the value and what it restarts

Registry components: shell_command_asset, cron_schedule.
"""
