#!/usr/bin/env bash
# Event Hubs Capture → ADLS → Dagster file processing → Synapse Serverless
#
# Streaming pipeline that doesn't pretend to be streaming. Instead:
#
#   producers
#       │
#       ▼
#   [Azure Event Hub]
#       │
#       ▼ EH Capture (built-in Azure service)
#         writes Parquet files to ADLS every N minutes or M bytes —
#         time-windowed, durable, replayable.
#       │
#       ▼ [ADLS container: avro/2026/05/13/14/00/00.parquet]
#       │
#       ▼ adls_monitor (partition_mode: dynamic_partition)
#         registers each new Parquet file as a Dagster dynamic partition
#         and yields RunRequest(partition_key=<file_path>).
#       │
#       ▼ file_ingestion (partitioned, from_run_config: uri_template "abfss://...")
#         reads the partition's Parquet into a pandas DataFrame.
#       │
#       ▼ summarize (per-partition aggregation)
#       │
#       ▼ dataframe_to_parquet
#         writes a curated Parquet to a different ADLS path (one parquet per partition).
#       │
#       ▼ Synapse Serverless SQL via OPENROWSET (documented; not Dagster-orchestrated)
#         the curated path is queryable directly with native Azure SQL.
#
# Why this is the right architecture:
#   - EH Capture handles the queue→storage step (durable, retained, replayable)
#     so Dagster never touches the queue directly.
#   - Dynamic partitions over ADLS files are FULLY re-runnable (the data is
#     in ADLS forever, not gated by EH retention).
#   - The whole pipeline is event-driven via the adls_monitor sensor.
#
# Validation status:
#   - dg check: passes against the YAML
#   - End-to-end materialization: requires real Azure Event Hubs namespace
#     with Capture enabled + a Storage Account. Blueprint.

set -euo pipefail

PROJECT_DIR="${1:-eh-capture-pipeline-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas pyarrow fsspec adlfs azure-identity
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add adls_monitor          --auto-install
$CLI add file_ingestion        --auto-install
$CLI add summarize             --auto-install
$CLI add dataframe_to_parquet  --auto-install

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/adls_monitor" \
       "src/$PKG/defs/file_ingestion" \
       "src/$PKG/defs/summarize" \
       "src/$PKG/defs/dataframe_to_parquet"

mkdir -p "src/$PKG/defs/eh_capture_sensor" \
         "src/$PKG/defs/raw_events" \
         "src/$PKG/defs/events_by_type" \
         "src/$PKG/defs/curated"

echo ">>> Writing defs.yaml"

# --- 1. adls_monitor: watches the EH Capture output path, dynamic-partition mode.
# Each new Parquet file becomes a tracked partition.
cat > "src/$PKG/defs/eh_capture_sensor/defs.yaml" <<EOF
type: $PKG.components.adls_monitor.component.ADLSMonitorSensorComponent
attributes:
  sensor_name: eh_capture_sensor
  # CUSTOMIZE: your storage account + container + EH Capture path layout
  storage_account_name: "{{ env('AZURE_STORAGE_ACCOUNT') }}"
  container_name: eh-capture
  directory_path: "namespace/eventhub"
  file_pattern: ".*\\\\.avro\$"
  recursive: true
  job_name: __ASSET_JOB
  minimum_interval_seconds: 30
  partition_mode: dynamic_partition
  dynamic_partitions_name: "eh_parquet_files"
  partition_key_template: "{file_path}"    # full ADLS file path inside container
  default_status: stopped
EOF

# --- 2. raw_events: file_ingestion in dynamic-partition mode, reads each
# new Parquet file from ADLS.
cat > "src/$PKG/defs/raw_events/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: raw_events
  # EH Capture writes Avro by default on Standard tier (Parquet is Premium/preview).
  format: avro
  partition_type: dynamic
  dynamic_partition_name: "eh_parquet_files"
  from_run_config:
    # CUSTOMIZE: match your storage account + container; {partition_key}
    # comes from the sensor's partition_key_template above.
    uri_template: "abfss://eh-capture@{{ env('AZURE_STORAGE_ACCOUNT') }}.dfs.core.windows.net/{partition_key}"
  description: One DataFrame per EH Capture Avro file (per partition)
  group_name: raw
EOF

# --- 3. events_by_type: aggregate per file.
cat > "src/$PKG/defs/events_by_type/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: events_by_type
  upstream_asset_key: raw_events
  partition_type: dynamic
  dynamic_partition_name: "eh_parquet_files"
  # CUSTOMIZE: replace with the actual column names from your event schema
  group_by: [event_type]
  aggregations:
    event_count:    {col: event_id, agg: count}
    distinct_users: {col: user_id,  agg: nunique}
  group_name: silver
EOF

