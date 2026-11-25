"""RunMyJobs chain NIGHTLY_SALES_LOAD, rewritten from its spec (rewrite route).

Accounting, one line per RunMyJobs object:

- NIGHTLY_SALES_LOAD/sales: replaced by `sales_raw` (ShellCommandAssetComponent, command kept verbatim, run_date passed as an environment variable)
- NIGHTLY_SALES_LOAD/inventory: replaced by `inventory_raw` (ShellCommandAssetComponent)
- NIGHTLY_SALES_LOAD/sales_fact: replaced by `fact_sales` (SqlAssetComponent, connection from env RMJ_CONN_WAREHOUSE)
- NIGHTLY_SALES_LOAD/inventory_fact: replaced by `fact_inventory` (SqlAssetComponent)
- NIGHTLY_SALES_LOAD/margin: replaced by `sales_margin` (reimplemented in Python; the RedwoodScript lookup getJobByName("x") was dropped, its result was unused)
- NIGHTLY_SALES_LOAD/notify: replaced by `notify_done` (reimplemented as a run-status sensor that logs "notifying" after a successful run)
- NIGHTLY_SALES_SCHEDULE: replaced by `nightly_sales_load` schedule via CronScheduleComponent, 30 2 * * * America/Chicago, stopped by default
- BATCH_QUEUE: replaced by pool `batch_queue`; run `dagster instance concurrency set batch_queue 4`
- DB_QUEUE: replaced by pool `db_queue`; run `dagster instance concurrency set db_queue 2`
- NIGHTLY_SALES_LOAD#precondition1: dropped: time window BUSINESS_HOURS is not defined in the export, so it cannot be enforced; needs the hours from the owner
- NIGHTLY_SALES_LOAD#handler1: replaced by retry_policy_max_retries 2 on `sales_raw` and `inventory_raw`

Registry components used: shell_command_asset, sql_asset-style SqlAssetComponent, cron_schedule.
Not preserved: the BUSINESS_HOURS precondition; notify no longer runs in parallel with margin and
only fires when the whole run succeeds.
"""
