# BigQuery Asset Checks — cost guardrail + freshness SLO

**Validated end-to-end against real APIs** (servicepulse-490502, gcp_observability_errors table). Two complementary checks on a BQ table:

```
warehouse_table  (existing BQ table, declare-only external asset)
       │
       ├── [check] query_cost_guard   ← bigquery_dry_run_check
       └── [check] freshness_slo      ← bigquery_table_freshness_check
```

## Components covered (2)

| Component | Mode | Cost |
|---|---|---|
| [`bigquery_dry_run_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/bigquery_dry_run_check) | Pre-flight cost guardrail. Three caps (pick any combination): `max_bytes`, `max_cost_usd`, `max_slot_ms`. Server-side dry-run, free, < 1s. | Free |
| [`bigquery_table_freshness_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/bigquery_table_freshness_check) | SLO check via table's `last_modified_time`. Optionally checks freshest partition for ingest-partitioned tables. | Free |

## Cost vs. bytes vs. slot-ms — pick one based on billing

| Billing model | Use | Why |
|---|---|---|
| On-demand | `max_bytes` (ground truth from BQ) plus optional `max_cost_usd` (convenience) | Bytes is exact; cost is locally computed at user-supplied rate |
| Flat-rate / capacity reservations | `max_slot_ms` | Bytes/cost are meaningless under reservations |
| Mixed / unsure | `max_bytes` only | Always safe; no pricing assumptions |

⚠️ `max_cost_usd` is computed as `bytes × on_demand_price_per_tb_usd / 1e12`. BigQuery's dry-run **does NOT return a cost** — Google can't (region/edition/contract vary). The rate is YOUR responsibility to keep current.

## Live run output

| Check | Verdict | Metadata |
|---|---|---|
| `query_cost_guard` | **PASSED** | bytes_scanned=0, max_bytes=10MB, max_cost_usd=$0.001 |
| `freshness_slo` | **PASSED** | age_minutes=~10, max=1440 |

## Common patterns

| Goal | Setup |
|---|---|
| Daily cost guardrail | `max_bytes` ~ 2× normal scan; flags anomalies |
| Hard cost ceiling | `max_cost_usd` at your budget per run; `blocking: true` |
| CI gate on new queries | Both checks in PR review; new queries that exceed budget fail review |
| Freshness alert (don't block) | `severity: WARN`, `blocking: false`; downstream alert pipeline reads Dagster events |
| Compliance freshness gate | `severity: ERROR`, `blocking: true` — stale data is worse than no data |

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
export BQ_TABLE=$GCP_PROJECT_ID.your_dataset.your_table   # must exist
```

## Required IAM

- `roles/bigquery.dataViewer` (table-level or dataset-level) — to read metadata
- `roles/bigquery.jobUser` (project-level) — dry-runs are jobs

## Cost

**Free.** Dry-runs return query plans without scanning data; `get_table` is metadata-only.

## Run it

```bash
./setup_bigquery_checks_demo.sh
cd bigquery-checks-demo
uv run dg launch --assets '*'
```

## Tweak to see failures

In `cost_guard/defs.yaml`, lower `max_bytes` to `100` → cost guard fails.
In `freshness_slo/defs.yaml`, lower `max_age_minutes` to `1` → freshness fails.
