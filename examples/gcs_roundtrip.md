# GCS Round-trip — DataFrame → GCS → BQ → BQ EXPORT → GCS

**Validated end-to-end against real APIs** (servicepulse-490502, servicepulse-490502-dagster-demo bucket).
DataFrame written as parquet, loaded into BigQuery, exported back as a CSV summary.

```
transactions           ← synthetic_data_generator (transactions schema, 20 rows)
       │
       └── transactions_in_gcs  ← dataframe_to_gcs (parquet)
                │
                └── transactions_in_bq      ← bigquery_load_from_gcs_asset
                         │
                         └── transactions_exported  ← bigquery_export_to_gcs_asset
                                                       (aggregate query → CSV)
```

## Components covered (4)

| Component | What it does |
|---|---|
| [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | Synthetic upstream. `schema_type: transactions` produces `(transaction_id, account_id, timestamp, type, amount, merchant, category, status)` — common shape for warehouse ingest demos. |
| [`dataframe_to_gcs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_gcs) | Write a DataFrame to GCS as parquet / csv / json. Bucket from env var. |
| [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) | BQ LOAD job from one or more GCS URIs. Format autodetect, partitioning, schema overrides. |
| [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset) | BQ EXPORT job from a table or query to GCS (parquet / csv / json / avro). Data never round-trips through the executor. |

## Live run output

| Step | Result |
|---|---|
| `sales_in_gcs` | 10 rows → `gs://servicepulse-490502-dagster-demo/sales/sales.parquet` |
| `sales_in_bq` | parquet loaded → `servicepulse-490502.dagster_demo.sales_from_gcs` |
| `sales_exported` | `GROUP BY region` aggregate → `gs://.../sales_summary_*.csv` |

CSV contents verified:
```
region,revenue_cents,n
us-east,11250,5
us-west,10000,5
```

## Bugs surfaced fixing this demo

1. **[`dataframe_to_gcs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_gcs) needs `gcsfs` at runtime** but `pip install dataframe_to_gcs` doesn't pull it transitively (lives behind pyarrow's fsspec layer for parquet writes to gs://). The setup script now `uv add`s it explicitly. Worth a note in the component's README too.

## Why this pattern matters

This is the canonical "warehouse ingest from GCS" loop:
1. **Producer** lands data in GCS (this demo: a Dagster asset; in production: Pub/Sub → Dataflow → GCS, or external vendor SFTP-to-GCS, etc.)
2. **Loader** issues a BQ LOAD job — data is read by BigQuery's own workers, never by Dagster's executor. Free for parquet/avro/orc.
3. **Exporter** publishes aggregates back to GCS for downstream consumers (other warehouses, BI tools, customers).

All three steps run as Dagster assets with lineage. Each operation is delegated to BQ/GCS native APIs — no data ever flows through Dagster's worker, so cost is independent of asset size.

## Cost

**< $0.01 for this demo.** GCS storage is pennies at this volume; BQ LOAD + EXPORT jobs are free; the aggregate query scans < 1 KB.

## Required setup (one-time)

```bash
# 1. Enable APIs
# https://console.cloud.google.com/apis/library/storage.googleapis.com
# https://console.cloud.google.com/apis/library/bigquery.googleapis.com

# 2. Create bucket + dataset
gcloud storage buckets create gs://$GCS_BUCKET --location=us-central1
bq --location=US mk --dataset $GCP_PROJECT_ID:$BQ_DATASET

# 3. IAM (on the service account)
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/storage.objectAdmin"
bq update --dataset --add-iam-policy-binding \
  --member="serviceAccount:$SA_EMAIL" --role="roles/bigquery.dataEditor" \
  $GCP_PROJECT_ID:$BQ_DATASET
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/bigquery.jobUser"
```

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
export GCS_BUCKET=your-bucket-name      # no gs:// prefix
export BQ_DATASET=dagster_demo
```

## Run it

```bash
./setup_gcs_roundtrip_demo.sh
cd gcs-roundtrip-demo
uv run dg launch --assets '*'

gcloud storage cat gs://$GCS_BUCKET/sales/sales_summary_000000000000.csv
```

## Sister components

- [`gcs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/gcs_to_database_asset) — read GCS → write to any SQLAlchemy DB
- [`dataframe_to_bigquery`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_bigquery) — direct DataFrame → BQ load (no GCS hop)
- [`bigquery_create_table_from_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/bigquery_create_table_from_query_asset) — CTAS in-warehouse, no GCS hop
- [`bigquery_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_query_asset) — read BQ → DataFrame
