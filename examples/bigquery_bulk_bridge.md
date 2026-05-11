# BQ ↔ GCS bulk bridge — vendor-native EXTRACT + LOAD round-trip

**Validated end-to-end against real GCP infrastructure**, full BQ → GCS → BQ
round-trip in <8s. Two new vendor-native components that use
BigQuery's `EXTRACT` job + `LOAD` job APIs directly — data never
round-trips through the Dagster executor, so the same pattern scales
from 150-row iris to multi-TB tables.

```
iris_export_to_gcs   ← bigquery_export_to_gcs_asset
                       (BQ EXTRACT iris_clean → gs://.../bridge/iris_*.parquet)
       │
       └── iris_round_tripped  ← bigquery_load_from_gcs_asset
                                 (BQ LOAD parquet → iris_round_tripped)
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset) | BQ table or query result → GCS (parquet / CSV / JSONL / AVRO). Uses BQ `EXTRACT` job for tables (free, native sharding for >1 GB) or `EXPORT DATA OPTIONS(...) AS <select>` for queries. |
| [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) | GCS objects → BQ table via the native LOAD JOB API. Supports parquet, CSV, JSONL, AVRO, ORC; write_disposition / autodetect / explicit schema / partition + cluster on destination. |

## Validation status — both live

| Step | Result |
|---|---|
| `iris_export_to_gcs` (EXTRACT) | 150 rows × 6,350 B → `gs://.../bridge/iris_000000000000.parquet` (2,643 B compressed) |
| `iris_round_tripped` (LOAD) | 150 rows × 6,350 B in `servicepulse-490502.dagster_demo.iris_round_tripped`, identical schema |

Round-trip total: ~8s for the full chain.

## Cost

**$0.** Iris is 150 rows / <10 KB. BQ EXTRACT is free; BQ LOAD is free;
GCS storage of <3 KB is free.

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

## Required SA roles

| For | Roles |
|---|---|
| Export from BQ | `roles/bigquery.dataViewer` (source) + `roles/bigquery.jobUser` (project) |
| Write to GCS | `roles/storage.objectCreator` (or `roles/storage.objectAdmin`) on the bucket |
| Load to BQ | `roles/bigquery.dataEditor` (destination dataset) + `roles/bigquery.jobUser` (project) |
| Read from GCS | `roles/storage.objectViewer` on the source bucket |

Or simpler for demos: `roles/owner` on the project.

## Run it

```bash
# Pre-req: a BQ source table to export. The default points at the
# iris_clean table created by setup_bigquery_ml_pipeline_demo.sh.
# Override with BQ_SOURCE_TABLE / BQ_TARGET_TABLE / GCS_BUCKET if needed.

./setup_bigquery_bulk_bridge_demo.sh
cd bigquery-bulk-bridge-demo
uv run dg launch --assets '*'
```

Inspect:

```bash
bq query --nouse_legacy_sql \
  'SELECT COUNT(*) AS n FROM dagster_demo.iris_round_tripped'
gsutil ls gs://servicepulse-490502-dagster-demo/bridge/iris_*.parquet
```

## Bug surfaced and fixed validating this demo

`google.cloud.bigquery.ExtractJob` doesn't expose
`total_bytes_processed` — that attribute lives on `QueryJob`. Caught
on the first run; the component now falls back to fetching the
source table's `num_bytes` for metadata display when running in
`source_table_id` mode.

## Why these vendor-native components vs the DataFrame-mediated peers?

The registry has two parallel paths for any warehouse ↔ object-storage
move:

| Pattern | Components |
|---|---|
| **Vendor-native** (data never touches the executor — fast at TB scale) | [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset), [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) |
| **DataFrame-mediated** (works across any warehouse + any cloud) | [`dataframe_to_gcs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_gcs) / `s3` / `adls` + [`gcs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/gcs_to_database_asset) / `s3_*` / `adls_*` |

Pick vendor-native when:
- Data volume > a few hundred MB
- You're staying within one warehouse + one cloud
- You want native partition/cluster preservation, BQ-aware schema autodetect, etc.

Pick DataFrame-mediated when:
- Data is small enough to fit in memory
- You're moving across vendors (e.g., Snowflake → S3 → Postgres)
- You need to manipulate the DataFrame between read and write

## Drop-in extensions

Switch to a query as the export source:

```yaml
# Export only the most recent 30 days as parquet
attributes:
  source_query: |
    SELECT * FROM `my-project.analytics.events`
    WHERE event_date > CURRENT_DATE() - 30
  destination_uri: "gs://my-bucket/exports/recent_events_*.parquet"
  format: parquet
```

Add partitioning + clustering on the destination of a load:

```yaml
attributes:
  source_uris: ["gs://my-bucket/landing/orders/*.parquet"]
  destination_table_id: my-project.warehouse.orders
  format: parquet
  partition_field: order_date
  partition_type: DAY
  cluster_fields: [customer_id, status]
```

## Sister components

- [`bigquery_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_query_asset) — ad-hoc SELECT (no CTAS, no export).
- [`bigquery_create_table_from_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/bigquery_create_table_from_query_asset) — CTAS, materialize a model.
- [`bigquery_ml_train_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_train_asset) / [`bigquery_ml_predict_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_predict_asset) — BQML.
- [`dataframe_to_gcs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_gcs) / [`gcs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/gcs_to_database_asset) — DataFrame-mediated peers.
