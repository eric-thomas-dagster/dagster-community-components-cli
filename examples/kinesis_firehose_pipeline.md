# Kinesis → Firehose → S3 → Dagster → Athena — blueprint
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

AWS mirror of the [Event Hubs Capture](eh_capture_pipeline.md) and [Pub/Sub → GCS](pubsub_gcs_pipeline.md) demos. Same production-shape pattern: **Dagster doesn't process the queue directly** — Kinesis Data Firehose (a built-in AWS service) lands every record in S3 as durable Parquet, and Dagster picks up files event-driven via a dynamic-partition `s3_monitor`. Each new file = one Dagster dynamic partition = fully re-runnable.

> **Validation status:** the Dagster wiring is buildable and `dg check` passes. End-to-end requires a real AWS account with Kinesis + Firehose + Glue + S3 — blueprint, not validated in this repo (AWS creds not available in the build environment as of this writing).

## Why this architecture

Same rationale as the Azure / GCP mirrors:

| Problem | Why direct-Kinesis-consumption fails |
|---|---|
| **Partitions must be re-runnable.** | Kinesis Data Streams retain records for 1–365 days (default 24h). Replay of a partition past retention = impossible. |
| **Sensor cursor is small.** | Accumulating record payloads across ticks doesn't fit. |
| **Operational concerns.** | Batching, format conversion (Parquet), schema enforcement (Glue), partition expressions — all built into Firehose. |

Firehose is the right boundary between the streaming layer and Dagster.

## Architecture

```
   ┌──────────────────────────────────────────┐
   │ Producers (any language / runtime)        │
   │   ↓                                       │
   │ Kinesis Data Stream  -OR-  Direct PUT to Firehose
   └─────────────────┬─────────────────────────┘
                     │
                     ▼  Kinesis Data Firehose (built-in AWS service)
                       buffers, converts to Parquet (Glue schema),
                       optionally transforms via Lambda
   ┌──────────────────────────────────────────┐
   │ S3: bucket/events/year=2026/month=05/...  │
   │   <window>.parquet                         │
   └─────────────────┬─────────────────────────┘
                     │
   ┌─────────────────▼─────────────────────────┐
   │ Dagster                                    │
   │                                            │
   │ s3_monitor (partition_mode:                │
   │   dynamic_partition) — registers each new  │
   │   Parquet file as a partition.             │
   │                ↓                           │
   │ raw_events (file_ingestion partitioned)    │
   │   reads s3://bucket/<partition>.parquet    │
   │                ↓                           │
   │ events_by_type (summarize partitioned)     │
   │   group_by + aggs per file.                │
   │                ↓                           │
   │ events_by_type_parquet                     │
   │   writes curated Parquet to a different    │
   │   S3 prefix, one file per partition.       │
   └─────────────────┬─────────────────────────┘
                     │
                     ▼ (independent of Dagster — query directly)
   ┌──────────────────────────────────────────┐
   │ Athena / Glue catalog                     │
   │ Standard SQL over the curated S3 prefix.  │
   │ Also queryable by Redshift Spectrum, EMR, │
   │ SageMaker, etc.                            │
   └──────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `s3_monitor` | community | Polls the S3 bucket, registers each new Parquet file as a dynamic partition. |
| `file_ingestion` | community | Partitioned asset reads each Parquet as a pandas DataFrame. |
| `summarize` | community | Per-partition group-by + aggregations. |
| `dataframe_to_parquet` | community | Writes the curated Parquet back to S3. |

Plus the AWS-native pieces you provide: Kinesis Data Stream (or direct PUT), Firehose delivery stream, S3 bucket, Glue database+table, and (optionally) Athena workgroup.

## AWS setup (prerequisites you provide)

### 1. S3 bucket + Glue database

```bash
BUCKET=firehose-events-$(openssl rand -hex 4)
REGION=us-east-1
aws s3 mb "s3://$BUCKET" --region "$REGION"
aws glue create-database --database-input "Name=events_demo" --region "$REGION"
```

### 2. Glue table (schema for Firehose's Parquet writer)

Firehose needs a Glue table describing your event schema so it can write Parquet. A minimal example:

```bash
aws glue create-table --database-name events_demo --region "$REGION" --table-input '{
  "Name": "raw_events",
  "StorageDescriptor": {
    "Columns": [
      {"Name": "event_id", "Type": "string"},
      {"Name": "event_type", "Type": "string"},
      {"Name": "user_id", "Type": "string"},
      {"Name": "timestamp", "Type": "timestamp"},
      {"Name": "payload", "Type": "string"}
    ],
    "Location": "s3://'"$BUCKET"'/events/",
    "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
    "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    "SerdeInfo": {"SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"}
  },
  "TableType": "EXTERNAL_TABLE"
}'
```

### 3. Firehose delivery stream

In the Firehose console (or via `aws firehose create-delivery-stream`):
- Source: `Direct PUT` (your producers `firehose:PutRecord`) or a Kinesis Data Stream.
- Destination: S3 with the bucket above.
- S3 prefix: `events/year=!{timestamp:YYYY}/month=!{timestamp:MM}/day=!{timestamp:dd}/hour=!{timestamp:HH}/`
- Buffer hints: 5 MB / 60 seconds (tune for your throughput).
- **Format conversion enabled**: Parquet, Glue database `events_demo`, table `raw_events`.

### 4. IAM

The Firehose role needs `s3:PutObject` to the bucket + `glue:GetTable` on the schema table. The Dagster client needs `s3:ListBucket` + `s3:GetObject` for the sensor's listing and the asset's reads.

### 5. Athena workgroup (optional)

```bash
aws athena create-work-group --name dagster-firehose-demo --region "$REGION"
```

Set the result location to another S3 prefix when you create it.

## Run

```bash
./setup_kinesis_firehose_pipeline_demo.sh
cd kinesis-firehose-pipeline-demo

