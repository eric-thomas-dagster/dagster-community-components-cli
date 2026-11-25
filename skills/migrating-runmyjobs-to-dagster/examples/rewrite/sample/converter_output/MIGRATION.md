# Migration report

13 source objects: **8 converted**, **4 need review**, **1 unmapped** (62% converted without review).

Caveats: **2 stub(s)** (the asset exists in the right place and fails with `RMJ-CAVEAT: <reason>` until it is ported) and **3 converted with a known difference** (listed in the Notes column). Nothing is silently dropped.

Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified against RunMyJobs; run both in parallel before cutover.

| Kind | Source | Dagster artifact | Status | Notes |
|---|---|---|---|---|
| process_definition | LEGACY_SAP_EXTRACT | legacy_sap_extract | unmapped | SAP connector job (a RunMyJobs SAP built-in): it needs an SAP integration, in Dagster or through the RunMyJobs connector |
| chain | NIGHTLY_SALES_LOAD | job nightly_sales_load | needs review | kept as one asset per call: step 'extract' runs 2 calls in parallel, which one chain asset cannot express |
| process_definition | NIGHTLY_SALES_LOAD/margin (CALC_MARGIN) | nightly_sales_load__margin | needs review | step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced; RedwoodScript is not auto-translated; port the source (kept in the asset body) by hand or with the planned LLM pass |
| process_definition | NIGHTLY_SALES_LOAD/notify (NOTIFY_DONE) | nightly_sales_load__notify | needs review | step 'post' precondition {'time_window': 'BUSINESS_HOURS'} not enforced |
| schedule | PURGE_HOURLY | 1 schedule(s) | needs review | time window 08:00-18:00 mapped to hour range 8-17 (hour granularity); 'last day of month' has no cron equivalent; needs a custom schedule function; multiple submit-frame elements become one Dagster schedule each (union of cycles) |
| process_definition | NIGHTLY_SALES_LOAD/inventory (EXTRACT_INVENTORY) | nightly_sales_load__inventory | converted |  |
| process_definition | NIGHTLY_SALES_LOAD/inventory_fact (LOAD_INVENTORY_FACT) | nightly_sales_load__inventory_fact | converted | SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse') |
| process_definition | NIGHTLY_SALES_LOAD/sales (EXTRACT_SALES) | nightly_sales_load__sales | converted |  |
| process_definition | NIGHTLY_SALES_LOAD/sales_fact (LOAD_SALES_FACT) | nightly_sales_load__sales_fact | converted | SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse') |
| process_definition | PURGE_TEMP | purge_temp | converted |  |
| queue | BATCH_QUEUE | pool batch_queue | converted | configure Dagster pool 'batch_queue' with limit 4 |
| queue | DB_QUEUE | pool db_queue | converted | configure Dagster pool 'db_queue' with limit 2 |
| schedule | NIGHTLY_SALES_SCHEDULE | 1 schedule(s) | converted |  |

## Pools (RunMyJobs queues)

- `batch_queue`: limit 4
- `db_queue`: limit 2

A pool's limit caps the steps running at once across all runs (the default granularity is `op`). Pools have no limit until one is set. Apply the limits to the deployment:

```bash
dagster instance concurrency set batch_queue 4
dagster instance concurrency set db_queue 2
```

Or in `dagster.yaml` (open source) / deployment settings (Dagster+), and enable run monitoring so a cancelled or failed run releases its slots (without it a pool can deadlock):

```yaml
run_monitoring:
  enabled: true
  free_slots_after_run_end_seconds: 300
```

## Needs your input

The export cannot answer these; each answer changes what the converter generates. Work through them chain by chain.

### NIGHTLY_SALES_LOAD

- [ ] Confirm the schedule against what actually runs today (the Q1 run history is the check)
- [ ] Which database does each SQL connection point to (WAREHOUSE), and how are its credentials supplied?
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

