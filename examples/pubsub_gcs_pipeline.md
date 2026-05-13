# Pub/Sub → Cloud Storage Subscription → Dagster → BigQuery — blueprint

GCP mirror of the [Event Hubs Capture demo](eh_capture_pipeline.md). Same production-shape pattern: **Dagster doesn't process the queue directly** — a Pub/Sub Cloud Storage Subscription (built-in GCP service) lands every message in GCS as durable Parquet, and Dagster picks up files event-driven. Each new file = one Dagster dynamic partition = fully re-runnable.

> **Validation status:** the Dagster wiring is buildable and `dg check` passes. End-to-end execution requires a real GCP Pub/Sub topic with a Cloud Storage subscription configured + a bucket. Blueprint.

## Why this architecture

Same rationale as the EH Capture demo:

| Problem | Why direct-queue-consumption fails |
|---|---|
| **Partitions must be re-runnable.** | Pub/Sub messages are deleted on acknowledgment. Replay = impossible. |
| **Sensor cursor is small.** | Accumulating payloads across ticks doesn't fit. |
| **Operational concerns.** | Backpressure, ordering, batching, retention — Cloud Storage Subscriptions handle these as a managed service. |

The Cloud Storage Subscription is the right boundary between the streaming layer and Dagster.

## Architecture

```
   ┌──────────────────────────────────────────┐
   │ Producers (any language / runtime)        │
   │   ↓                                       │
   │ Google Pub/Sub topic                      │
   └─────────────────┬─────────────────────────┘
                     │
                     ▼  Cloud Storage Subscription (built-in GCP service)
   ┌──────────────────────────────────────────┐
   │ GCS: gs://bucket/events/                  │
   │   events-2026-05-13T14:00:00Z.parquet     │
   │   events-2026-05-13T14:05:00Z.parquet     │
   │   ...                                     │
   └─────────────────┬─────────────────────────┘
                     │
   ┌─────────────────▼─────────────────────────┐
   │ Dagster                                    │
   │                                            │
   │ gcs_monitor (partition_mode:               │
   │   dynamic_partition) — registers each new  │
   │   Parquet file as a partition, fires       │
   │   RunRequest(partition_key=<name>).        │
   │                ↓                           │
   │ raw_events (file_ingestion partitioned)    │
   │   reads gs://bucket/<partition>.parquet    │
   │                ↓                           │
   │ events_by_type (summarize partitioned)     │
   │   group_by + aggs per file.                │
   │                ↓                           │
   │ events_by_type_parquet                     │
   │   writes curated Parquet to a different    │
   │   GCS path, one file per partition.        │
   └─────────────────┬─────────────────────────┘
                     │
                     ▼ (independent of Dagster — query directly)
   ┌──────────────────────────────────────────┐
   │ BigQuery external table                   │
   │ Standard SQL over the curated GCS path.   │
   └──────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `gcs_monitor` | community | Polls the GCS bucket, registers each new Parquet file as a dynamic partition, yields `RunRequest(partition_key=...)`. |
| `file_ingestion` | community | Partitioned asset reads each Parquet as a pandas DataFrame. |
| `summarize` | community | Per-partition group-by + aggregations. |
| `dataframe_to_parquet` | community | Writes the curated Parquet back to GCS. |

Plus the GCP-native pieces you provide: the Pub/Sub topic, the Cloud Storage Subscription, the GCS bucket, and (optionally) a BigQuery dataset.

## GCP setup (prerequisites you provide)

### 1. Bucket

```bash
export GCP_PROJECT=your-project-id
export GCS_BUCKET=gs-pubsub-events-$(openssl rand -hex 4)
gsutil mb -p "$GCP_PROJECT" -l us-central1 "gs://$GCS_BUCKET"
```

### 2. Pub/Sub topic + Cloud Storage Subscription

```bash
gcloud pubsub topics create my-events --project "$GCP_PROJECT"

# Cloud Storage Subscription writing Parquet:
gcloud pubsub subscriptions create my-events-to-gcs \
  --project "$GCP_PROJECT" \
  --topic my-events \
  --cloud-storage-bucket "$GCS_BUCKET" \
  --cloud-storage-file-prefix "events/events-" \
  --cloud-storage-file-suffix ".parquet" \
  --cloud-storage-output-format parquet \
  --cloud-storage-max-bytes 10485760 \
  --cloud-storage-max-duration 60s
```

Notes:
- `--cloud-storage-max-duration 60s` flushes every minute regardless of size.
- `--cloud-storage-max-bytes 10485760` = 10 MB max per file.
- The Pub/Sub service account (the one shown when you create the subscription) needs `roles/storage.objectAdmin` on the bucket.

### 3. BigQuery dataset (optional — for the analytics surface)

```bash
bq mk --location=US --dataset "${GCP_PROJECT}:events_demo"
```

### 4. Auth

ADC (Application Default Credentials) is the recommended path:

```bash
# Local dev:
gcloud auth application-default login

# GCE / GKE: use Workload Identity, no env vars needed.

# Service account key:
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
```

## Run the demo

```bash
./setup_pubsub_gcs_pipeline_demo.sh
cd pubsub-gcs-pipeline-demo

export GCP_PROJECT="$GCP_PROJECT"
export GCS_BUCKET="$GCS_BUCKET"

uv run dg check defs
uv run dg dev
```

In the UI: enable the `pubsub_gcs_sensor`. Within ~30s it picks up any existing Parquet files in the bucket, registers each as a dynamic partition, and launches the per-partition pipeline. Each new file landed by the Cloud Storage Subscription from this point on triggers another run.

## Querying via BigQuery (independent of Dagster)

```sql
-- One-time setup: create an external table over the curated GCS path
CREATE OR REPLACE EXTERNAL TABLE `your_project.events_demo.events_by_type`
OPTIONS (
  format = 'PARQUET',
  uris = ['gs://your-bucket/curated/events_by_type/*.parquet']
);

-- Then query like any BigQuery table:
SELECT event_type, SUM(event_count) AS total_events
FROM `your_project.events_demo.events_by_type`
GROUP BY event_type
ORDER BY total_events DESC;
```

For better performance, materialize a non-external BigQuery table from the external one on a schedule.

## Trade-offs & gotchas

- **Subscription latency = your minimum batch size.** With `--cloud-storage-max-duration 60s`, files land every minute minimum. Add ~30s–1min for Dagster's sensor + asset.
- **Parquet format requires a fixed schema.** Pub/Sub Cloud Storage Subscription writing Parquet requires that you have a schema attached to the topic (Pub/Sub Schemas, AVRO or Protocol Buffers). Without one, fall back to Avro or text format, but downstream `file_ingestion` will need `format: json` or similar.
- **File-fanout costs.** Lots of small files = lots of GCS list operations. Tune `--cloud-storage-max-duration` / `--cloud-storage-max-bytes` upward if you're seeing this.
- **Schema evolution.** Pub/Sub Schema Registry handles compatibility; downstream `summarize`'s `group_by` may break if it references a removed column.
- **Re-runnable.** Every partition in Dagster's UI can be rematerialized. The Parquet is in GCS forever (or until you lifecycle-delete it).

## See also

- [`eh_capture_pipeline.md`](eh_capture_pipeline.md) — Azure mirror of this demo.
- [`s3_pipeline.md`](s3_pipeline.md) — same dynamic-partition pattern using Minio S3 + manual file uploads (no real streaming source).
