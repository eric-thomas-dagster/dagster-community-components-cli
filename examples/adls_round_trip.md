# Azure ADLS Round-Trip demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

DataFrame → Azure Data Lake Storage Gen2 (Parquet) → observable external asset.

```
synthetic_data_generator → dataframe_to_adls → external_adls_asset
                                  │
                                  └─→ writes Parquet to abfs://<container>@<account>.dfs.core.windows.net/<path>
```

## What it demonstrates

- `dataframe_to_adls` writing snappy-compressed Parquet to an ADLS Gen2 container, authenticated by account key.
- `external_adls_asset` declaring the landed Parquet as an observable external asset — gives downstream lineage on the cold side of the lake without re-reading the file.

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher — your own. Cost during the demo: < $0.05/month for the storage account. |
| Azure CLI signed in | `az login` |
| `Microsoft.Storage` provider registered | One-time, ~1min: `az provider register --namespace Microsoft.Storage --wait` |
| ADLS Gen2 storage account + container | See "Provisioning" below |
| Required env vars | `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_STORAGE_ACCOUNT_KEY` |

## Provisioning (one-time, ~1 minute)

```bash
RG=dagster-demo-rg
LOC=eastus
SA=dagsterdemo$RANDOM            # globally unique; lowercase, alphanum

az group create --name "$RG" --location "$LOC"
az storage account create -g "$RG" -n "$SA" -l "$LOC" \
    --sku Standard_LRS --kind StorageV2 --hns true --access-tier Hot
KEY=$(az storage account keys list -g "$RG" -n "$SA" --query '[0].value' -o tsv)
az storage container create --name demo --account-name "$SA" --account-key "$KEY"

export AZURE_STORAGE_ACCOUNT_NAME="$SA"
export AZURE_STORAGE_ACCOUNT_KEY="$KEY"
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Generate 200 synthetic e-commerce orders |
| 2 | `dataframe_to_adls` | sink | Write Parquet to ADLS, snappy-compressed |
| 3 | `external_adls_asset` | external | Declare the landed file as an observable external asset |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_adls_round_trip_demo.sh | bash
cd adls-round-trip-demo
uv run dg launch --assets '*'
```

## Expected output

`200 rows → /round_trip/orders.parquet (~14KB, snappy)` in your `demo` container.

Verify:

```bash
az storage blob list --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
    --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
    --container-name demo --prefix round_trip/ \
    --query '[].{name:name, size:properties.contentLength}' -o table
```

```
Name                       Size
-------------------------  ------
round_trip                 0
round_trip/orders.parquet  14181
```

Read it back:

```bash
az storage blob download --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
    --account-key "$AZURE_STORAGE_ACCOUNT_KEY" \
    --container-name demo --name round_trip/orders.parquet --file /tmp/orders.parquet
uv run python -c "import pandas; print(pandas.read_parquet('/tmp/orders.parquet').head())"
```

## Cost while running

The demo writes a single 14KB Parquet to a Standard_LRS hot-tier ADLS Gen2
account in `eastus`. Storage cost is ~**$0.02 per GB per month** for hot LRS,
so this demo's data costs less than $0.0001/month. Operations
(PUT/LIST/READ) are also pennies in the free monthly allotment.

## Teardown

Deletes the resource group and everything in it:

```bash
az group delete --name dagster-demo-rg --yes
```

## What this demo isn't

- **Not a "Synapse" demo.** Synapse Analytics workspace is its own
  $$ resource and isn't required just to land data in ADLS.
- **Not an ADLS-as-source demo.** `adls_to_database_asset` reads from ADLS
  back into SQL — that's a separate demo, since it requires per-run config
  for the container/blob path. Reach out if you want it.

## Variations to try

- Change `format: parquet` to `csv` in `dataframe_to_adls/defs.yaml` to
  write CSV instead. Smaller, less efficient — but often what other systems consume.
- Add a `dataframe_join` upstream of `dataframe_to_adls` to land a
  joined dataset (orders + customers).
- Make it daily-partitioned by adding `partition_type: daily` to all three
  defs.yaml — then each `dg launch --partition <date>` writes a separate
  Parquet file under `round_trip/<date>/orders.parquet`.

## See also

<!-- TODO: link related walkthroughs -->
