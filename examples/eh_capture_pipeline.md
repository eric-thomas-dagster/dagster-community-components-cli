# Event Hubs Capture → ADLS → Dagster → Synapse — blueprint

Production-shape streaming pipeline. **Dagster doesn't process the queue directly** — Event Hubs Capture (a built-in Azure service) lands every event in ADLS as durable **Avro** files (the Standard-tier default; Parquet capture is Premium/preview), and Dagster picks up files event-driven. Each new file = one Dagster dynamic partition = fully re-runnable.

`file_ingestion` reads Avro natively (via `fastavro`); the curated sink can write Parquet for downstream Athena-style queries, or stay in Avro via `dataframe_to_avro` if your downstream consumers prefer it.

> **Validation status:** the Dagster wiring is buildable and `dg check` passes. End-to-end execution requires a real Azure Event Hubs namespace with Capture enabled + a Storage Account. Blueprint.

## Why this architecture (the design rationale)

The instinct is "Dagster should consume the queue with a sensor that microbatches messages." Don't.

| Problem | Why direct-queue-consumption fails |
|---|---|
| **Partitions must be re-runnable.** Dagster's dynamic partition model assumes "rematerialize this partition key" works. | Event Hubs has finite retention (default 1 day, up to 90). After retention, the events are gone — replay of a partition fails. |
| **Sensor cursor is small.** Accumulating message payloads across ticks would blow past the cursor size limit. | The sensor would need its own durable spool — at which point you're reinventing EH Capture badly. |
| **Operational ergonomics.** Compression, schema evolution, retention policy, encoding — these are managed-service concerns. | EH Capture handles all of them. |

EH Capture is the right boundary. It owns the queue → storage step. Dagster owns the file → DataFrame → curated → analytics step. Each side does what it's good at.

## Architecture

```
   ┌──────────────────────────────────────────┐
   │ Producers (any language / runtime)        │
   │   ↓                                       │
   │ Azure Event Hubs (topic-like)             │
   └─────────────────┬─────────────────────────┘
                     │
                     ▼  Event Hubs Capture (built-in Azure service)
   ┌──────────────────────────────────────────┐
   │ ADLS: eh-capture container                │
   │   namespace/eventhub/2026/05/13/14/00/00.parquet
   │   namespace/eventhub/2026/05/13/14/05/00.parquet
   │   ...                                     │
   └─────────────────┬─────────────────────────┘
                     │
   ┌─────────────────▼─────────────────────────┐
   │ Dagster                                    │
   │                                            │
   │ adls_monitor (partition_mode:              │
   │   dynamic_partition) — registers each new  │
   │   Parquet file as a partition, fires       │
   │   RunRequest(partition_key=<file_path>).   │
   │                ↓                           │
   │ raw_events (file_ingestion partitioned)    │
   │   reads abfss://eh-capture/<partition>.parquet
   │                ↓                           │
   │ events_by_type (summarize partitioned)     │
   │   group_by + aggs per file.                │
   │                ↓                           │
   │ events_by_type_parquet                     │
   │   writes curated Parquet to a different    │
   │   ADLS path, one file per partition.       │
   └─────────────────┬─────────────────────────┘
                     │
                     ▼ (independent of Dagster — query directly)
   ┌──────────────────────────────────────────┐
   │ Synapse Serverless SQL via OPENROWSET     │
   │ Standard ANSI SQL over the curated path.  │
   └──────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `adls_monitor` | community | Polls the ADLS Capture container, registers each new Parquet file as a dynamic partition, yields `RunRequest(partition_key=...)`. |
| `file_ingestion` | community | Partitioned asset reads each Parquet file as a pandas DataFrame. |
| `summarize` | community | Per-partition group-by + aggregations. |
| `dataframe_to_parquet` | community | Writes the curated Parquet back to ADLS (one file per partition). |

Plus the Azure-native pieces you provide: the Event Hub namespace, the Capture configuration, the Storage Account + containers, and (optionally) a Synapse Serverless SQL pool.

## Azure setup (prerequisites you provide)

### 1. Storage account + containers

```bash
RG=my-resource-group
SA=mystorageacct$(openssl rand -hex 4)
az group create -n "$RG" -l eastus
az storage account create -g "$RG" -n "$SA" -l eastus --sku Standard_LRS --kind StorageV2 --hierarchical-namespace true
az storage container create --account-name "$SA" -n eh-capture
az storage container create --account-name "$SA" -n curated
```

### 2. Event Hubs namespace + Capture

```bash
EHN=my-eh-ns$(openssl rand -hex 4)
az eventhubs namespace create -g "$RG" -n "$EHN" -l eastus --sku Standard
az eventhubs eventhub create -g "$RG" --namespace-name "$EHN" -n my-event-hub \
  --partition-count 4 \
  --enable-capture true \
  --capture-interval 300 --capture-size-limit 314572800 \
  --destination-name EventHubArchive.AzureBlockBlob \
  --storage-account "$SA" --blob-container eh-capture \
  --archive-name-format "namespace/eventhub/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}.parquet"
