# Local transforms + sinks — DataFrame pipeline without cloud or auth
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

A full DataFrame transform chain that lives entirely on the local filesystem. Every intermediate asset is persisted as Parquet by the project's IO manager, filter + summarize transforms are wired declaratively, and the final stage writes Avro on disk. No SaaS, no cloud, no auth, no warehouse.

## Components used

| Component | Source | Role |
|---|---|---|
| `synthetic_data_generator` | community | Seeds 1000 synthetic orders |
| `local_parquet_io_manager` | community | Project IO manager — persists every asset as Parquet |
| `filter` | community | Row-level filter via pandas `df.query` |
| `summarize` | community | Group-by + aggregate (SQL `GROUP BY` equivalent) |
| `dataframe_to_avro` | community | Sink — writes a DataFrame to a local `.avro` file |

## Architecture

```
   ┌────────────────────────┐
   │ synthetic_orders       │  1000 rows × 10 cols
   │ (order_id, customer_id,│
   │  total, status, …)     │
   └───────────┬────────────┘
               │ filter (total > 50)
               ▼
   ┌────────────────────────┐
   │ high_value_orders      │  ~750 rows (filtered)
   └───────────┬────────────┘
               │ summarize (group by customer_id)
               ▼
   ┌────────────────────────┐
   │ revenue_by_customer    │  N customers, sum(total), count(orders)
   └───────────┬────────────┘
               │ dataframe_to_avro (codec=deflate)
               ▼
   ┌────────────────────────┐
   │ revenue_summary_avro   │  /tmp/revenue_summary.avro
   └────────────────────────┘
```

Every asset above (except the avro sink) is automatically persisted to `/tmp/local-transforms-storage/<asset>.parquet` by the `local_parquet_io_manager`.

## Run

```bash
bash setup_local_transforms_demo.sh
cd local-transforms-demo

uv run dg check defs
uv run dg launch --assets '*'
```

Expect `RUN_SUCCESS` with 4 materialized assets.

Inspect on disk:

```bash
ls -la /tmp/local-transforms-storage/
# high_value_orders.parquet     38 KB
# revenue_by_customer.parquet   10 KB
# synthetic_orders.parquet      39 KB

ls -la /tmp/revenue_summary.avro
# 4.6 KB

# Read the Avro back
uv run python -c "
import fastavro
with open('/tmp/revenue_summary.avro','rb') as f:
    for rec in fastavro.reader(f):
        print(rec)
" | head
```

## Why each component

### `local_parquet_io_manager`

Wires `resource_key: io_manager` — making it the **default** IO manager for the whole project. Every asset that returns a DataFrame is automatically serialized as Parquet under `base_dir`. Downstream assets get the same DataFrame back when they declare an `ins=` on the upstream.

For dev / notebook-style iteration this is ideal: no warehouse, no DBT model, but you still get persistence and lineage between runs. To retarget at S3, swap to `s3_parquet_io_manager`; at ADLS, `azure_blob_parquet_io_manager`; at GCS, `gcs_parquet_io_manager`. Same component shape.

The IO manager treats `obj is None` outputs as no-ops, so sink components like `dataframe_to_avro` (which write their own files and return `Output(value=None)`) coexist with the parquet IO manager without colliding.

### `filter`

```yaml
condition: 'total > 50'
negate: false
```

Pandas `df.query()` under the hood. The condition string is evaluated against column names in the upstream DataFrame. Use `negate: true` to invert the predicate.

### `summarize`

```yaml
group_by: [customer_id]
aggregations:
  revenue:     {col: total, agg: sum}
  order_count: {col: order_id, agg: count}
```

Each `aggregations` entry has two forms:

- **Simple**: `revenue: sum` — apply that aggregation to the column with the same name as the output.
- **Named**: `revenue: {col: total, agg: sum}` — output column named `revenue`, computed by aggregating `total` with `sum`. This form is required when you want two different aggregations on the same source column (e.g. `avg_rating` AND `num_ratings` both off `rating`).

Equivalent to SQL `SELECT customer_id, SUM(total) AS revenue, COUNT(order_id) AS order_count FROM ... GROUP BY customer_id`.

### `dataframe_to_avro`

```yaml
file_path: /tmp/revenue_summary.avro
codec: deflate
record_name: RevenueByCustomer
```

Writes the upstream DataFrame to one Avro file. Schema is auto-inferred from the DataFrame's dtypes (override with explicit `avro_schema:` for cross-language compatibility). Codecs: `null | snappy | deflate | bzip2 | xz | zstandard`.

`file_path` accepts any `fsspec`-supported URI — swap `/tmp/...` for `s3://bucket/key.avro`, `gs://bucket/key.avro`, `az://container/key.avro` and the same component writes to remote storage (add the matching `s3fs`/`gcsfs`/`adlfs` extra to requirements).

## Trade-offs & gotchas

- **Local-only persistence.** Parquet files live under `base_dir`. Project IO managers don't read across projects, so to share with another Dagster project, write to a cloud bucket instead.
- **In-process transforms.** `filter` and `summarize` run inside the Dagster worker — fine for ~millions of rows on a laptop. For larger data, push the transform into the warehouse via `sql_command_job` or a dbt model, then read back with `*_ingestion`.
- **Single-file Avro sink.** `dataframe_to_avro` writes the whole frame to one file. For partitioned writes, parameterize `file_path` with `{partition_key}`.
- **Avro schema inference.** Reasonable defaults for primitive types, but rich types (nested structs, lists of records) may need an explicit `avro_schema:`.

## See also

- [`composition_primitives.md`](composition_primitives.md) — small jobs with no auth
- [`lakehouse_local.md`](lakehouse_local.md) — Iceberg + Delta roundtrip
- [`notebooks.md`](notebooks.md) — papermill notebook execution
- [`external_assets.md`](external_assets.md) — declare-only asset family
