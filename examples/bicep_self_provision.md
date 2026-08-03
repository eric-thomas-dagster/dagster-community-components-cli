# Bicep Self-Provisioning demo

The first asset in the graph is a Bicep deployment. Once it succeeds,
downstream assets use the storage account it just created. Dagster owns the
infra in addition to owning the data — useful for ephemeral environments,
per-tenant provisioning, or treating infra as just another asset.

```
bicep_asset (provisions storage)
        │
        └─→ synthetic_data_generator → dataframe_to_adls (uses provisioned storage)
```

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| Azure CLI + Bicep | `az login`; Bicep auto-installs on first use, or `az bicep install` |
| `Microsoft.Storage` provider | `az provider register --namespace Microsoft.Storage --wait` |
| Required env var | `AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)` |

The demo uses a separate resource group (`dagster-demo-bicep-rg`) so it doesn't
collide with the ADLS round-trip demo's RG.

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `bicep_asset` | infrastructure | Deploy `bicep/main.bicep` via `az deployment group create` |
| 2 | `synthetic_data_generator` | ai | Generate 100 synthetic orders |
| 3 | `dataframe_to_adls` | sink | Write Parquet to the just-provisioned storage |

## What the Bicep template provisions

`bicep/main.bicep`:

```bicep
@description('Storage account name (globally unique)')
param storageAccountName string = 'dagdemo${uniqueString(resourceGroup().id)}'

resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: resourceGroup().location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { isHnsEnabled: true, accessTier: 'Hot' }
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  name: '${sa.name}/default/demo'
  properties: { publicAccess: 'None' }
}

output storageAccountName string = sa.name
output blobEndpoint string = sa.properties.primaryEndpoints.blob
```

`uniqueString(resourceGroup().id)` makes the account name deterministic per
RG, so re-running the deployment is idempotent.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_bicep_self_provision_demo.sh | bash
cd bicep-self-provision-demo

# Step 1: Bicep provisions the storage account (~60s)
uv run dg launch --assets provisioned_storage

# Step 2: discover what got provisioned
SA=$(az storage account list -g dagster-demo-bicep-rg --query '[0].name' -o tsv)
KEY=$(az storage account keys list -g dagster-demo-bicep-rg -n "$SA" --query '[0].value' -o tsv)
export AZURE_STORAGE_ACCOUNT_NAME="$SA"
export AZURE_STORAGE_ACCOUNT_KEY="$KEY"

# Step 3: downstream pipeline writes to the new account
uv run dg launch --assets 'orders_raw,orders_in_provisioned_adls'
```

## Validated end-to-end

Ran live against an Azure subscription:

| Step | Result |
|---|---|
| `bicep_asset` provisioning | succeeded in ~67s; `dagdemowuzc7ysvzvpeo` storage account created |
| Downstream materialize | 100 rows → `bicep_provisioned/orders.parquet` (10.6KB snappy) |

## Why `az` CLI instead of the Python SDK?

`bicep_asset` shells out to `az deployment group create` rather than using
`azure-mgmt-resource`'s Python SDK. Two reasons:

1. **Bicep already requires the `az` CLI** for the `az bicep build` compile
   step. Adding the SDK on top is redundant.
2. **The SDK has version churn.** The `.deployments` accessor moved to a
   separate `azure-mgmt-resource-deployments` package after v25, breaking
   imports. `az` is stable.

## Cost

The Bicep deployment is free. The storage account it creates is
Standard_LRS / hot tier, ~$0.05/month. For this demo's data (~10KB), the
storage cost is essentially $0.

## Teardown

```bash
az group delete --name dagster-demo-bicep-rg --yes
```

## Variations to try

- Switch `mode: Incremental` → `mode: Complete` to have Dagster reconcile to
  match the Bicep template exactly (deletes resources not in the template).
- Set `what_if: true` to preview changes without applying — useful for
  CI/CD dry-runs before rolling forward.
- Stack a `data_factory` Bicep resource on top of the storage account, then
  add an `azure_data_factory` component asset that triggers a pipeline in it.

## What this isn't

- **Not a multi-cloud IaC story.** For AWS use `cloudformation_asset` or
  `terraform_asset`; for GCP use `gcp_deployment_manager_asset`. Same shape.
- **Not a Bicep CI demo.** This runs on `dg launch`; for CI, wrap the same
  deployment in a `cron_schedule` or trigger via the
  [external scheduler pattern](external_scheduler.md).

## See also

<!-- TODO: link related walkthroughs -->