```

Notes:
- `--capture-interval 300` = flush every 5 minutes regardless of size.
- `--capture-size-limit 314572800` = 300 MB max per file.
- The path pattern controls how files land. Match it in the `directory_path:` field on `adls_monitor`.

### 3. Synapse Serverless SQL pool (optional — for the analytics surface)

Provision a Synapse workspace + Serverless SQL pool. We have a [`azure_synapse_serverless.md`](azure_synapse_serverless.md) walkthrough covering the OPENROWSET wiring against a storage container.

### 4. Auth

Either use `DefaultAzureCredential` (managed identity / service principal) or a connection string. `adls_monitor` supports both — see its README. The defs.yaml uses Default credential by referencing only `AZURE_STORAGE_ACCOUNT` env var; the credential discovery picks up `az login` / managed identity / `AZURE_CLIENT_ID`+`AZURE_CLIENT_SECRET`+`AZURE_TENANT_ID` automatically.

## Run the demo

```bash
./setup_eh_capture_pipeline_demo.sh
cd eh-capture-pipeline-demo

export AZURE_STORAGE_ACCOUNT=$SA
uv run dg check defs
uv run dg dev
```

In the UI: enable the `eh_capture_sensor`. Within ~30s (the sensor's poll interval) it picks up any existing Parquet files in the Capture container, registers each as a dynamic partition, and launches the per-partition pipeline. Each new file landed by Capture from this point on triggers another run.

## Querying via Synapse Serverless (independent of Dagster)

```sql
SELECT event_type, SUM(event_count) AS total_events
FROM OPENROWSET(
    BULK 'https://<storage_account>.dfs.core.windows.net/curated/events_by_type/**',
    FORMAT = 'PARQUET'
) AS r
GROUP BY event_type
ORDER BY total_events DESC;
```

Build a Synapse external table on the same path for a stable BI surface.

## Trade-offs & gotchas

- **EH Capture is per-Event-Hub, not per-namespace.** Each Event Hub configures its own Capture destination. If you have many event hubs, you'll have many ADLS paths to watch (one `adls_monitor` per — or one with a broad `recursive` prefix).
- **Capture latency = your minimum batch size.** With `--capture-interval 300`, files land every 5 min minimum. Dagster's sensor + asset add another 30s–1min on top. Net: ~5-7 min lag from event publish to curated Parquet.
- **Schema evolution.** EH Capture writes whatever the events' schema is at the time. If your producer schema changes, downstream files will have a new schema — `file_ingestion`'s `format: parquet` reads each file independently, so this Just Works at the file level; `summarize`'s `group_by` may break if it references a removed column.
- **Cost.** Storage Account writes (Capture's job) + storage egress (Dagster's reads). Both are pennies-per-GB-per-month at small scales. The fixed cost is the Event Hubs Standard SKU.
- **Re-runnable.** Every partition in Dagster's UI can be rematerialized. The Parquet is in ADLS forever (or until you lifecycle-delete it).

## See also

- [`adls_round_trip.md`](adls_round_trip.md) — Simpler ADLS read/write demo (no streaming source).
- [`adls_inbox.md`](adls_inbox.md) — Same `adls_monitor` pattern but driven by manual file drops.
- [`azure_synapse_serverless.md`](azure_synapse_serverless.md) — OPENROWSET setup details.
- [`pubsub_gcs_pipeline.md`](pubsub_gcs_pipeline.md) — GCP mirror of this demo (Pub/Sub → GCS Subscription → BigQuery).
