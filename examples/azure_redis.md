# Azure Cache for Redis Round-Trip demo

DataFrame → Redis hashes (TLS on port 6380) → SQL query back as DataFrame
→ CSV report. Validates `redis_writer` and `redis_reader` against a real
Azure managed Redis with TLS, with the read depending on the write so
lineage flows through the cache.

```
synthetic_data_generator → redis_writer → redis_reader → dataframe_to_csv
                                │              │
                                └─→ Azure Cache for Redis ─┘
                                          (ORD*)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate 30 synthetic orders |
| 2 | `redis_writer` | sink | HSET each row keyed by `order_id`, expire after 1h |
| 3 | `redis_reader` | source | Read all `ORD*` hashes back as a DataFrame |
| 4 | `dataframe_to_csv` | sink | Write the report locally |

`redis_writer` and `redis_reader` both gained an `ssl: bool` field; setting
`ssl: true` is required for Azure Cache for Redis (which only accepts
TLS connections on port 6380).

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.Cache` provider | `az provider register --namespace Microsoft.Cache --wait` |
| Azure Cache for Redis instance | See "Provisioning" below (~15-25 min provisioning) |
| `REDIS_HOST`, `REDIS_PASSWORD` env vars | from `az redis list-keys` |

## Provisioning (one-time, ~15-25 min)

```bash
RG=dagster-demo-rg
REDIS_NAME=dgredis$(openssl rand -hex 3)

az group create -n "$RG" -l eastus 2>/dev/null || true
az provider register --namespace Microsoft.Cache --wait

# Basic C0 — smallest / cheapest tier (~$16/mo). Provisioning takes
# 15-25 minutes; the create command blocks until ready.
az redis create -g "$RG" -n "$REDIS_NAME" -l eastus \
    --sku Basic --vm-size c0

export REDIS_HOST="$REDIS_NAME.redis.cache.windows.net"
export REDIS_PASSWORD=$(az redis list-keys -g "$RG" -n "$REDIS_NAME" \
    --query primaryKey -o tsv)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_redis_demo.sh | bash
cd azure-redis-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| `redis_writer` | 30 rows HSET to `ORD*` keys (TLS:6380) in ~1s |
| `redis_reader` | 30 hashes read back as a DataFrame (1.6s) |
| `dataframe_to_csv` | `/tmp/redis_orders_report.csv` written, 30 rows |

Sample output (CSV):

```
key,order_id,customer_id,order_date,category,num_items,subtotal,...,status
ORD00000025,ORD00000025,CUST000690,2025-06-06 15:14:59,Food,1,21.52,...,pending
ORD00000014,ORD00000014,CUST000611,2025-09-09 15:14:59,Food,5,532.96,...,delivered
```

## TLS and managed identity

Azure Cache for Redis enforces TLS on port 6380. Set both fields:

```yaml
port: 6380
ssl: true
```

The components use `password_env_var` for auth. If you want **passwordless
auth via Microsoft Entra ID** (managed identity in ACA / AKS), see
<https://learn.microsoft.com/azure/azure-cache-for-redis/cache-azure-active-directory-for-authentication>
— Entra-ID auth on Cache for Redis is in preview and requires SKU Premium
or higher.

## Cost

| Tier | Cost | Notes |
|---|---|---|
| Basic C0 (this demo) | ~$0.022/hr (~$16/mo) | 250MB cache, single node, no SLA |
| Standard C0 | ~$0.055/hr (~$40/mo) | 250MB cache, replicated, 99.9% SLA |
| Premium P1 | ~$0.39/hr (~$285/mo) | 6GB, clustering, persistence, Entra-ID auth |

Cannot auto-stop; delete when done.

## Teardown

```bash
az redis delete -g dagster-demo-rg -n <name> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- **Streams instead of hashes:** swap `redis_writer` for the
  `redis_streams_to_database_asset` ingestion path (Redis Streams are a
  log-structured queue with consumer groups).
- **Cache aside:** use `redis_reader` as a source upstream of dbt /
  ML inference; write to Redis with `redis_writer` after long-running
  computation.
- **Multi-region:** use Premium-tier geo-replication; this demo's Basic
  tier is single-region.
