# Event Hubs Capture → ADLS → Dagster → Synapse — blueprint

Production-shape streaming pipeline. **Dagster doesn't process the queue directly** — Event Hubs Capture (a built-in Azure service) lands every event in ADLS as durable **Avro** files (the Standard-tier default; Parquet capture is Premium/preview), and Dagster picks up files event-driven. Each new file = one Dagster dynamic partition = fully re-runnable.

`file_ingestion` reads Avro natively (via `fastavro`); the curated sink can write Parquet for downstream Athena-style queries, or stay in Avro via `dataframe_to_avro` if your downstream consumers prefer it.

> **Validation status:** end-to-end validated 2026-05-13 against a Pay-As-You-Go Azure subscription. 500 events sent to EH → Capture flushed two Avro files (~40 KB each, one per partition) → Dagster's `file_ingestion` (`format: avro`) read 250 rows × 6 cols. See **Validation gaps** below for the failure modes we hit along the way (hns storage + RBAC paths) and the working config that produced the success.

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
# Use FLAT-NAMESPACE storage (--hns omitted / false). With hierarchical-namespace,
# EH Capture's default auth path silently fails to write — see "Validation gaps" below.
az storage account create -g "$RG" -n "$SA" -l eastus --sku Standard_LRS --kind StorageV2
az storage container create --account-name "$SA" -n eh-capture --auth-mode login
az storage container create --account-name "$SA" -n curated --auth-mode login
```

### 2. Event Hubs namespace + Capture

```bash
EHN=my-eh-ns$(openssl rand -hex 4)
az eventhubs namespace create -g "$RG" -n "$EHN" -l eastus --sku Standard

# Assign system-managed identity (REQUIRED — without this, Capture silently fails to write).
az eventhubs namespace identity assign --namespace-name "$EHN" -g "$RG" --system-assigned
EH_PID=$(az eventhubs namespace show -g "$RG" --name "$EHN" --query identity.principalId -o tsv)
SA_ID=$(az storage account show -g "$RG" -n "$SA" --query id -o tsv)

# Grant the namespace's MI permission to write to the storage account.
az role assignment create --assignee-object-id "$EH_PID" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$SA_ID"

# Now create the event hub with Capture pointed at the storage account.
az eventhubs eventhub create -g "$RG" --namespace-name "$EHN" -n my-event-hub \
  --partition-count 4 \
  --enable-capture true \
  --capture-interval 300 --capture-size-limit 314572800 \
  --destination-name EventHubArchive.AzureBlockBlob \
  --storage-account "$SA_ID" --blob-container eh-capture \
  --archive-name-format "{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}"
