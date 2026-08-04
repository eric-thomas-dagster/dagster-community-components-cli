# Kitchen Sink demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

The showcase demo: **21 community components in one project**. An e-commerce intelligence
pipeline that exercises ingest → quality → join → transform → analytics → sink → schedule —
the same shape your real pipelines look like, just compressed into one autoloaded `dg` project.

Synthetic data only — no API keys, no external services, runs offline.

Pipeline (21 components, all autoloaded by `dg`):

```
synthetic_data_generator (orders, 2000)    ─┐
synthetic_data_generator (customers, 600)   ├─ INGEST
synthetic_data_generator (products, 200)   ─┘
      │
      ├─→ unique_dedup → type_coercer → outlier_clipper ─┐
      ├─→ data_cleansing                                 │
      │                                                  ├─→ dataframe_join
      │                                                  │   (orders ⋈ customers)
      │                                                  │        │
      │                                                  │        ├─→ filter (delivered)  → CSV
      │                                                  │        ├─→ summarize (category)→ CSV
      │                                                  │        ├─→ summarize (city)    → CSV
      │                                                  │        ├─→ rank (top cats)     → CSV
      │                                                  │        ├─→ select_columns ─┐
      │                                                  │        ├─→ sort (recent)   │
      │                                                  │        └─→ rfm_segmentation → CSV
      │                                                  │                            │
      │                                                  │                            └─→ CSV
      │
      └─→ cron_schedule (daily 7am refresh of all 5 reports)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate 2000 synthetic orders |
| 2 | `synthetic_data_generator` | ai | Generate 600 synthetic customers |
| 3 | `synthetic_data_generator` | ai | Generate 200 synthetic products |
| 4 | `unique_dedup` | transformation | Drop duplicate order_ids |
| 5 | `type_coercer` | transformation | Parse order_date + numeric cols |
| 6 | `outlier_clipper` | transformation | IQR-clip extreme totals |
| 7 | `data_cleansing` | transformation | Trim + lowercase customer fields |
| 8 | `dataframe_join` | transformation | Join orders to customers |
| 9 | `filter` | transformation | Keep delivered orders only |
| 10 | `select_columns` | transformation | Slim to reporting columns |
| 11 | `sort` | transformation | Sort by date desc |
| 12 | `summarize` | transformation | Revenue by category |
| 13 | `summarize` | transformation | Revenue by city/state |
| 14 | `rank` | transformation | Rank top categories by revenue |
| 15 | `rfm_segmentation` | analytics | Customer RFM segments |
| 16 | `dataframe_to_csv` | sink | Write category report |
| 17 | `dataframe_to_csv` | sink | Write city report |
| 18 | `dataframe_to_csv` | sink | Write top-categories report |
| 19 | `dataframe_to_csv` | sink | Write RFM report |
| 20 | `dataframe_to_csv` | sink | Write recent-orders report |
| 21 | `cron_schedule` | infrastructure | Daily 7am refresh |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_kitchen_sink_demo.sh | bash
cd kitchen-sink-demo
uv run dg launch --assets '*'
```

Or browse the lineage graph in the UI:

```bash
cd kitchen-sink-demo
uv run dg dev
```

## Outputs

Five CSVs written to `/tmp`:

- `kitchen_sink_revenue_by_category.csv` — total revenue + order count per category
- `kitchen_sink_revenue_by_city.csv` — total revenue + order count per city/state
- `kitchen_sink_top_categories.csv` — categories ranked by revenue
- `kitchen_sink_rfm.csv` — per-customer RFM scores + segment label
- `kitchen_sink_orders_recent.csv` — completed orders sorted by date desc

```bash
head /tmp/kitchen_sink_revenue_by_category.csv
head /tmp/kitchen_sink_rfm.csv
```

## Why this demo exists

Most demos focus on one technique (forecasting, ML, geospatial, etc.).
This one is the breadth showcase — proof that the registry components
compose like Lego blocks, regardless of how many you stack.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build an e-commerce intelligence pipeline:
      1. Generate 2000 synthetic orders, 600 synthetic customers, and
         200 synthetic products (schema_types: orders, customers, products).
      2. Drop duplicate orders on order_id.
      3. Coerce total, subtotal, num_items to numeric.
      4. Clip outliers on the total column using IQR.
      5. Cleanse customer text columns (trim + lowercase first_name, last_name, city, state).
      6. Join the deduped/coerced/clipped orders with the cleansed customers on customer_id.
      7. From the joined data, produce these branched outputs:
           A. Filter to delivered (status=='paid') orders → /tmp/ks_delivered.csv
           B. Summarize by category: sum(total), count → /tmp/ks_revenue_by_category.csv
           C. Summarize by city and state: sum(total), count → /tmp/ks_revenue_by_city.csv
           D. Rank the top 5 categories by revenue → /tmp/ks_top_categories.csv
           E. Select reporting columns [customer_id, first_name, email, city, state, order_date, total],
              then sort by order_date descending → /tmp/ks_recent_orders.csv
           F. Run rfm_segmentation on orders (customer_id + order_date + total) → /tmp/ks_rfm.csv
  include_ids: ['synthetic_data_generator', 'rfm_segmentation']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **16/22 clean picks in 43s, ~$0.016 total cost.** Outputs written: `/tmp/ks_delivered.csv`, `/tmp/ks_revenue_by_category.csv`, `/tmp/ks_revenue_by_city.csv`, `/tmp/ks_recent_orders.csv`, `/tmp/ks_rfm.csv`.

> **Note:** Kitchen Sink is at the edge of what gpt-4o-mini handles reliably. 5 of the 6 requested CSVs land end-to-end; the 6th (rank by revenue → top-categories.csv) is where mini occasionally stops early. Upgrade to gpt-4o for full coverage.

After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.

## See also

<!-- TODO: link related walkthroughs -->
