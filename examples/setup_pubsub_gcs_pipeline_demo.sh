#!/usr/bin/env bash
# Pub/Sub → Cloud Storage Subscription → Dagster file processing → BigQuery
#
# Stream → durable-storage → batch — the production pattern. Mirror of the
# Azure EH Capture demo for GCP.
#
#   producers
#       │
#       ▼
#   [Google Pub/Sub topic]
#       │
#       ▼ Cloud Storage Subscription (built-in GCP service)
#         writes Avro/Parquet files to GCS every N min or M bytes —
#         time-windowed, durable, replayable.
#       │
#       ▼ [GCS bucket: gs://bucket/topic-name/<timestamp>.parquet]
#       │
#       ▼ gcs_monitor (partition_mode: dynamic_partition)
#         registers each new file as a Dagster dynamic partition
#         and yields RunRequest(partition_key=<file_path>).
#       │
#       ▼ file_ingestion (partitioned, from_run_config: uri_template "gs://...")
#         reads the partition's Parquet into a pandas DataFrame.
#       │
#       ▼ summarize (per-partition aggregation)
#       │
#       ▼ dataframe_to_parquet
#         writes a curated Parquet to a different GCS path.
#       │
#       ▼ BigQuery external table via auto-detect schema
#         (documented; not Dagster-orchestrated) — the curated path is
#         queryable directly with standard SQL.
#
# Validation status:
#   - dg check: passes against the YAML
#   - End-to-end materialization: requires real GCP Pub/Sub topic with a
#     Cloud Storage Subscription configured + a GCS bucket. Blueprint.

set -euo pipefail

PROJECT_DIR="${1:-pubsub-gcs-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas pyarrow fsspec gcsfs google-cloud-storage google-cloud-bigquery
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add gcs_monitor           --auto-install
$CLI add file_ingestion        --auto-install
$CLI add summarize             --auto-install
$CLI add dataframe_to_parquet  --auto-install

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/gcs_monitor" \
       "src/$PKG/defs/file_ingestion" \
       "src/$PKG/defs/summarize" \
       "src/$PKG/defs/dataframe_to_parquet"

mkdir -p "src/$PKG/defs/pubsub_gcs_sensor" \
         "src/$PKG/defs/raw_events" \
         "src/$PKG/defs/events_by_type" \
         "src/$PKG/defs/curated"

echo ">>> Writing defs.yaml"

# --- 1. gcs_monitor: watches the Pub/Sub Subscription output path, dynamic-partition mode.
cat > "src/$PKG/defs/pubsub_gcs_sensor/defs.yaml" <<EOF
type: $PKG.components.gcs_monitor.component.GCSMonitorSensorComponent
attributes:
  sensor_name: pubsub_gcs_sensor
  # CUSTOMIZE: your GCS bucket where the Pub/Sub subscription writes files
  bucket_name: "{{ env('GCS_BUCKET') }}"
  prefix: "events/"
  blob_pattern: ".*\\\\.parquet\$"
  project: "{{ env('GCP_PROJECT') }}"
  job_name: __ASSET_JOB
  minimum_interval_seconds: 30
  partition_mode: dynamic_partition
  dynamic_partitions_name: "pubsub_parquet_files"
  partition_key_template: "{name}"      # bare blob name (object key inside bucket)
  default_status: stopped
EOF

# --- 2. raw_events: file_ingestion in dynamic-partition mode, reads each
# new Parquet file from GCS.
cat > "src/$PKG/defs/raw_events/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: raw_events
  format: parquet
  partition_type: dynamic
  dynamic_partition_name: "pubsub_parquet_files"
  from_run_config:
    # CUSTOMIZE: match your bucket name
    uri_template: "gs://{{ env('GCS_BUCKET') }}/{partition_key}"
  description: One DataFrame per Pub/Sub Subscription Parquet file (per partition)
  group_name: raw
EOF

# --- 3. events_by_type
cat > "src/$PKG/defs/events_by_type/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: events_by_type
  upstream_asset_key: raw_events
  partition_type: dynamic
  dynamic_partition_name: "pubsub_parquet_files"
  # CUSTOMIZE: replace with your event schema's column names
  group_by: [event_type]
  aggregations:
    event_count:    {col: event_id, agg: count}
    distinct_users: {col: user_id,  agg: nunique}
  group_name: silver
EOF

# --- 4. curated parquet output — written to GCS, BigQuery reads via
# external table.
cat > "src/$PKG/defs/curated/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: events_by_type_parquet
  upstream_asset_key: events_by_type
  partition_type: dynamic
  dynamic_partition_name: "pubsub_parquet_files"
  # CUSTOMIZE: target your own curated GCS path
  file_path: "gs://{{ env('GCS_BUCKET') }}/curated/events_by_type/{partition_key}.parquet"
  compression: snappy
  group_name: gold
