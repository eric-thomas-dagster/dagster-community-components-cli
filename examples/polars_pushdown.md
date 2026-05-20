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

## Component reference

### polars_scan_parquet

Read parquet via polars's lazy scanner. When you supply `predicate:` and/or `columns:`, polars's query planner pushes them down to the parquet reader — only matching row groups + selected columns come off disk.

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Output Dagster asset name |
| `path` | string | yes | Local path / glob / cloud URI (`s3://`, `gs://`, `az://`). Globs supported (`**/*.parquet`) |
| `columns` | list[string] | no | Projection pushdown — only these columns are read off disk. None = read all |
| `predicate` | string | no | SQL predicate (polars SQL dialect) pushed down to the parquet reader |
| `storage_options` | dict[string, string] | no | Cloud auth options forwarded to object_store-rs. E.g. AWS: `{aws_access_key_id, aws_secret_access_key, region}` |
| `output_type` | enum | no | `polars` (default) or `pandas`. Polars output preserves the type for downstream polars chains |
| `n_rows` | int | no | LIMIT pushdown — reader stops after this many rows |
| `streaming` | bool | no | Use polars's streaming engine for out-of-core execution. Default `false` |
| `group_name`, `description`, `asset_tags`, `kinds`, `owners`, `deps` | (standard) | no | Standard Dagster metadata fields |
| `include_preview_metadata` | bool | no | Emit a head() preview in metadata. Default `false` |
| `preview_rows` | int (1–500) | no | Rows in preview. Default `25` |

### polars_pipeline

Multi-step LazyFrame chain inside a single Dagster asset. Polars's query planner fuses + parallelizes the whole sequence.

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Output Dagster asset name |
| `upstream_asset_key` | string | yes | Upstream asset key (pandas or polars DataFrame) |
| `operations` | list[dict] | yes | Ordered ops applied as one lazy chain. Each is `{op: <kind>, ...params}` |
| `output_type` | enum | no | `polars` (default) or `pandas`. Polars preserves type for downstream chains |
| `streaming` | bool | no | Use polars's streaming engine on the final `.collect()` for out-of-core frames. Default `false` |
| `group_name`, `description`, `asset_tags`, `kinds`, `owners`, `deps` | (standard) | no | Standard Dagster metadata fields |
| `include_preview_metadata` | bool | no | Default `false` |
| `preview_rows` | int (1–500) | no | Default `25` |

Supported `operations[*].op` values:

| `op` | Params | Notes |
|---|---|---|
| `filter` | `predicate: "<SQL>"` | Polars SQLContext over the condition string |
| `with_columns` | `expressions: {name: <SQL expr>}` | Add/replace columns from SQL expressions |
| `select` | `columns: [a, b]` | Keep only these columns |
| `drop` | `columns: [a, b]` | Drop these columns |
| `rename` | `mapping: {old: new}` | Rename columns |
| `group_by` | `group_by: [cols]`, `aggregations: {out: {col, agg}}` | Same agg shape as `summarize` |
| `sort` | `by: [cols]`, `descending: bool/list` | Sort |
| `head` / `tail` | `n: int` | First/last N rows |
| `head_per_group` | `group_by: [cols]`, `n: int` | Top-N per group |
| `unique` | `subset: [cols]`, `keep: first/last/none` | Dedup |
| `drop_nulls` | `subset: [cols]` (optional) | Drop rows with null in subset |
| `fill_null` | `value: <any>` | Fill nulls |
| `cast` | `mapping: {col: 'Int64'}` | Type cast |

Supported `agg` values in `group_by`: `sum / mean / avg / min / max / count / median / std / var / first / last / nunique`.

### Component READMEs (full reference)

- [polars_scan_parquet](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/sources/polars_scan_parquet/README.md)
- [polars_pipeline](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/polars_pipeline/README.md)
- [polars_read_database](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/sources/polars_read_database/README.md) — SQL-source equivalent of scan_parquet

## Demo notes

- **Pushdown only fires within one lazy graph.** `polars_scan_parquet` → `polars_pipeline` work because both halves can be in the same chain. Spread across two Dagster assets, the asset boundary forces materialization between them — the predicate from polars_pipeline can't push back through the boundary into the parquet reader.
- **Parquet predicate coverage is statistics-dependent.** Polars uses parquet column min/max stats per row group to skip non-matching pages. Range/equality predicates on indexed columns push best. Computed-column predicates (e.g. `EXTRACT(YEAR FROM date) = 2026`) can't push to the reader — they evaluate after read.
- **Streaming for out-of-core frames.** `streaming: true` works for frames larger than memory. Combine with predicate + projection pushdown so only matching pages stream in.
- **Output type matters for downstream chains.** `output_type: polars` preserves the polars DataFrame so the next polars-aware asset doesn't pay a conversion cost. `output_type: pandas` is the safe default for mixed downstream consumers.
