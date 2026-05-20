# Polars-native pushdown — predicate-pushdown source + lazy pipeline

The polars-specific components that earn their keep beyond the per-asset `backend: polars` field.

## Why these components exist

The per-asset `backend: polars` on `filter` / `summarize` / `top_n_per_group` / etc. gets you polars's vectorized execution. But pushdown only works **within** a single lazy query graph; spread across separate Dagster assets, the asset boundary forces materialization at every step and the lazy chain is broken.

These two components fill that gap:

- `polars_scan_parquet` — push predicate + column projection to the parquet reader. Only matching pages come off disk; unread columns never get loaded. (S3 / GCS / ADLS work too via `storage_options:`.)
- `polars_pipeline` — multi-step LazyFrame chain in a single asset. Whole sequence is planned together; filter fusion + projection pruning + parallelism.

Use them together: scan_parquet feeds polars_pipeline (or any polars-aware downstream), and the predicate from polars_pipeline can in some cases push back through into the parquet reader because both halves are in the same lazy graph.

## Components exercised (4)

| Step | Component | What's pushed down |
|---|---|---|
| 1 | `synthetic_data_generator` | n/a (seed) |
| 2 | `dataframe_to_parquet` | n/a (write) |
| 3 | `polars_scan_parquet` | predicate (`status = 'paid' AND total > 50`) + column projection — parquet reader skips non-matching row groups |
| 4 | `polars_pipeline` | 5-op chain: filter → with_columns → group_by → sort → head, executed as ONE lazy collect |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_polars_pushdown_demo.sh | bash
cd polars-pushdown-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

Local FS parquet, 2000 input rows → RUN_SUCCESS. Logs explicitly show:

```
polars_scan_parquet: scanning ./orders.parquet
  projection pushdown: 5 columns
  predicate pushdown: status = 'paid' AND total > 50
...
polars_pipeline: applying 5 operation(s) as one lazy chain
  step 1/5: filter
  step 2/5: with_columns
  step 3/5: group_by
  step 4/5: sort
  step 5/5: head
```

## Retargeting at cloud storage

`polars_scan_parquet` accepts cloud URIs natively:

```yaml
path: s3://bucket/orders/year=2026/*.parquet
storage_options:
  aws_access_key_id: AKIA...
  aws_secret_access_key: ...
  region: us-east-1
```

GCS uses `gs://...` + `GOOGLE_SERVICE_ACCOUNT_KEY`; ADLS uses `az://container@account.dfs.core.windows.net/...`. The pushdown still applies — the polars reader translates predicates into HTTP range requests where possible.

## Companion: per-asset `backend: polars`

For the simple case (one transform per Dagster asset, polars-native execution), the per-asset transforms work great:

```yaml
type: dagster_component_templates.SummarizeComponent
attributes:
  backend: polars
  ...
```

`summarize` / `filter` / `top_n_per_group` / `unique_dedup` / `pct_change` / `dataframe_describe` / `dataframe_join` / `dataframe_union` all accept `backend: polars` (with summarize as the original pattern-proving shot). Use those when you want per-step Dagster lineage; use `polars_pipeline` when you want one asset for the whole chain with full lazy optimization.

## Companion: source-side SQL pushdown

For the SQL-source equivalent of polars_scan_parquet, see [`polars_read_database`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/sources/polars_read_database/README.md) — push the predicate INTO the SQL query, get polars out via connectorx (no pandas intermediate).
