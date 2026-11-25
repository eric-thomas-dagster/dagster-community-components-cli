# Migration report

11 source objects: **9 converted**, **2 need review**, **0 unmapped** (82% converted without review).

Caveats: **0 stub(s)** (the asset exists in the right place and fails with `RMJ-CAVEAT: <reason>` until it is ported) and **2 converted with a known difference** (listed in the Notes column). Nothing is silently dropped.

Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified against RunMyJobs; run both in parallel before cutover.

| Kind | Source | Dagster artifact | Status | Notes |
|---|---|---|---|---|
| chain | FIN_NIGHTLY | asset fin_nightly (TaskChainAssetComponent) | needs review | per-step timeouts become one chain timeout of 900s (the sum of the steps that have one); 3 steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call |
| file_event | FIN_FILE_NO_CHECKS | sensor fin_file_no_checks -> job fin_mixed_retry_on_fin_file_no_checks | needs review | no minimum size, age or stability check: a file still being written can start the job (RunMyJobs files events usually have one; check the source); the sensor starts stopped; its cursor remembers files already seen, so turn it on once the parallel run starts |
| chain | FIN_MIXED_RETRY | job fin_mixed_retry | converted | kept as one asset per call: steps have different retry counts (the component retries the whole chain) |
| file_event | FIN_FILE_ARRIVED | sensor fin_file_arrived -> job fin_nightly_job_on_fin_file_arrived | converted | the sensor starts stopped; its cursor remembers files already seen, so turn it on once the parallel run starts |
| process_definition | FIN_MIXED_RETRY/alone (PUBLISH_ALONE) | fin_mixed_retry__alone | converted | SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse'); 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it |
| process_definition | FIN_MIXED_RETRY/pull (PULL) | fin_mixed_retry__pull | converted | 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it |
| process_definition | FIN_NIGHTLY/publish (PUBLISH) | step publish of asset fin_nightly | converted | SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database (components output: define a resource named 'warehouse'); 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it; SQL step: set env var RMJ_CONN_WAREHOUSE to a SQLAlchemy URL for the target database |
| process_definition | FIN_NIGHTLY/pull (PULL) | step pull of asset fin_nightly | converted | 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it |
| process_definition | FIN_NIGHTLY/transform (TRANSFORM) | step transform of asset fin_nightly | converted | 'Maximum Starts' interpreted as total attempts; verify against how the source team uses it |
| queue | BATCH_QUEUE | pool batch_queue | converted | configure Dagster pool 'batch_queue' with limit 3 |
| schedule | FIN_SCHED | 1 schedule(s) | converted |  |

## Pools (RunMyJobs queues)

- `batch_queue`: limit 3

A pool's limit caps the steps running at once across all runs (the default granularity is `op`). Pools have no limit until one is set. Apply the limits to the deployment:

```bash
dagster instance concurrency set batch_queue 3
```

Or in `dagster.yaml` (open source) / deployment settings (Dagster+), and enable run monitoring so a cancelled or failed run releases its slots (without it a pool can deadlock):

```yaml
run_monitoring:
  enabled: true
  free_slots_after_run_end_seconds: 300
```

## Needs your input

The export cannot answer these; each answer changes what the converter generates. Work through them chain by chain.

### FIN_MIXED_RETRY

- [ ] Confirm the schedule against what actually runs today (the Q1 run history is the check)
- [ ] Which database does each SQL connection point to (WAREHOUSE), and how are its credentials supplied?
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

### FIN_NIGHTLY

- [ ] Confirm the schedule against what actually runs today (the Q1 run history is the check)
- [ ] Which database does each SQL connection point to (WAREHOUSE), and how are its credentials supplied?
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

