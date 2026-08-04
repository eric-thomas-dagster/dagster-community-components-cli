# Data Platform Showcase — Multi-Vendor Ingest → dbt → Dynamic Fan-Out

> ⚠️ **Dagster+ Serverless:** deploys, but the partitioned dbt path needs persistent storage (Serverless containers are ephemeral per-run — `/tmp/warehouse.duckdb` doesn't survive across runs). Workaround: materialize `+customer_360/fct_customer_daily` in one run (upstream `+` forces single-run execution). Full fix: swap DuckDB for MotherDuck or a real cloud warehouse.

The "typical Dagster" data platform demo — the shape a data-engineering SE audience will recognize immediately. Multiple upstream sources, dbt in the middle, downstream fan-out over runtime-decided cohorts. **One code location, local DuckDB, no Docker, 100% components + YAML.**

## Architecture

```
  ┌── vendor_sources ─────┐    ┌── raw_warehouse ──────┐    ┌── dbt ────────────────────┐    ┌── cohort_extracts ─────┐
  │ src_customers         │──► │ raw_customers         │──► │ stg_customers             │──► │ cohort_extracts        │
  │ src_orders            │──► │ raw_orders            │──► │ stg_orders     ─┐         │    │ (DynamicOut fan-out    │
  │ src_stripe_charges    │──► │ raw_stripe_charges    │──► │ stg_charges    ─┤         │    │  over 4 cohorts →      │
  │ (SyntheticDataGen)    │    │ (DuckDBTableWriter)   │    │ fct_customer_daily ◄──┘   │    │  4 per-cohort CSVs)    │
  └───────────────────────┘    └───────────────────────┘    │ (dagster_dbt.DbtProject)  │    └────────────────────────┘
                                                            └───────────────────────────┘
```

Every layer is a real component. Every asset in the graph is materializable, has a materialization history, and links to its lineage.

## What's in it

**Ingest layer** — three "vendors," each generated synthetically for the demo:
- `src_customers` — dim table (500 rows)
- `src_orders` — Shopify-style orders (3000 rows)
- `src_stripe_charges` — Stripe-style payments (3000 rows)

Each via `SyntheticDataGeneratorComponent`. Landed into a shared local DuckDB (`data/warehouse.duckdb`) via three `DuckDBTableWriterComponent` sinks as `raw_customers`, `raw_orders`, `raw_stripe_charges`.

**Transform layer** — real dbt:
- Small dbt project in `dbt_project/` written by the setup script
- Uses the **built-in** [`dagster_dbt.DbtProjectComponent`](https://docs.dagster.io/integrations/libraries/dbt) — the official dagster-dbt integration component, not a DCC wrapper
- 4 models: `stg_customers`, `stg_orders`, `stg_charges`, and the join `fct_customer_daily`
- All materialized in the same DuckDB — Dagster auto-loads them as assets with dbt-native lineage

**Fan-out layer** — [`DynamicFanoutAssetComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dynamic_fanout_asset):
- `discover_cohorts()` emits 4 cohorts (high_value / medium / low / at_risk) based on `total_activity_value` thresholds
- `rank_cohort_default()` queries `fct_customer_daily` per cohort, ranks customers by lifetime value, writes a CSV to `extracts/`
- `summarize_batch_default()` returns a summary DataFrame

Runtime-decided fan-out (Dagster's answer to Prefect's `task.map()`). The 4 cohorts are computed from the fact table's data — not declared at plan time.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_customer_360_demo.sh \
  -o setup_customer_360_demo.sh
bash setup_customer_360_demo.sh
```

Requirements: `uv`. **No API keys.** ~1 min first run.

## What you'll see in the UI

`dg dev` at http://localhost:3000. Assets tab shows five asset groups:

- **vendor_sources** — 3 synthetic generators (unpartitioned)
- **raw_warehouse** — 3 DuckDB landings
- **default** (or dbt group) — 4 dbt models with column-level lineage from the manifest
- **cohort_extracts** — 1 graph_asset with `_discover → 4x process → _collect` inside

Click `cohort_extracts` → materialize → the run view shows the fan-out live: 4 parallel `process` op boxes, one per cohort. Total: ~4-5 seconds; each cohort's CSV lands on disk.

Click `fct_customer_daily` → click "View lineage" → the full graph from the 3 vendor sources through raw → staging → mart is visible, columns tracked through dbt's manifest.

## What this demo shows an SE

- **Cross-vendor ingest**: 3 synthetic sources → DuckDB, all as assets with lineage
- **dbt integration**: real dbt, real DuckDB, real column-level lineage — the standard `dagster_dbt.DbtProjectComponent` (no fancy wrapper)
- **Dynamic fan-out**: cohorts decided at runtime from the fact table's data, N parallel process ops in one run
- **Sensor-driven scheduling would be one-line to add**: turn on a `DailyPartitionsDefinition` at any layer + a schedule — the graph shape stays identical
- **Every layer is a component** — the setup script writes ~40 lines of YAML per layer and ~50 lines of Python for the fan-out callables

## What's intentionally NOT in this demo

Kept simple for the "typical Dagster" story. Layer any of these on later if the conversation goes there:

- **Partitions**: daily/hourly per-source partitions. Add `partition_type: daily` to the `SyntheticDataGeneratorComponent` YAML — that's it. dbt models auto-inherit through the manifest.
- **Sensors**: watch S3/SFTP for new files → register dynamic partitions → auto-materialize. [`filesystem_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/filesystem_monitor) shows the pattern.
- **Freshness policies + asset checks**: add `freshness_max_lag_minutes` on the fact model and a data-quality asset check on top.
- **Reverse ETL**: swap or add sinks — `dataframe_to_snowflake`, `dataframe_to_bigquery`, `hubspot_writer` — all as YAML.
- **Notification**: on cohort completion, a `slack_message` component posts a summary — one YAML file.

## Layers of the story to walk through

1. **Multi-source ingest** — start with "here's 3 vendors, all landing in the warehouse as first-class assets."
2. **dbt integration** — "and here's where a real dbt project fits — same asset graph, native lineage from the dbt manifest."
3. **Dynamic fan-out** — "and when we need runtime-decided parallelism (per-cohort, per-region, per-tenant), the fan-out asset lives in the same graph."
4. **Everything is YAML** — walk them through the setup script's `defs.yaml` files. Each layer is 10-20 lines.

## See also

- **[agentic_router.md](agentic_router.md)** — the agentic pattern (LLM router + human gate + sensor) on top of similar primitives.
- **[agentic_batch_triage.md](agentic_batch_triage.md)** — the same batch fan-out pattern applied to per-case LLM triage.

## Files worth reading in the scaffolded project

- `src/customer_360_demo/defs/dbt/defs.yaml` — one-line dbt project reference (via `dagster_dbt.DbtProjectComponent`)
- `dbt_project/models/marts/fct_customer_daily.sql` — the fact join across all 3 vendors
- `src/customer_360_demo/helpers.py` — the 3 fan-out callables (~50 lines)
- `src/customer_360_demo/defs/cohort_extracts/defs.yaml` — the `DynamicFanoutAssetComponent` wiring
