# Azure Event Hubs Round-Trip demo

100 synthetic e-commerce orders → DataFrame → published to Azure Event
Hubs → consumed by [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) → landed in Azure
PostgreSQL. Round-trips through a real message queue with lineage flowing
across the broker.

```
synthetic_data_generator → dataframe_to_eventhub → Event Hub
                                                       │
                       ┌───────────────────────────────┘
                       ▼
            eventhubs_to_database_asset → Azure Postgres ('orders_received')
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | ai | 100 synthetic e-commerce orders |
| 2 | [`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) (NEW) | sink | Publish each row as a JSON event; pinned by `customer_id` partition key |
| 3 | [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) | ingestion | Consume events from EH and bulk-insert into Postgres |

### Why a new producer component?

The user asked: should the producer be a component or a CLI? We added
[`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) so the demo composes from registry components
without a custom asset. Since each message-queue SDK has distinct auth +
partition-key semantics (Event Hubs vs Kafka vs PubSub vs Kinesis vs
Redis Streams), each gets its own focused component rather than one fat
multi-queue component. [`redis_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/redis_writer) and [`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) are
already in the registry; `dataframe_to_kafka`, `dataframe_to_pubsub`, and
`dataframe_to_kinesis` are planned in the same shape.

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.EventHub` provider | `az provider register --namespace Microsoft.EventHub --wait` |
| Event Hubs namespace + a hub | See "Provisioning" below |
| `Microsoft.DBforPostgreSQL` provider | required by the consumer (lands events in Postgres) |
| Postgres Flexible Server + DB | re-uses the [`azure_postgres`](azure_postgres.md) demo's |

## Required env vars

```bash
export EVENTHUB_CONNECTION_STRING=...
export EVENTHUB_NAME=demo-events
export DATABASE_URL="postgresql+psycopg2://..."   # Azure Postgres
```

## Provisioning (one-time, ~2 min)

```bash
RG=dagster-demo-rg
EH_NS=dgeh$(openssl rand -hex 4)
EH_NAME=demo-events

az group create -n "$RG" -l eastus 2>/dev/null || true
az provider register --namespace Microsoft.EventHub --wait
az eventhubs namespace create -g "$RG" -n "$EH_NS" -l eastus --sku Basic
az eventhubs eventhub create -g "$RG" --namespace-name "$EH_NS" -n "$EH_NAME" \
    --partition-count 2 --cleanup-policy Delete --retention-time-in-hours 24

export EVENTHUB_NAME=$EH_NAME
export EVENTHUB_CONNECTION_STRING=$(az eventhubs namespace authorization-rule keys list \
    -g "$RG" --namespace-name "$EH_NS" --name RootManageSharedAccessKey \
    --query primaryConnectionString -o tsv)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_eventhubs_demo.sh | bash
cd azure-eventhubs-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| [`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) | Sent 100/100 events to `demo-events` in 1.37s |
| [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) | Consumed 200 events → 10 cols, drained in 6.12s |
| Verification | `SELECT COUNT(*) FROM orders_received` returns 200 rows |

(Consumer drained 200 because a previous failed run left 100 events in
the hub before the consumer fix landed; with a fresh hub it'd be exactly
100.)

## What got fixed during validation

While building this demo we shipped:

1. **[`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub)** — the producer component
2. **[`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) consumer hang fix** — `receive_batch()`
   loops forever; previous logic returned early from the callback but
   never closed the client. Now closes from inside the callback once
   max_events is reached, with the exception caught and treated as clean
   shutdown when records were already collected.

## Auth: managed identity in Azure compute

When running in Azure Container Apps or AKS, you can swap connection
strings for Entra-ID auth. The `azure-eventhub` SDK accepts an
`AsyncCredential` or `Credential` object via:

```python
EventHubProducerClient(fully_qualified_namespace=..., credential=DefaultAzureCredential())
```

The current components consume the connection string for portability;
upgrading to Entra-ID auth is a per-component config addition (not
yet wired). For local development, use the connection string with a
namespace-scoped or hub-scoped SAS rule.

## Cost

| Tier | Cost | Notes |
|---|---|---|
| Basic | ~$0.015/hr (~$11/mo) + $0.028/M events | What we used. No consumer groups, 1-day retention |
| Standard | ~$0.025/hr (~$18/mo) + same | 20 consumer groups, capture-to-blob, 7-day retention |
| Premium / Dedicated | $$$ | Reserved capacity, geo-DR |

For 100 events: <$0.001 in event charges; namespace itself is the dominant cost.

## Teardown

```bash
az eventhubs namespace delete -g dagster-demo-rg -n <namespace> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- **Stream-driven downstream:** use the [`eventhubs_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/eventhubs_monitor) sensor to
  trigger [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) reactively whenever a new
  partition advances (instead of asset materialization).
- **Different sink:** the consumer's `database_url_env_var` works against
  any SQLAlchemy URL — flip to mssql+pymssql / mysql+pymysql for Azure SQL
  / MySQL.
- **Partition key:** the demo pins events by `customer_id` so per-customer
  ordering is preserved. Drop `partition_key_column` for round-robin
  distribution.
