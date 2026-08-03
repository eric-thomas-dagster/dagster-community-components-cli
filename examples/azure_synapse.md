# Azure Synapse Analytics demo

Import an existing Synapse workspace into Dagster: every Synapse pipeline
becomes a Dagster external asset that can be triggered + polled. Same
shape as the [`azure_data_factory`](azure_data_factory.md) demo but for
Synapse workspaces — Synapse is Microsoft's all-in-one analytics
platform combining Data Factory pipelines + Spark + serverless/dedicated
SQL pools + notebooks.

```
azure_synapse imports the workspace →
    1 external asset per Synapse pipeline (here: demo_wait_pipeline)
    Dagster materializes → Synapse pipeline run triggered → poll → metadata captured
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `azure_synapse` | integration | Discover Synapse pipelines / Spark jobs / notebooks, expose each as a Dagster asset, trigger + poll runs |

## Synapse vs ADF

| | Azure Data Factory | Azure Synapse |
|---|---|---|
| Pipelines | ✓ | ✓ (subset of Synapse) |
| Spark | — | ✓ (Apache Spark pools) |
| SQL — Serverless | — | ✓ (free, pay per TB scanned) |
| SQL — Dedicated | — | ✓ (DWU-priced) |
| Notebooks | — | ✓ |
| Pricing | per-activity | workspace free; pools metered separately |

If your team only uses ADF pipelines, prefer the `azure_data_factory`
component (lighter dependency footprint). If you use any of Spark / SQL
pools / notebooks alongside pipelines, use `azure_synapse`.

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.Synapse` + `Microsoft.Storage` providers | `az provider register --namespace Microsoft.Synapse --wait` |
| ADLS Gen2 storage with HNS | required by Synapse workspace |
| Synapse workspace + at least one pipeline | See "Provisioning" below |
| Service principal | scoped to RG with **Synapse Administrator** role on the workspace (see "Auth" below) |

## Auth: the Contributor-vs-Administrator gotcha

Triggering a Synapse pipeline run requires
`Microsoft.Synapse/workspaces/credentials/useSecret/action`. The
`Synapse Contributor` role does NOT grant this — `Synapse Administrator`
does.

If you see this error:

```
ClientAuthenticationError: Operation returned an invalid status 'Unauthorized'
{"code":"AccessControlUnauthorized","message":"Insufficient permissions...
does not have Microsoft.Synapse/workspaces/credentials/useSecret/action"}
```

…elevate the SP from `Synapse Contributor` to `Synapse Administrator`:

```bash
az synapse role assignment create --workspace-name "$SYN" \
    --role "Synapse Administrator" --assignee "$SP_OBJECT_ID"
```

The setup script does this for you.

## Required env vars (local development)

```bash
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export AZURE_SUBSCRIPTION_ID=...
export SYNAPSE_WORKSPACE=...
```

In Azure Container Apps / AKS with managed identity, omit these and
ensure the identity has `Synapse Administrator` on the workspace —
`DefaultAzureCredential` falls back to managed identity automatically.

## Provisioning (one-time, ~5 min)

Synapse uses Azure SQL behind the scenes. If `eastus` rejects with
`SqlServerRegionDoesNotAllowProvisioning`, fall back to `westus3` /
`eastus2` / `centralus`.

