# ADLS Inbox demo

Sensor-driven inbox-to-warehouse — the most common Azure data flow customers
ask about. An upstream system drops files into an ADLS Gen2 container; Dagster
sees them and lands each one in SQL automatically. No external scheduler
required.

```
adls_monitor (sensor)
      │  every 30s, lists demo/inbox/*.csv
      │  for each new file: emits a RunRequest with container/blob in run_config
      ▼
asset_job (ingest_one_file)        ← named job; sensor targets this
      ▼
adls_to_database_asset (orders_ingest)
      │  reads run_config (Dagster Config class)
      │  downloads blob, parses CSV/JSON/Parquet, writes via SQLAlchemy
      ▼
SQLite (DATABASE_URL=sqlite:////tmp/adls_inbox.db)  → table: orders
```

## Why this matters

The Azure inbox pattern is the most common ADLS use case in real customers.
Show this demo and they immediately see how to map their existing Sterling /
Connect:Direct / partner-feed / SFTP-to-ADLS / Event Grid blob-created
workflow onto Dagster. The pieces are:

1. **Sensor catches the file** the moment it arrives (30s polling default)
2. **Run config carries the file identity** — container + blob name as
   strongly-typed inputs (Pydantic Config class), not magic env vars
3. **Asset job scopes the run** to just the ingest asset, not the whole graph
4. **Asset materialization is per-file** — failures retry per blob, lineage
   tracks each file's run

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| ADLS Gen2 storage account + container | See ADLS Round-Trip demo's "Provisioning" |
| Azure CLI signed in | `az login` |
| `Microsoft.Storage` provider registered | `az provider register --namespace Microsoft.Storage --wait` |

## Required env vars

```bash
export AZURE_STORAGE_ACCOUNT_NAME=<your-storage-account>
export AZURE_STORAGE_CONNECTION_STRING=$(az storage account show-connection-string \
    -g dagster-demo-rg -n "$AZURE_STORAGE_ACCOUNT_NAME" --query connectionString -o tsv)
export DATABASE_URL=sqlite:////tmp/adls_inbox.db    # or postgresql:// / mysql://
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `adls_monitor` | sensor | Lists `demo/inbox/*.csv` every 30s; emits one RunRequest per new blob |
| 2 | `adls_to_database_asset` | ingestion | Pydantic-Config-driven asset; downloads + parses + writes per file |
| 3 | `asset_job` | infrastructure | Named job (`ingest_one_file`) that materializes just the ingest asset |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_adls_inbox_demo.sh | bash
cd adls-inbox-demo

# Drop a sample CSV into the inbox
cat > /tmp/sample_orders.csv <<'EOF'
order_id,customer_id,total
ORD0001,C001,420.50
ORD0002,C002,89.99
ORD0003,C003,1250.00
EOF

az storage blob upload \
    --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
    --container-name demo --name "inbox/orders_$(date +%Y%m%d_%H%M%S).csv" \
    --file /tmp/sample_orders.csv \
    --connection-string "$AZURE_STORAGE_CONNECTION_STRING"

# Start the webserver — sensor sees the new blob within 30s
uv run dg dev
```

In the Dagster UI:
1. Find sensor `adls_inbox_sensor` in the **Sensors** tab
2. Toggle it on (default_status is stopped)
3. Watch the run kick off automatically when the next blob is detected
4. Inspect the materialization — see container/blob in the asset metadata

Verify in SQLite:

```bash
sqlite3 /tmp/adls_inbox.db 'SELECT * FROM orders'
# ORD0001|C001|420.5
# ORD0002|C002|89.99
# ORD0003|C003|1250.0
```

## Validated end-to-end

Ran live against the same Azure subscription used for the round-trip demo.
End-to-end:

| Step | Result |
|---|---|
| Sample CSV upload | landed at `demo/inbox/orders_20260506_*.csv` (74 bytes) |
| Manual run via `dg launch --job ingest_one_file --config run.yaml` | materialized in ~1s |
| SQLite table `orders` | 3 rows present (`ORD0001`, `ORD0002`, `ORD0003`) |

## How the sensor talks to the asset

The pivotal piece is `target_op_name` on `adls_monitor`. Without it, the
sensor would emit a generic `ops.config.{container, file_path}` shape that
the asset's Pydantic `ADLSFileConfig` (which expects `container_name` and
`blob_name`) can't consume. With it set to the asset's name, the sensor
emits:

```yaml
ops:
  orders_ingest:
    config:
      container_name: demo
      blob_name: inbox/orders_20260506_110004.csv
      blob_size: 74
```

That matches the asset's `ADLSFileConfig` exactly — no glue code needed.

## Cost

| Resource | Cost |
|---|---|
| Storage account | <$0.05/month |
| Sensor evaluation | runs in the Dagster process, free |
| Per-file ingestion | seconds of local Python; effectively free |

## Teardown

```bash
az group delete --name dagster-demo-rg --yes
```

## What this isn't

- **Not an ADLS-as-streaming-source.** Sensor polls every 30s; for sub-second
  latency, Event Grid + Service Bus + an HTTP webhook are the right shape.
- **Not a multi-format orchestration.** The asset's `file_format: auto`
  works for CSV/JSON/Parquet by extension, but if you have mixed formats per
  file with format-specific transforms, split into multiple assets with
  different `file_pattern` filters in separate sensors.

## Variations

- Switch destination from SQLite → Postgres / Snowflake / etc. by setting
  `DATABASE_URL=postgresql://...` — the asset uses SQLAlchemy.
- Add `if_exists: replace` for full-refresh-per-file semantics.
- Add a downstream `summarize` asset that depends on `orders_ingest` — it'll
  re-materialize as new files arrive.