EOF

# Write a CUSTOMIZE.md punch list
cat > CUSTOMIZE.md <<'CUSTOMIZE_EOF'
# Customize before running

## Step 1 — GCP prerequisites (you provide)

1. **GCS bucket** for both raw + curated. The demo uses one bucket with two prefixes (`events/` and `curated/`); split into two buckets if you want stricter ACLs.
2. **Pub/Sub topic** receiving events from your producers.
3. **Cloud Storage subscription on that topic** writing Parquet to `gs://<bucket>/events/<window>.parquet`. Configure in the GCP Console: Pub/Sub → Subscriptions → Create → Delivery type "Write to Cloud Storage" → format Parquet → set max bytes / max duration per file. This is the GCP-native streamer.
4. **BigQuery dataset** for the eventual external table (Step 5).

## Step 2 — Auth (Workload Identity, service account, or `gcloud auth`)

The defs use Application Default Credentials (ADC). Set up via one of:

```bash
# Local dev (laptop)
gcloud auth application-default login

# GCE / GKE: use Workload Identity; no env vars needed.

# Service account JSON
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json

# Plus the project + bucket the defs reference:
export GCP_PROJECT=your-gcp-project
export GCS_BUCKET=your-bucket-name
```

## Step 3 — Customize defs.yaml (4 files)

Each `# CUSTOMIZE:` comment marks a line to change:

- `src/<pkg>/defs/pubsub_gcs_sensor/defs.yaml` — adjust `prefix:` to match the path your Cloud Storage subscription writes to.
- `src/<pkg>/defs/raw_events/defs.yaml` — `uri_template` should resolve to your bucket.
- `src/<pkg>/defs/events_by_type/defs.yaml` — `group_by` + `aggregations` should match your event schema (demo assumes `event_type`, `event_id`, `user_id`).
- `src/<pkg>/defs/curated/defs.yaml` — set the curated target path.

## Step 4 — Validate + run

```bash
uv run dg check defs
uv run dg dev
```

In Dagster's UI:
- `pubsub_gcs_sensor` (set `running` once your Cloud Storage Subscription is producing files) registers a new partition for each Parquet file as it lands.
- Each partition kicks off `raw_events → events_by_type → events_by_type_parquet`.
- Re-runnable via the UI because the data lives in GCS, not the topic.

## Step 5 — Query via BigQuery (independent of Dagster)

```sql
-- Create an external table over the curated GCS path:
CREATE OR REPLACE EXTERNAL TABLE `your_project.your_dataset.events_by_type`
OPTIONS (
  format = 'PARQUET',
  uris = ['gs://your-bucket/curated/events_by_type/*.parquet']
);

-- Then query like any BigQuery table:
SELECT event_type, SUM(event_count) AS total_events
FROM `your_project.your_dataset.events_by_type`
GROUP BY event_type
ORDER BY total_events DESC;
```

## Why this architecture (not microbatch directly on Pub/Sub)

Pub/Sub messages are gone once acknowledged — no replay. If you tried to track each batch of messages as a Dagster partition with the topic as the source of truth, "rematerialize partition X" would fail (the messages are gone). The Cloud Storage Subscription solves this by writing every message to GCS as durable Parquet — partitions over GCS files ARE re-runnable forever because the data is in object storage.
CUSTOMIZE_EOF

cat <<MSG

============================================================================
>>> Setup complete — BLUEPRINT, requires real GCP resources.
============================================================================

This scaffolds the Dagster project + defs.yaml. It does NOT stand up GCP
Pub/Sub topics, Subscriptions, or buckets — that's what
$PROJECT_DIR/CUSTOMIZE.md walks through.

Architecture:
  Pub/Sub producers
      │
      ▼ (built-in GCP)
  Cloud Storage Subscription writes Parquet files to GCS
      │
      ▼
  gcs_monitor (dynamic_partition mode) — sensor
      │
      ▼ per-file RunRequest
  raw_events (file_ingestion partitioned, reads Parquet via fsspec/gcsfs)
      │
      ▼
  events_by_type (summarize)
      │
      ▼
  events_by_type_parquet (dataframe_to_parquet, writes to curated path)
      │
      ▼ (independent of Dagster)
  BigQuery external table — standard SQL over the curated GCS path

Next steps:
  cd $PROJECT_DIR
  cat CUSTOMIZE.md         # prereq punch list
  # set up GCP resources per CUSTOMIZE.md
  export GCP_PROJECT=...
  export GCS_BUCKET=...
  uv run dg check defs
  uv run dg dev
MSG
