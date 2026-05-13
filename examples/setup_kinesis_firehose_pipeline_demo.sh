#!/usr/bin/env bash
# Kinesis → Firehose → S3 → Dagster file processing → Athena
#
# AWS mirror of the Azure (EH Capture) and GCP (Pub/Sub-to-GCS) blueprints.
# Same production-shape pattern: external streamer lands events in durable
# object storage, Dagster picks up files event-driven via dynamic-partition
# sensor. Each new S3 object = one Dagster partition = re-runnable.
#
#   producers
#       │
#       ▼
#   [Kinesis Data Stream  -or-  direct PUT to Firehose]
#       │
#       ▼ Kinesis Data Firehose (built-in AWS service)
#         buffers + writes Parquet to S3 every N min or M MB,
#         optional Glue schema, optional record transformation
#         via Lambda.
#       │
#       ▼ [S3 bucket: s3://bucket/events/year=2026/month=05/day=13/hour=14/...parquet]
#       │
#       ▼ s3_monitor (partition_mode: dynamic_partition)
#         registers each new Parquet file as a Dagster dynamic partition
#         and yields RunRequest(partition_key=<file_key>).
#       │
#       ▼ file_ingestion (partitioned, from_run_config: uri_template "s3://...")
#         reads the partition's Parquet into a pandas DataFrame.
#       │
#       ▼ summarize (per-partition aggregation)
#       │
#       ▼ dataframe_to_parquet
#         writes a curated Parquet to a different S3 path.
#       │
#       ▼ Athena (or Glue catalog → Athena/Redshift Spectrum/EMR/etc.)
#         queries the curated path directly via standard SQL.
#
# Validation status:
#   - dg check: passes against the YAML
#   - End-to-end materialization: requires real AWS resources (Kinesis Data
#     Stream or direct-to-Firehose source, a Firehose with S3 destination
#     in Parquet mode, an S3 bucket, optionally Glue Database + Athena
#     workgroup). Blueprint.

set -euo pipefail

PROJECT_DIR="${1:-kinesis-firehose-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas pyarrow fsspec s3fs boto3
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add s3_monitor            --auto-install
$CLI add file_ingestion        --auto-install
$CLI add summarize             --auto-install
$CLI add dataframe_to_parquet  --auto-install

rm -rf "src/$PKG/defs/s3_monitor" \
       "src/$PKG/defs/file_ingestion" \
       "src/$PKG/defs/summarize" \
       "src/$PKG/defs/dataframe_to_parquet"

mkdir -p "src/$PKG/defs/firehose_sensor" \
         "src/$PKG/defs/raw_events" \
         "src/$PKG/defs/events_by_type" \
         "src/$PKG/defs/curated"

echo ">>> Writing defs.yaml"

cat > "src/$PKG/defs/firehose_sensor/defs.yaml" <<EOF
type: $PKG.components.s3_monitor.component.S3MonitorSensorComponent
attributes:
  sensor_name: firehose_sensor
  # CUSTOMIZE: your S3 bucket where Firehose lands Parquet
  bucket_name: "{{ env('FIREHOSE_S3_BUCKET') }}"
  prefix: "events/"
  key_pattern: ".*\\\\.parquet\$"
  job_name: __ASSET_JOB
  minimum_interval_seconds: 30
  region_name: "{{ env('AWS_REGION') }}"
  partition_mode: dynamic_partition
  dynamic_partitions_name: "firehose_parquet_files"
  partition_key_template: "{key}"   # bare S3 key (object path inside bucket)
  default_status: stopped
EOF

cat > "src/$PKG/defs/raw_events/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: raw_events
  format: parquet
  partition_type: dynamic
  dynamic_partition_name: "firehose_parquet_files"
  from_run_config:
    uri_template: "s3://{{ env('FIREHOSE_S3_BUCKET') }}/{partition_key}"
  description: One DataFrame per Firehose Parquet file (per partition)
  group_name: raw
EOF

cat > "src/$PKG/defs/events_by_type/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: events_by_type
  upstream_asset_key: raw_events
  partition_type: dynamic
  dynamic_partition_name: "firehose_parquet_files"
  # CUSTOMIZE: replace with your event schema's column names
  group_by: [event_type]
  aggregations:
    event_count:    {col: event_id, agg: count}
    distinct_users: {col: user_id,  agg: nunique}
  group_name: silver
EOF

cat > "src/$PKG/defs/curated/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: events_by_type_parquet
  upstream_asset_key: events_by_type
  partition_type: dynamic
  dynamic_partition_name: "firehose_parquet_files"
  # CUSTOMIZE: target your curated S3 prefix
  file_path: "s3://{{ env('FIREHOSE_S3_BUCKET') }}/curated/events_by_type/{partition_key}.parquet"
  compression: snappy
  group_name: gold