```bash
RG=dagster-demo-rg
SYN=dgsyn$(openssl rand -hex 4)
ST=dgsynst$(openssl rand -hex 3)
USER=dagsteradmin
PASS="P$(openssl rand -hex 12)!Aa"
LOC=westus3

az group create -n "$RG" -l "$LOC" 2>/dev/null || true
az provider register --namespace Microsoft.Synapse --wait
az provider register --namespace Microsoft.Storage --wait

az storage account create -g "$RG" -n "$ST" -l "$LOC" \
    --sku Standard_LRS --kind StorageV2 --hierarchical-namespace true
az storage fs create -n synapsefs --account-name "$ST" --auth-mode login

az synapse workspace create -g "$RG" -n "$SYN" -l "$LOC" \
    --storage-account "$ST" --file-system synapsefs \
    --sql-admin-login-user "$USER" --sql-admin-login-password "$PASS"

MYIP=$(curl -s https://api.ipify.org)
az synapse workspace firewall-rule create -g "$RG" --workspace-name "$SYN" \
    --name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# Service principal + Synapse Administrator role
SUB=$(az account show --query id -o tsv)
az ad sp create-for-rbac --name "dagster-synapse-sp" \
    --scopes "/subscriptions/$SUB/resourceGroups/$RG" --years 1 > /tmp/sp.json
SP_APP=$(jq -r .appId /tmp/sp.json)
SP_OBJ=$(az ad sp show --id "$SP_APP" --query id -o tsv)
az synapse role assignment create --workspace-name "$SYN" \
    --role "Synapse Administrator" --assignee "$SP_OBJ"

# Trivial demo pipeline
cat > /tmp/syn_pipe.json <<'EOF'
{"properties":{"activities":[{"name":"WaitFiveSeconds","type":"Wait","typeProperties":{"waitTimeInSeconds":5}}]}}
EOF
az synapse pipeline create --workspace-name "$SYN" \
    --name demo_wait_pipeline --file @/tmp/syn_pipe.json

export AZURE_TENANT_ID=$(jq -r .tenant /tmp/sp.json)
export AZURE_CLIENT_ID=$SP_APP
export AZURE_CLIENT_SECRET=$(jq -r .password /tmp/sp.json)
export AZURE_SUBSCRIPTION_ID=$SUB
export SYNAPSE_WORKSPACE=$SYN
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_synapse_demo.sh | bash
cd azure-synapse-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Discovery | 1 Synapse pipeline imported as `synapse_pipeline_demo_wait_pipeline` |
| Materialize | Pipeline triggered, polled Queued→Succeeded in 35s |
| Returned metadata | `run_id`, `status=Succeeded`, `start_time`, `end_time`, `duration_seconds=5.0` |

## What got fixed during validation

The component had two issues that needed fixing while building this demo:

1. **`resolve` → `build_defs`:** Component implemented the abstract method
   `resolve` instead of the required `build_defs`, causing
   `TypeError: Can't instantiate abstract class AzureSynapseComponent`.
2. **`pipeline_run.create_pipeline_run` → `pipeline.create_pipeline_run`:**
   The Azure SDK API moved this method to the `pipeline` operations
   group. The `pipeline_run` group only has `get_pipeline_run` for
   polling status.
3. **Closure default-arg interpreted as AssetIn:** Same factory-pattern fix
   applied to ADF — wrap each per-pipeline asset in a factory function
   so closure variables are kwargs of the factory, not args of the asset op.

## Cost

| Resource | Cost |
|---|---|
| Synapse workspace itself | $0 |
| Serverless SQL (queries) | $0 first 1TB/mo scanned, then $5/TB |
| Spark pools | not used here; ~$0.30/hour for Small node when idle (auto-pause) |
| Dedicated SQL pool | not used here; DWU-priced when running |
| Storage (ADLS Gen2) | ~$0.018/GB/month |

This demo's import-only flow: $0 in Synapse charges.

## Teardown

```bash
az synapse workspace delete -g dagster-demo-rg -n "$SYNAPSE_WORKSPACE" --yes
az storage account delete -g dagster-demo-rg -n <storage> --yes
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- **Spark jobs:** set `import_spark_jobs: true` to also import Synapse
  Spark job definitions as Dagster assets. The component currently
  surfaces metadata; full submission via Spark pool is a stub.
- **Notebooks:** set `import_notebooks: true` to expose notebook lineage
  in the catalog. Notebook execution requires a Spark pool and is
  typically driven via pipeline notebook activities.
- **Per-pipeline upstream deps:** the component supports the same
  `assets_by_pipeline_name.<name>.deps` pattern as `azure_data_factory`
  — wire one Synapse pipeline to specific upstream Dagster assets.
- **Filtering:** use `filter_by_name_pattern` / `exclude_name_pattern` to
  scope which pipelines are imported (regex).

## See also

<!-- TODO: link related walkthroughs -->
