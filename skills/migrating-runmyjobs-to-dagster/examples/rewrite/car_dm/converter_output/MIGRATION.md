# Migration report

5 source objects: **3 converted**, **2 need review**, **0 unmapped** (60% converted without review).

Caveats: **0 stub(s)** (the asset exists in the right place and fails with `RMJ-CAVEAT: <reason>` until it is ported) and **2 converted with a known difference** (listed in the Notes column). Nothing is silently dropped.

Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified against RunMyJobs; run both in parallel before cutover.

| Kind | Source | Dagster artifact | Status | Notes |
|---|---|---|---|---|
| chain | NIGHTLY | job nightly | needs review | chain-level restart count is not converted; kept as one asset per call: steps have different retry counts (the component retries the whole chain) |
| process_definition | NIGHTLY/load_1 (LOAD) | nightly__load_1 | needs review | step 'load' precondition {'time_window': 'BIZ'} not enforced |
| process_definition | NIGHTLY/extract_1 (EXTRACT) | nightly__extract_1 | converted |  |
| queue | Q1 | pool q1 | converted | configure Dagster pool 'q1' with limit 3 |
| schedule | JG | 1 schedule(s) | converted |  |

## Pools (RunMyJobs queues)

- `q1`: limit 3

A pool's limit caps the steps running at once across all runs (the default granularity is `op`). Pools have no limit until one is set. Apply the limits to the deployment:

```bash
dagster instance concurrency set q1 3
```

Or in `dagster.yaml` (open source) / deployment settings (Dagster+), and enable run monitoring so a cancelled or failed run releases its slots (without it a pool can deadlock):

```yaml
run_monitoring:
  enabled: true
  free_slots_after_run_end_seconds: 300
```

## Needs your input

The export cannot answer these; each answer changes what the converter generates. Work through them chain by chain.

### NIGHTLY

- [ ] Confirm the schedule against what actually runs today (the Q1 run history is the check)
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

