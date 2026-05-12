# DataFrame basics — 9 fundamental shape-preserving transforms

**Validated end-to-end** — RUN_SUCCESS in seconds. Synthetic 60-row sales
dataset fans out through 9 standard pandas operations, each exposed as a
typed Dagster component.

```
sales (synthetic source: 3 regions × 4 categories × 5 days)
       │
       ├── sales_filtered      ← filter (active rows + revenue > 50)
       ├── sales_sorted        ← sort (by date, revenue desc)
       ├── sales_unique        ← unique_dedup (subset=[transaction_id])
       ├── sales_slim          ← select_columns (5-col subset)
       ├── sales_cleansed      ← data_cleansing (trim/normalize/fillna)
       ├── revenue_by_region   ← summarize (group_by + aggregate)
       ├── ranked_categories   ← rank (per-region rank by revenue)
       └── running_revenue     ← running_total (cumulative per region)

monthly_metrics (long-format)
       │
       └── metrics_transposed  ← transpose (long → wide)
```

## Components covered (9)

| Component | Pandas equivalent |
|---|---|
| `filter` | `df.query("...")` |
| `sort` | `df.sort_values(...)` |
| `unique_dedup` | `df.drop_duplicates(...)` |
| `select_columns` | `df[[cols]]` / `df.drop(cols)` |
| `data_cleansing` | trim + case-normalize + fillna pipeline |
| `summarize` | `df.groupby(...).agg(...)` |
| `rank` | `df.groupby(...).rank(...)` |
| `running_total` | `df.groupby(...).cumsum()` |
| `transpose` | `df.set_index(...).T.reset_index()` |

## Cost

**$0.** Pandas only.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dataframe_basics_demo.sh | bash
cd dataframe-basics-demo
uv run dg launch --assets '*'
uv run dg dev   # http://localhost:3000
```

## Why one demo per family

Each component is a thin pydantic-backed wrapper around a single pandas
operation. Composed together, they replace much of the bespoke
"ETL helper" Python that data teams typically write — and they show up
as first-class assets with lineage, partitioning, and freshness in the
Dagster catalog.
