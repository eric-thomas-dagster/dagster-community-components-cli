# Equivalence between the converter route and the rewrite route

Behavior, not code text. Each side is measured against the source facts in the spec; a dimension is *equivalent* when both sides match them (or differ identically). What replaced a precondition, status handler, event or notification is not machine-compared: the two statements are shown side by side. Also not machine-compared: timeouts, run windows, success exit codes and sensor settings (poll interval, minimum size and age): read them in the spec and in each side's YAML.

## `NIGHTLY_SALES_LOAD`

- assets: converter 6 (nightly_sales_load__inventory, nightly_sales_load__inventory_fact, nightly_sales_load__margin, nightly_sales_load__notify, nightly_sales_load__sales, nightly_sales_load__sales_fact); rewrite 5 (fact_inventory, fact_sales, inventory_raw, sales_margin, sales_raw)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **no**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 8 pair(s); 2 involve a call the rewrite replaced with a sensor (not compared) | all kept | all kept | equivalent |
| schedules (cron, time zone) | [('30 2 * * *', 'America/Chicago')] | [('30 2 * * *', 'America/Chicago')] | [('30 2 * * *', 'America/Chicago')] | equivalent |
| pools | ['batch_queue', 'db_queue'] | ['batch_queue', 'db_queue'] | ['batch_queue', 'db_queue'] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 6 command(s) | all present | ['NIGHTLY_SALES_LOAD/margin', 'NIGHTLY_SALES_LOAD/notify'] | converter matches the source |
| loud stubs left | none wanted | ['NIGHTLY_SALES_LOAD/margin (CALC_MARGIN)'] | none | rewrite matches the source |

Not machine-compared:

- converter: step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced
- rewrite, step post: time window BUSINESS_HOURS must be open: dropped: time window BUSINESS_HOURS is not defined in the export, so it cannot be enforced; needs the hours from the owner
- rewrite, step extract: on status Error: restart_step, max=2: replaced by retry_policy_max_retries 2 on `sales_raw` and `inventory_raw`

## `PURGE_TEMP`

- assets: converter 1 (purge_temp); rewrite 1 (purge_temp)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **no**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 0 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone); 1 element(s) have no cron form | [('*/15 8-17 * * 1,2,3,4,5', 'America/Chicago')] plus 1 not expressible in cron | [('*/15 8-17 * * 1,2,3,4,5', 'America/Chicago'), '1 element(s) dropped'] | [('*/15 8-17 * * 1,2,3,4,5', 'America/Chicago'), ('0 23 28-31 * *', 'America/Chicago')] | rewrite matches the source |
| pools | none | [] | [] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 1 command(s) | all present | all present | equivalent |
| loud stubs left | none wanted | none | none | equivalent |

## `LEGACY_SAP_EXTRACT`

- assets: converter 1 (legacy_sap_extract); rewrite 1 (legacy_sap_extract)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **yes**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 0 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | none | [] | [] | equivalent |
| pools | none | [] | [] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 0 command(s) | all present | all present | equivalent |
| loud stubs left | none wanted | ['LEGACY_SAP_EXTRACT'] | ['a loud stub remains'] | equivalent (both differ from the source the same way) |
