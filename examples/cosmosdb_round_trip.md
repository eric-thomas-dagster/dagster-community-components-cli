# Azure Cosmos DB Round-Trip demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

DataFrame → Cosmos DB (NoSQL/SQL API) → SQL query back → CSV report. Validates
both the writer and reader components in one chain, with the read depending on
the write so lineage flows through the database.

```
synthetic_data_generator → cosmosdb_writer → cosmosdb_reader → dataframe_to_csv
                                  │                  │
                                  └─→ Cosmos DB ─────┘
                                     demo.orders
```

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| Azure CLI signed in | `az login` |
| `Microsoft.DocumentDB` provider registered | One-time: `az provider register --namespace Microsoft.DocumentDB --wait` |
| Cosmos DB account + database + container | See "Provisioning" below |

## Required env vars

```bash
export COSMOS_ENDPOINT="https://<account-name>.documents.azure.com:443/"
export COSMOS_KEY=$(az cosmosdb keys list -g dagster-demo-rg -n <account-name> --query primaryMasterKey -o tsv)
```

## Provisioning (one-time, ~3-5 min for account creation)

```bash
RG=dagster-demo-rg
NAME="dagsterdemocosmos$(openssl rand -hex 3)"

az group create --name "$RG" --location eastus
az cosmosdb create -g "$RG" -n "$NAME" \
    --locations regionName=eastus failoverPriority=0 \
    --enable-free-tier true \
    --default-consistency-level Session

az cosmosdb sql database create -g "$RG" -a "$NAME" --name demo
az cosmosdb sql container create -g "$RG" -a "$NAME" -d demo \
    --name orders --partition-key-path /customer_id

export COSMOS_ENDPOINT="https://$NAME.documents.azure.com:443/"
export COSMOS_KEY=$(az cosmosdb keys list -g "$RG" -n "$NAME" --query primaryMasterKey -o tsv)
```

`--enable-free-tier true` gives you 1000 RU/s + 25 GB free per subscription.

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate 50 synthetic orders |
| 2 | `cosmosdb_writer` | sink | Upsert each row as a Cosmos document; `id_field=order_id` to satisfy Cosmos's required `id` |
| 3 | `cosmosdb_reader` | source | Run a SQL query (`SELECT * FROM c WHERE c.total > 500 ORDER BY c.total DESC`) and return rows as a DataFrame |
| 4 | `dataframe_to_csv` | sink | Write the high-value-orders report locally |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cosmosdb_round_trip_demo.sh | bash
cd cosmosdb-round-trip-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| `cosmosdb_writer` | 50 items upserted into `demo.orders` |
| `cosmosdb_reader` | 9 rows returned (those with `total > 500`) |
| `dataframe_to_csv` | `/tmp/cosmos_high_value_orders.csv` written; rows include Cosmos system fields (`_rid`, `_etag`, `_ts`) |

## Cosmos requires `id`

Cosmos's NoSQL/SQL API requires every document to have an `id` string field.
The `cosmosdb_writer` component's `id_field` parameter copies a column at
write time:

```yaml
id_field: order_id    # writes {id: 'ORD00000001', order_id: 'ORD00000001', ...}
```

If you don't set this and your rows lack `id`, you'll get
`CosmosHttpResponseError: BadRequest`.

## Cost

| Resource | Cost |
|---|---|
| Free tier (1 free Cosmos account / subscription) | $0 |
| Without free tier (smallest provisioned-throughput container) | ~$24/mo |
| Serverless | cents per run for this volume |

## Teardown

```bash
az cosmosdb delete -g dagster-demo-rg -n <account-name> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- Swap to `if_exists: insert` to fail on duplicate IDs (vs upsert).
- Tune the read query in `cosmosdb_reader/defs.yaml` — Cosmos SQL supports
  WHERE / GROUP BY / aggregates / spatial / array_contains.
- Add a `cosmosdb_resource` (existing component) so multiple assets share
  one client, useful when the read and write live in the same process.

## See also

<!-- TODO: link related walkthroughs -->
