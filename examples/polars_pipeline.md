# Polars Pipeline (single-asset multi-step lazy chain)
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Run multiple polars operations as **one** LazyFrame chain inside a single Dagster asset. The polars query planner fuses filters, prunes projections, and parallelizes execution — but only within one lazy chain. Spread across separate Dagster assets, the asset boundary forces materialization and breaks the optimization.

## Components used

- `polars_pipeline`
- `synthetic_data_generator`

## When to use

- Multiple polars ops that are tightly coupled (filter → group_by → sort → head)
- You care about throughput more than per-step Dagster lineage
- You want polars's full query optimization (fusion + parallelism)

For per-step lineage, use the per-asset `backend: polars` field on `filter` / `summarize` / `top_n_per_group` etc. — each is its own asset, no fusion across.

## Components exercised (2)

- `synthetic_data_generator` — seed 1000 synthetic orders
- `polars_pipeline` — apply filter → group_by → sort → head_per_group as one lazy chain

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_polars_pipeline_demo.sh | bash
cd polars-pipeline-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

RUN_SUCCESS on 1000 rows. The polars planner fuses the 4 operations into a single execution; the asset emits a polars DataFrame (`output_type: polars`) so downstream polars-aware chains can continue without conversion.

## What's in the YAML

```yaml
type: dagster_component_templates.PolarsPipelineComponent
attributes:
  asset_name: top_status_per_category
  upstream_asset_key: orders
  operations:
    - op: filter
      predicate: "total > 100"
    - op: group_by
      group_by: [category, status]
      aggregations:
        revenue:     {col: total, agg: sum}
        order_count: {col: order_id, agg: count}
    - op: sort
      by: [category, revenue]
      descending: [false, true]
    - op: head_per_group
      group_by: [category]
      n: 2
  output_type: polars
  include_preview_metadata: true
```

## Supported ops

`filter` / `with_columns` / `select` / `drop` / `rename` / `group_by` / `sort` / `head` / `tail` / `head_per_group` / `unique` / `drop_nulls` / `fill_null` / `cast`. See [the component README](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/polars_pipeline/README.md) for the full op vocabulary.

## Streaming

Set `streaming: true` to use polars's streaming engine — out-of-core execution for frames larger than memory. Combine with `polars_scan_parquet` upstream for the full predicate-pushdown + streaming story.

## Component reference

### polars_pipeline

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Output Dagster asset name |
| `upstream_asset_key` | string | yes | Upstream asset key (pandas or polars DataFrame) |
| `operations` | list[dict] | yes | Ordered ops applied as one lazy chain. Each is `{op: <kind>, ...params}` |
| `output_type` | enum | no | `polars` (default) or `pandas`. Polars preserves type for downstream chains |
| `streaming` | bool | no | Use polars's streaming engine on the final `.collect()`. Default `false` |
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
| `group_by` | `group_by: [cols]`, `aggregations: {out: {col, agg}}` | Aggregations as `out_col: {col, agg}` |
| `sort` | `by: [cols]`, `descending: bool/list` | Sort |
| `head` / `tail` | `n: int` | First/last N rows |
| `head_per_group` | `group_by: [cols]`, `n: int` | Top-N per group (sort first if order matters) |
| `unique` | `subset: [cols]`, `keep: first/last/none` | Dedup |
| `drop_nulls` | `subset: [cols]` (optional) | Drop rows with null in subset |
| `fill_null` | `value: <any>` | Fill nulls |
| `cast` | `mapping: {col: 'Int64'}` | Type cast |

Supported `agg` values in `group_by`: `sum / mean / avg / min / max / count / median / std / var / first / last / nunique`.

### Component README (full reference)

[polars_pipeline](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/polars_pipeline/README.md)

## See also

<!-- TODO: link related walkthroughs -->
