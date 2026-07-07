# Adaptive Backfill Detective — the agent decides *how* to fill each gap

**Components:** `synthetic_data_generator` (sparse_sensors), `formula`, `summarize`, `langchain_chain_asset`, `router`, `dataframe_to_csv` — **100% composition of existing components.**

**Script:** [`setup_adaptive_backfill_demo.sh`](./setup_adaptive_backfill_demo.sh)
**Cost:** ~$0.005–$0.01 per run (one LLM call per (sensor, day), ~42 rows × gpt-4o-mini)
**Validated:** 2026-07-07 — 3 sensors × 14 days at 25% dropout produced 42 (sensor, day) rows; LLM correctly separated `ok` days from `interpolate` days using the reading-count thresholds; `re_ingest` / `escalate` queues remained empty as expected for that dropout rate.

## Why this exists

Every IoT / metrics / streaming pipeline has gaps: flaky sensors, network hiccups, upstream stalls. The naive fix is one-size-fits-all — *"interpolate everything"* or *"re-ingest everything nightly."* That wastes cycles on healthy days and hides real outages under a blanket of interpolation.

The agentic shape: an LLM looks at what's actually missing **per partition** (per `(sensor, day)`, per `(region, hour)`, per `(source, date)` — whatever your grain is) and picks a strategy from a bounded, safe set. Same declarative pipeline, adaptive per-run response.

```
sparse_sensors_raw       (synthetic — 3 sensors × 14 days × 24h, ~25% dropout)
       ↓
sensors_with_date        (formula — derive a `date` column from reading_ts)
       ↓
daily_readings_by_sensor (summarize — group by (sensor_id, date), count readings)
       ↓
backfill_plan            (langchain_chain_asset — LLM picks per (sensor, day):
                          ok / interpolate / re_ingest / escalate)
       ↓
┌── ok_days             ┐   (router splits by action)
│── interpolate_queue    │
│── re_ingest_queue      │
└── escalate_queue       ┘
       ↓ (each)
<action>_export.csv      (simulated per-action sinks — swap for real
                          responses in prod: gap-fill job, re-ingest
                          trigger, PagerDuty alert)
```

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_adaptive_backfill_demo.sh -o setup_adaptive_backfill_demo.sh
chmod +x setup_adaptive_backfill_demo.sh
./setup_adaptive_backfill_demo.sh
```

## The agent's rules (from the prompt)

Bounded action set — the LLM picks by name, cannot invent operations:

| Action | Trigger | Downstream (in prod) |
|---|---|---|
| `ok` | 22+ readings/day (normal jitter) | Drop — no work needed |
| `interpolate` | 12–21 readings/day (partial gaps) | Gap-fill job with per-row date param |
| `re_ingest` | 1–11 readings/day (significant loss) | Re-pull that `(sensor, day)` from source |
| `escalate` | 0 readings OR temperature wildly off | `slack_notification` / `pagerduty_alert` |

The LLM also has an escape hatch for `avg_temp` outside 15–30°C — sensor probably faulty even if reading_count is high. So the choice adapts to *why* the day looks weird, not just how many readings are present.

## Validated run output (2026-07-07)

```
Per-action (sensor, day) counts:
  ok_days:           2 rows  (reading_count 22+)
  interpolate_queue: 40 rows (reading_count 12–21)
  re_ingest_queue:   0 rows  (no severe gaps at 25% dropout)
  escalate_queue:    0 rows  (no zero-reading days)
```

Sample rows from the agent's plan (`backfill_plan` asset):

```
sensor_id  date         reading_count  avg_temp    action        reason
sensor_a   2026-04-01           18       18.81  interpolate    18 within 12-21 range; avg_temp normal.
sensor_a   2026-04-10           22       19.18  ok             22 within normal daily jitter range.
sensor_a   2026-04-13           16       19.02  interpolate    16 within 12-21; avg_temp normal.
sensor_b   2026-04-01           19       21.16  interpolate    19 within range for interpolation.
```

To force `re_ingest` / `escalate` queues to populate, bump `dropout_rate` in the raw asset's `schema_options` to `0.75` or higher — more days will drop into the severe-loss bands.

## Extension patterns

The demo uses CSV sinks for reproducibility. Real deployments swap each per-action CSV for the actual response the ops team wants:

| Route | Real destination |
|---|---|
| `ok_days` | Drop / observation asset for tracking healthy runs |
| `interpolate_queue` | Trigger a `dagster asset materialize` on a `time_series_interpolator` asset with per-row `(sensor, date)` config |
| `re_ingest_queue` | Trigger a `python_callable_job` that re-pulls historical data from the source system |
| `escalate_queue` | `slack_notification` component → `#data-oncall`, or a PagerDuty webhook |

Other extensions:

- **Different partition grain.** Change `group_by` to `[region, hour]` or `[table_name, date]` — the pattern works for any partition shape.
- **Add cost sensitivity.** Give the LLM a `re_ingest_cost_estimate` column so it can factor $$$ into the choice (e.g., "escalate low-value re-ingests, interpolate high-cost ones").
- **Human-in-the-loop.** Add an `asset_check` on `backfill_plan` that fails if `escalate` count exceeds a threshold — force manual review before pipeline continues.
- **Real data.** Replace `synthetic_data_generator` with a Timescale / InfluxDB / Snowflake / BigQuery query that returns rows-per-partition counts against your actual data lake.

## The family of agentic-pipeline demos

Adaptive Backfill Detective closes the three-demo arc:

1. [**Data Doctor**](./data_doctor.md) — agent picks DQ **remediations** per column, executed by `data_remediation_asset`.
2. [**Adaptive Triage Router**](./adaptive_triage.md) — agent picks the **downstream route** per row, executed by `router`.
3. **Adaptive Backfill Detective** *(this demo)* — agent picks the **fill/response strategy** per partition, executed by `router` fan-out.

The common pattern: **agent picks by name from a bounded, safe set**. Dagster's declarative machinery executes. AI decides *what*; Dagster runs *how*.

## Why this pattern beats "LLM writes code"

- **Bounded blast radius.** The agent has 4 actions. It cannot invent operations.
- **Auditable.** Every pick has a `reason` stored on the row. Reproducible modulo LLM temperature.
- **Composable.** The plan is a DataFrame. Gate it with an `asset_check`, route it through review, publish to Slack for approval — all before anything downstream runs.
- **Transparent lineage.** Every step is a Dagster asset. No hidden runtime decisions.

Compare to letting an LLM emit SQL / Python that runs directly against your systems: unbounded blast, no schema, hard to reproduce, invisible in lineage. This shape gets you the same adaptive intelligence with none of that risk.

## Related

- [Data Quality agent — anomaly + LLM explanations](./data_quality_agent.md) — different agentic shape: LLM **narrates** anomalies for on-call rather than picking remediations.
- [PII detection + LLM redaction](./pii_redaction.md) — LLM as **fresh eyes on statistical output**, another agentic use case.
- [Detect Changes](./detect_changes.md) — deterministic CDC (insert/update/delete/unchanged); replace its downstream with an LLM-plan step to get "agentic change response."
