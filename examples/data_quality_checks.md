# Data Quality Checks demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end** — 100 synthetic orders go through 4 different
asset_check components in parallel, all attached to the same asset.

```
synthetic_data_generator → orders_raw → dataframe_to_csv
                              ▲
                              ├── enhanced_data_quality_checks  (4 sub-checks: row_count, null, range, data_type)
                              ├── pandas_dataframe_check        (required cols + dtype)
                              ├── pandera_asset_check           (Pandera schema)
                              └── freshness_check               (time_window SLA)
```

## Components used

| # | Component | Sub-checks created |
|---|---|---|
| 1 | `synthetic_data_generator` | — (source) |
| 2 | `dataframe_to_csv` | — (sink) |
| 3 | `enhanced_data_quality_checks` | `orders_row_count`, `orders_critical_not_null`, `orders_total_in_range`, `orders_dtype` |
| 4 | `pandas_dataframe_check` | `_pandas_check` |
| 5 | `pandera_asset_check` | `_pandera_check` |
| 6 | `freshness_check` | `freshness_check` (declarative SLA: 1h warn, 24h fail) |

## Validated end-to-end

| Check | Result |
|---|---|
| `column_schema_change` | passed (first materialization) |
| `_pandas_check` | passed |
| `_pandera_check` | did not pass (Pandera enforces stricter constraints — demo's intent: show check FAILURE wired up correctly without blocking pipeline) |
| `freshness_check` | passed ("3 seconds ago, within the allowed time range of 1 day") |
| `orders_row_count` | passed (100 within 50–1000 range) |
| `orders_critical_not_null` | passed (no nulls in order_id, customer_id, total) |
| `orders_total_in_range` | passed (all totals in 0–100000) |
| `orders_dtype` | passed (total: float64, status: object) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_data_quality_checks_demo.sh | bash
cd data-quality-checks-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets → orders_raw → Checks
```

## When to use which check component

| Need | Component |
|---|---|
| Rich rule library (row count, null, range, type, anomaly, correlation, custom) with selection-by-tag/group | `enhanced_data_quality_checks` |
| Lightweight pandas dtype + required-cols enforcement | `pandas_dataframe_check` |
| Schema-as-code with Pandera DataFrameSchema (typed columns + validators) | `pandera_asset_check` |
| Declarative freshness SLA (rolling window OR cron deadline) | `freshness_check` |
| Great Expectations expectation suite | `great_expectations_check` |
| Vendor-specific (Acceldata / Monte Carlo / Sifflet / Soda) | per-vendor check component |

## Cost

$0 — entirely local.

## See also

<!-- TODO: link related walkthroughs -->
