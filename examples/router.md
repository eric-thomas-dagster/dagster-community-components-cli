# Router demo

Multi-output conditional split on 30 synthetic orders — `router` emits one
asset per route, exclusive matching (each row goes to exactly one bucket).

Equivalent to ADF's Conditional Split / Informatica Router.

```
                  ┌─→ high_value_orders   → CSV
csv → router ─────┼─→ medium_value_orders → CSV
                  └─→ low_value_orders    → CSV  (default — catch-all)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `file_ingestion` | ingestion | 30 synthetic orders with mixed totals |
| 2 | `router` | transformation | Split into high/medium/low by `total` |
| 3-5 | `dataframe_to_csv` × 3 | sink | One CSV per bucket |

## Routes

```yaml
routes:
  - asset_name: high_value_orders
    condition: "total > 1000"
  - asset_name: medium_value_orders
    condition: "total >= 100 and total <= 1000"
default_asset_name: low_value_orders
exclusive: true   # each row goes to exactly one bucket (first-match wins)
```

`exclusive: false` would allow overlap (a row matching multiple conditions appears in multiple outputs).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_router_demo.sh | bash
cd router-demo && uv run dg launch --assets '*'
```

## Expected output

3 CSVs in `/tmp/router_demo/`. Sum of rows = input row count (no row lost or
duplicated under `exclusive: true`).

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Generate 400 synthetic orders (schema_type: orders). Split them into three
    routes by total:
      - high_value_orders when total > 500
      - medium_value_orders when total >= 100 and total <= 500
      - low_value_orders as the default (everything else)
    Then write each of the three routes to its own CSV file:
      /tmp/high_orders.csv, /tmp/medium_orders.csv, /tmp/low_orders.csv
  include_ids: ['synthetic_data_generator', 'router']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **4/4 clean picks in 9s, ~$0.0038 total cost.** Outputs written: `/tmp/high_orders.csv`, `/tmp/medium_orders.csv`, `/tmp/low_orders.csv`.

> **Note:** Router itself works cleanly — 3 routes with correct row counts. When re-running you may see the LLM occasionally stop after 2 of 3 sinks; the placeholder diagnostics asset spells out which paths are missing so you can prompt more explicitly.

After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.