EOF

cat > CUSTOMIZE.md <<'CUSTOMIZE_EOF'
# Customize before running

## Step 1 — AWS prerequisites (you provide)

1. **S3 bucket** for both raw (`events/`) and curated (`curated/`) data.
2. **Kinesis Data Firehose delivery stream** with S3 destination, **Parquet** record format, optional Lambda transformation. Configure:
   - Source: either a Kinesis Data Stream or "Direct PUT" (sends from your producers using `firehose:PutRecord` / `PutRecordBatch`).
   - Destination: S3 bucket above. Prefix `events/year=!{timestamp:YYYY}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/` (Firehose's dynamic partitioning expressions).
   - Buffer hints: 5 MB / 60 seconds is a typical starting point.
   - Format conversion: enable, choose Parquet, select your Glue schema (Firehose requires a Glue schema for Parquet output).
3. **Glue database + table** describing your event schema. Firehose's Parquet writer reads this to know columns/types.
4. **Producers** sending to the Kinesis Data Stream (or directly to Firehose).
5. **Athena workgroup** (optional, for the analytics surface).

## Step 2 — Auth

The defs use the default boto3 credential chain (env vars, instance role, SSO). Set:

```bash
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
# Or use AWS_PROFILE / instance role / IRSA — anything boto3 picks up.

# Plus the bucket the defs reference:
export FIREHOSE_S3_BUCKET=your-firehose-bucket-name
```

## Step 3 — Customize defs.yaml (4 files)

Each `# CUSTOMIZE:` comment marks a line to change:

- `src/<pkg>/defs/firehose_sensor/defs.yaml` — `prefix:` should match where Firehose lands its files.
- `src/<pkg>/defs/raw_events/defs.yaml` — `uri_template`'s bucket name.
- `src/<pkg>/defs/events_by_type/defs.yaml` — `group_by` and `aggregations` should match your event schema columns.
- `src/<pkg>/defs/curated/defs.yaml` — set the curated target path.

## Step 4 — Validate + run

```bash
uv run dg check defs
uv run dg dev
```

In the UI, enable `firehose_sensor`. It picks up existing Parquet files in the bucket within ~30s, registers each as a partition, and runs the chain. Each new file landed by Firehose triggers another partition + run.

## Step 5 — Query via Athena (independent of Dagster)

```sql
-- Create an external table over the curated S3 path (if not already there)
CREATE EXTERNAL TABLE IF NOT EXISTS events_by_type (
  event_type     STRING,
  event_count    BIGINT,
  distinct_users BIGINT
)
STORED AS PARQUET
LOCATION 's3://your-firehose-bucket/curated/events_by_type/'
TBLPROPERTIES ('parquet.compress'='SNAPPY');

SELECT event_type, SUM(event_count) AS total_events
FROM events_by_type
GROUP BY event_type
ORDER BY total_events DESC;
```

Or register the table in the Glue catalog so Athena, Redshift Spectrum, EMR, and SageMaker can all read it.

## Why this architecture (not direct-Kinesis-consumption in a sensor)

Kinesis Data Streams have finite retention (default 24 hours, max 365 days). Sensor-side consumption of records would create Dagster partitions that aren't re-runnable after retention expires. Firehose solves this by landing every record in S3 as durable Parquet — partitions over S3 files ARE re-runnable forever because the data is in object storage.

Same shape as the Azure (EH Capture) and GCP (Pub/Sub-to-GCS Subscription) blueprints — see `eh_capture_pipeline.md` and `pubsub_gcs_pipeline.md`.
CUSTOMIZE_EOF

cat <<MSG

============================================================================
>>> Setup complete — BLUEPRINT, requires real AWS resources.
============================================================================

This scaffolds the Dagster project + defs.yaml. It does NOT stand up AWS
Kinesis Data Streams, Firehose delivery streams, Glue schemas, or S3
buckets — that's what $PROJECT_DIR/CUSTOMIZE.md walks through.

Architecture:
  Producers
      │
      ▼ (built-in AWS)
  Kinesis Data Firehose writes Parquet files to S3
      │
      ▼
  s3_monitor (dynamic_partition mode) — sensor
      │
      ▼ per-file RunRequest
  raw_events (file_ingestion partitioned, reads Parquet via s3fs)
      ▼
  events_by_type (summarize)
      ▼
  events_by_type_parquet (dataframe_to_parquet, writes to curated S3 prefix)
      ▼ (independent of Dagster)
  Athena / Glue catalog — standard SQL over the curated path

Next steps:
  cd $PROJECT_DIR
  cat CUSTOMIZE.md         # prereq punch list
  # set up AWS resources per CUSTOMIZE.md
  export AWS_REGION=...
  export FIREHOSE_S3_BUCKET=...
  uv run dg check defs
  uv run dg dev
MSG
