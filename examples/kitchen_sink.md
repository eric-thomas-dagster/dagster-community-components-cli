# Kitchen Sink demo

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
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | Generate 2000 synthetic orders |
| 2 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | Generate 600 synthetic customers |
| 3 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | Generate 200 synthetic products |
| 4 | [`unique_dedup`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/unique_dedup) | transformation | Drop duplicate order_ids |
| 5 | [`type_coercer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/type_coercer) | transformation | Parse order_date + numeric cols |
| 6 | [`outlier_clipper`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/outlier_clipper) | transformation | IQR-clip extreme totals |
| 7 | [`data_cleansing`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/data_cleansing) | transformation | Trim + lowercase customer fields |
| 8 | [`dataframe_join`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_join) | transformation | Join orders to customers |
| 9 | [`filter`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/filter) | transformation | Keep delivered orders only |
| 10 | [`select_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/select_columns) | transformation | Slim to reporting columns |
| 11 | [`sort`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/sort) | transformation | Sort by date desc |
| 12 | [`summarize`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/summarize) | transformation | Revenue by category |
| 13 | [`summarize`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/summarize) | transformation | Revenue by city/state |
| 14 | [`rank`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/rank) | transformation | Rank top categories by revenue |
| 15 | [`rfm_segmentation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/rfm_segmentation) | analytics | Customer RFM segments |
| 16 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write category report |
| 17 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write city report |
| 18 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write top-categories report |
| 19 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write RFM report |
| 20 | [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | sink | Write recent-orders report |
| 21 | [`cron_schedule`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/schedules/cron_schedule) | infrastructure | Daily 7am refresh |

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