# --- 4. curated parquet output — one file per partition, written to a
# different ADLS path that Synapse Serverless can query via OPENROWSET.
cat > "src/$PKG/defs/curated/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: events_by_type_parquet
  upstream_asset_key: events_by_type
  partition_type: dynamic
  dynamic_partition_name: "eh_parquet_files"
  # CUSTOMIZE: target your own curated container. {partition_key} keeps
  # the file path namespace stable across runs.
  file_path: "abfss://curated@{{ env('AZURE_STORAGE_ACCOUNT') }}.dfs.core.windows.net/events_by_type/{partition_key}.parquet"
  compression: snappy
  group_name: gold
EOF

# Write a CUSTOMIZE.md punch list
cat > CUSTOMIZE.md <<'CUSTOMIZE_EOF'
# Customize before running

## Step 1 — Azure prerequisites (you provide)

1. **Storage account** with two containers:
   - `eh-capture` — where Event Hubs Capture writes Parquet
   - `curated` — where Dagster writes the per-partition processed Parquet
2. **Event Hubs namespace + Event Hub** with **Capture enabled** writing Parquet to `eh-capture/namespace/eventhub/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}.parquet`. Configure in the Azure portal: Event Hub → Capture → Avro vs Parquet (pick Parquet) → choose the storage account + container + path pattern.
3. **Producers** sending events to the Event Hub.

## Step 2 — Auth (Managed Identity or service principal)

The defs use `DefaultAzureCredential`, which picks up env vars / managed identity / `az login`. Set:

```bash
export AZURE_STORAGE_ACCOUNT=your_storage_account_name
# Optional, if not using managed identity / az login:
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
```

## Step 3 — Customize defs.yaml (3 files)

Each `# CUSTOMIZE:` comment marks a line to change:

- `src/<pkg>/defs/eh_capture_sensor/defs.yaml` — `directory_path` should match where EH Capture is actually writing (typically `<namespace>/<eventhub>`).
- `src/<pkg>/defs/raw_events/defs.yaml` — `uri_template`'s container name (`eh-capture` here) should match your Capture container.
- `src/<pkg>/defs/events_by_type/defs.yaml` — `group_by` and `aggregations` should match your event schema columns (the demo assumes `event_type`, `event_id`, `user_id`).
- `src/<pkg>/defs/curated/defs.yaml` — `file_path` should target your curated container.

## Step 4 — Validate + run

```bash
uv run dg check defs
uv run dg dev
```

In Dagster's UI:
- `eh_capture_sensor` (set to `running` once your Capture is producing files) registers a new partition for each Parquet file as it lands.
- Each partition kicks off the chain: `raw_events → events_by_type → events_by_type_parquet`.
- Every detected file becomes a tracked dynamic partition you can re-run from the UI (durable because the data is in ADLS, not the Event Hub).

## Step 5 — Query via Synapse Serverless (independent of Dagster)

```sql
-- In Synapse Serverless SQL pool, query the curated Parquet directly:
SELECT event_type, SUM(event_count) AS total_events
FROM OPENROWSET(
    BULK 'https://<storage_account>.dfs.core.windows.net/curated/events_by_type/**',
    FORMAT = 'PARQUET'
) AS r
GROUP BY event_type
ORDER BY total_events DESC;
```

You can build a Synapse external table on the same path for a stable BI surface.

## Why this architecture (not microbatch directly on the queue)

Event Hubs has finite retention (default 1–7 days). If you tried to track each batch of events as a Dagster partition with the queue as the source of truth, "rematerialize partition X" would fail once retention expires. EH Capture solves this by landing every event in ADLS as durable Parquet — partitions over ADLS files ARE re-runnable forever because the data is in object storage, not the queue.
CUSTOMIZE_EOF

cat <<MSG

============================================================================
>>> Setup complete — BLUEPRINT, requires real Azure resources.
============================================================================

This scaffolds the Dagster project + defs.yaml. It does NOT stand up Azure
Event Hubs, Capture configuration, or storage containers — that's what
$PROJECT_DIR/CUSTOMIZE.md walks through.

Architecture:
  Event Hubs producers
      │
      ▼ (built-in Azure)
  EH Capture writes Parquet files to ADLS
      │
      ▼
  adls_monitor (dynamic_partition mode) — sensor
      │
      ▼ per-file RunRequest
  raw_events (file_ingestion partitioned, reads Parquet via fsspec/adlfs)
      │
      ▼
  events_by_type (summarize)
      │
      ▼
  events_by_type_parquet (dataframe_to_parquet, writes to curated container)
      │
      ▼ (independent of Dagster)
  Synapse Serverless SQL — OPENROWSET against the curated container

Next steps:
  cd $PROJECT_DIR
  cat CUSTOMIZE.md         # prereq punch list
  # set up Azure resources per CUSTOMIZE.md
  export AZURE_STORAGE_ACCOUNT=...
  uv run dg check defs
  uv run dg dev
MSG