export AWS_REGION="$REGION"
export FIREHOSE_S3_BUCKET="$BUCKET"
# Plus normal boto3 credentials — env vars, instance role, profile, SSO; anything boto3 picks up.

uv run dg check defs
uv run dg dev
```

In the UI, enable `firehose_sensor`. Within ~30s the sensor picks up existing Parquet files in `s3://$BUCKET/events/`, registers each as a dynamic partition, and runs the chain. Each new file from this point on triggers another partition + run.

## Querying via Athena (independent of Dagster)

```sql
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

Or register in the Glue catalog so Athena, Redshift Spectrum, EMR, and SageMaker all read the same table.

## Trade-offs & gotchas

- **Firehose buffer hints determine latency.** Default 5 MB / 60s gives roughly per-minute landings. Dagster adds 30s–1min. Net 1.5–2 min from PUT to curated Parquet.
- **Glue schema is the bottleneck for schema evolution.** Firehose's Parquet writer requires every record to match the Glue table schema. Adding a column means: update the Glue table, then producers, then optionally Lambda transform for backfill. Out-of-schema records get dropped or sent to the error S3 prefix.
- **Sensor key length on partitioned prefixes.** With deeply partitioned S3 layouts (year=/month=/day=/hour=/...), the partition_key on Dagster's side gets long. Acceptable but worth noting.
- **Cost.** Firehose: $0.029 per GB ingested (us-east-1, on-demand) + S3 PUT/GET costs. Tiny at demo scale.
- **Re-runnable.** Every partition in Dagster's UI can be rematerialized — the Parquet is in S3 forever (lifecycle-delete on your own schedule).

## See also

- [`eh_capture_pipeline.md`](eh_capture_pipeline.md) — Azure mirror of this demo.
- [`pubsub_gcs_pipeline.md`](pubsub_gcs_pipeline.md) — GCP mirror.
- [`s3_pipeline.md`](s3_pipeline.md) — same dynamic-partition pattern using local Minio S3 + manual file uploads (no real streaming source — runs offline in CI).
