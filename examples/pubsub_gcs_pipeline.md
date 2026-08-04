# Pub/Sub → Cloud Storage Subscription → Dagster → BigQuery — blueprint
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

GCP mirror of the [Event Hubs Capture demo](eh_capture_pipeline.md). Same production-shape pattern: **Dagster doesn't process the queue directly** — a Pub/Sub Cloud Storage Subscription (built-in GCP service) lands every message in GCS as durable Parquet, and Dagster picks up files event-driven. Each new file = one Dagster dynamic partition = fully re-runnable.

> **Validation status:** validated end-to-end 2026-05-13 against a real GCP project. Provisioned the topic + Cloud Storage subscription (JSON-Lines mode) + bucket, published 150 messages via `gcloud pubsub topics publish`, watched the subscription land multiple JSONL files in GCS within 60s. `file_ingestion` (`format: jsonl`) read 11 rows × 4 cols directly from a `gs://` URI in a `dg dev` Dagster process, materializing the `raw_events` partitioned asset successfully (`RUN_SUCCESS`). See **Validation gaps** below for the three gotchas we hit and how we resolved them.

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

## Run

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

## Validation gaps (what we learned end-to-end, 2026-05-13)

We validated against a real GCP project. The infra side works; one auth nuance to watch.

### 1. Pub/Sub service account needs TWO bucket roles, not one

The `gcloud pubsub subscriptions create --cloud-storage-bucket ...` command fails with a helpful-but-incomplete error if you only grant `roles/storage.objectAdmin`. The actual minimum is:

```bash
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"
gsutil iam ch "serviceAccount:${PUBSUB_SA}:legacyBucketReader" "gs://${BUCKET}"
gsutil iam ch "serviceAccount:${PUBSUB_SA}:objectCreator"     "gs://${BUCKET}"
```

`legacyBucketReader` + `objectCreator` are what the subscription creation actually checks for. Grant them BEFORE `pubsub subscriptions create`. The Console wizard nudges you toward this; the `gcloud` CLI surfaces the precise role names only in the error message after the create fails.

### 2. Parquet output requires a topic schema; JSON-Lines doesn't

The CLI flag `--cloud-storage-output-format parquet` fails until you've attached a Pub/Sub Schema (AVRO or Protocol Buffers) to the topic via `gcloud pubsub schemas create` + `topics update --schema`. The default text format produces JSON-Lines and works without any schema configuration.

We validated end-to-end with the default text format:
- `--cloud-storage-file-suffix ".jsonl"` (just a filename hint, doesn't change the content)
- `file_ingestion`'s `format: jsonl` in the asset config

If you need Parquet's schema enforcement, add a topic schema. Otherwise stick with JSON-Lines; it's friendlier for evolution.

### 3. ADC ≠ `gcloud auth list` — this WILL bite local-dev

`gcsfs` (the fsspec driver pandas uses to read `gs://` URIs) reads Application Default Credentials, which is **a different identity store than what `gcloud auth list` shows as ACTIVE**. If the two diverge — for example, you're logged in as a service account in gcloud but `gcloud auth application-default login` cached an end-user token earlier — Python reads fail with `Forbidden: ... /storage/v1/b/<bucket>/o/...` despite `gsutil`/`gcloud` working fine against the same bucket.

This is what bit us first when validating. The fix that worked end-to-end:

```bash
# Issue a key for the SA that has bucket access
gcloud iam service-accounts keys create /tmp/.dagster_gcp_sa_key.json \
  --iam-account="<sa-email>"

# Point ADC at the key, then re-run dg dev in the same shell
export GOOGLE_APPLICATION_CREDENTIALS=/tmp/.dagster_gcp_sa_key.json
uv run dg dev
```

`gcsfs` reads `GOOGLE_APPLICATION_CREDENTIALS` automatically — no code change in the asset. With that env var set, `file_ingestion` successfully read 11 rows × 4 cols from `gs://<bucket>/<partition>.jsonl` and the partitioned materialization returned `RUN_SUCCESS`.

Two alternative fixes that also work:

```bash
# Option A: re-cache ADC as your end-user identity
gcloud auth application-default login
gcloud auth application-default set-quota-project <PROJECT_ID>
```

```bash
# Option B: in GKE / GCE production — Workload Identity
#   Both gcloud and gcsfs pick up the workload identity automatically.
#   This whole class of issue disappears in prod; it's a local-dev wart.
```

Symptom signature: `gsutil ls gs://<bucket>/...` works fine, but the Dagster asset run logs `OSError: Forbidden: <storage URL>`. That divergence = ADC mismatch.

### Working configuration

```bash
# Bucket + topic + subscription (text/JSON output, no schema needed)
gsutil mb -p "$PROJECT" -l us-central1 "gs://$BUCKET"
gcloud pubsub topics create "$TOPIC" --project "$PROJECT"
PUBSUB_SA="service-$(gcloud projects describe $PROJECT --format='value(projectNumber)')@gcp-sa-pubsub.iam.gserviceaccount.com"
gsutil iam ch "serviceAccount:${PUBSUB_SA}:legacyBucketReader" "gs://$BUCKET"
gsutil iam ch "serviceAccount:${PUBSUB_SA}:objectCreator"     "gs://$BUCKET"
gcloud pubsub subscriptions create "$SUB" --project "$PROJECT" --topic "$TOPIC" \
  --cloud-storage-bucket "$BUCKET" \
  --cloud-storage-file-prefix "events/events-" \
  --cloud-storage-file-suffix ".jsonl" \
  --cloud-storage-max-bytes 1048576 \
  --cloud-storage-max-duration 60s
```

```yaml
# defs/raw_events/defs.yaml
type: <pkg>.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: raw_events
  format: jsonl
  file_path: "gs://your-bucket/events/<filename>.jsonl"
```

Validated result: published 150 messages via `gcloud pubsub topics publish`; subscription wrote multiple JSONL files within 60s; with `GOOGLE_APPLICATION_CREDENTIALS` pointed at a service-account key, the `dg dev` process materialized `raw_events` partition from a `gs://` URI — **11 rows × 4 cols read, `RUN_SUCCESS`**.

## Trade-offs & gotchas

- **Subscription latency = your minimum batch size.** With `--cloud-storage-max-duration 60s`, files land every minute minimum. Add ~30s–1min for Dagster's sensor + asset.
- **Parquet format requires a fixed schema.** Pub/Sub Cloud Storage Subscription writing Parquet requires that you have a schema attached to the topic (Pub/Sub Schemas, AVRO or Protocol Buffers). Without one, fall back to Avro or text format, but downstream `file_ingestion` will need `format: json` or similar.
- **File-fanout costs.** Lots of small files = lots of GCS list operations. Tune `--cloud-storage-max-duration` / `--cloud-storage-max-bytes` upward if you're seeing this.
- **Schema evolution.** Pub/Sub Schema Registry handles compatibility; downstream `summarize`'s `group_by` may break if it references a removed column.
- **Re-runnable.** Every partition in Dagster's UI can be rematerialized. The Parquet is in GCS forever (or until you lifecycle-delete it).

## See also

- [`eh_capture_pipeline.md`](eh_capture_pipeline.md) — Azure mirror of this demo.
- [`s3_pipeline.md`](s3_pipeline.md) — same dynamic-partition pattern using Minio S3 + manual file uploads (no real streaming source).