```

> **Critical:** the `az eventhubs namespace identity assign` + `role assignment create` steps **must run before** `eventhub create --enable-capture`. Without them, the EH service can't write to the storage account and Capture stays silent — `eventhub show` reports `captureDescription.enabled: true` but the container remains empty. The CLI surfaces zero errors for this; the real error only appears in the namespace's Azure Activity Log. See the **Validation gaps** section below.

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

## Validation gaps (what we learned end-to-end, 2026-05-13)

We validated this blueprint against a Pay-As-You-Go Azure subscription. The blueprint works end-to-end — but along the way we hit two real-world friction points that the `az` CLI silently glosses over. The walkthrough above already reflects the working config; this section is the diagnostic reference.

### 1. Hierarchical-namespace storage breaks Capture's default auth path

If your storage account is created with `--hierarchical-namespace true` (ADLS Gen2), Capture's default shared-key auth doesn't work. The CLI reports `captureDescription.enabled: true` and accepts 750+ events without error, but the container stays empty forever.

**Symptoms:**
- `az storage blob list ... -c eh-capture` returns only a single zero-byte placeholder directory entry.
- `az eventhubs eventhub show` reports Capture is enabled.
- No CLI errors anywhere. The actual error lives in the EH namespace's Azure Activity Log.

**Fix:** use flat-namespace storage (omit `--hns` / set `--hierarchical-namespace false`). Capture's default auth path works against flat blob accounts out of the box. We validated end-to-end with this configuration — 500 events → 40 KB Avro file per partition within one capture interval.

**If you must use ADLS Gen2:** enable a system-assigned managed identity on the EH namespace and grant it `Storage Blob Data Contributor` on the storage account:

```bash
az eventhubs namespace identity assign --namespace-name $EHN -g $RG --system-assigned
EH_PID=$(az eventhubs namespace show -g $RG --name $EHN --query identity.principalId -o tsv)
az role assignment create --assignee-object-id $EH_PID --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope $(az storage account show -g $RG -n $SA --query id -o tsv)
```

These need to be in place **before** `eventhub create --enable-capture`. We weren't able to make this path work in our validation session even with all of the above; suspect there's an additional ADLS-Gen2-specific config (capture's `identity` field on the description) that the `az` CLI doesn't fully wire. Investigate the activity log first.

### 2. URI scheme depends on storage account type

`file_ingestion` reads via `fsspec` (`adlfs`). The URI scheme that works depends on which storage account variant you provisioned:

| Storage account | URI to use | What it looks like |
|---|---|---|
| Flat-namespace (StorageV2, no hns) | `az://<container>/<path>` + `AZURE_STORAGE_ACCOUNT_NAME` env var | `az://eh-capture/path/to/file.avro` |
| Flat-namespace, explicit form | `https://<account>.blob.core.windows.net/<container>/<path>` | `https://mysa.blob.core.windows.net/eh-capture/path` |
| Hierarchical namespace (ADLS Gen2) | `abfss://<container>@<account>.dfs.core.windows.net/<path>` | `abfss://eh-capture@mysa.dfs.core.windows.net/path` |

Using `abfss://...dfs...` against a flat-namespace account, or `az://` against an hns account, will fail with auth errors that look like permission problems. They're actually URI-scheme mismatches.

### 3. Auth for `file_ingestion`'s reads

`adlfs` picks up either:
- **Managed Identity / DefaultAzureCredential** — requires the principal (your user or the compute identity) to have `Storage Blob Data Reader` on the storage account, plus several minutes for RBAC propagation. We hit this in validation — granting yourself the role and immediately running the asset still failed with `AuthorizationPermissionMismatch`; needed a 30-60s pause.
- **Shared key via `AZURE_STORAGE_KEY` env var** — instant, no propagation lag. Works for dev / single-account scenarios; not what you want in production (no per-user audit; key rotation is a config change).

In our validation we used the shared-key path for speed; in production prefer Managed Identity with `Storage Blob Data Reader` granted ahead of time.

### Working configuration (what we proved)

```bash
# Storage: flat namespace (CRITICAL)
az storage account create -g $RG -n $SA -l eastus --sku Standard_LRS --kind StorageV2
# NO --hierarchical-namespace flag

# EH namespace + event hub with Capture
az eventhubs namespace create -g $RG -n $EHN -l eastus --sku Standard
az eventhubs eventhub create -g $RG --namespace-name $EHN -n my-event-hub \
  --partition-count 2 --enable-capture true \
  --capture-interval 60 --capture-size-limit 10485760 \
  --destination-name 'EventHubArchive.AzureBlockBlob' \
  --storage-account $(az storage account show -g $RG -n $SA --query id -o tsv) \
  --blob-container eh-capture \
  --archive-name-format '{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}'

# Dagster auth (flat-ns + shared key)
export AZURE_STORAGE_ACCOUNT_NAME=$SA
export AZURE_STORAGE_KEY=$(az storage account keys list -g $RG -n $SA --query "[0].value" -o tsv)
```

```yaml
# defs/raw_events/defs.yaml
type: <pkg>.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: raw_events
  format: avro
  file_path: "az://eh-capture/<namespace>/<eventhub>/<partition>/<...>/<seconds>.avro"
```

Result on validation: 250 rows × 6 cols loaded into the asset's DataFrame. Capture's empty-archive markers (~508 bytes — Avro headers with no records) are a normal byproduct of empty intervals; only flush windows with actual events produce real-sized files.

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
