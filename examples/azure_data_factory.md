# Azure Data Factory demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Import an existing ADF instance into Dagster: every ADF pipeline becomes a
Dagster external asset, ADF triggers can be reflected as Dagster schedules,
and pipeline runs can be launched from Dagster on-demand. The pattern is
"Dagster as the orchestration pane on top of ADF" — ideal for teams
migrating off ADF gradually while keeping existing pipelines running, or for
teams who want a single mixed-DAG (Dagster assets + ADF pipelines) view.

```
azure_data_factory imports the ADF instance →
    1 external asset per ADF pipeline (here: demo_wait_pipeline)
    Dagster materializes → ADF run triggered → poll → per-activity metadata captured
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `azure_data_factory` | integration | Discover ADF pipelines, expose each as a Dagster asset, trigger + poll runs, capture per-activity metadata |

## Comprehensive features

The component is intentionally feature-dense because ADF orchestration is a
common ask. All of the following are configurable from `defs.yaml`:

| Capability | How it's enabled |
|---|---|
| Trigger ADF pipeline runs | `import_pipelines: true` (default) |
| Pipeline parameters (static) | `pipeline_parameters: { environment: prod, sla_minutes: 60 }` |
| Pipeline parameters (per partition) | `partition_type: daily` + `partition_parameter_name: ODATE` — ADF receives the partition key as the named parameter |
| Wait for completion | `wait_for_completion: true` (default) — poll until Succeeded/Failed/Cancelled |
| Poll interval | `run_poll_interval_seconds: 5` |
| Run timeout | `max_wait_seconds: 600` |
| Per-activity metadata | `capture_activity_metadata: true` — surfaces each activity's status, duration, error, and output keys as metadata on the asset materialization |
| Retries | `retry_policy_max_retries: 3 / retry_policy_delay_seconds: 30 / retry_policy_backoff: exponential` |
| Asset metadata: owners | `owners: ["data-platform@example.com"]` |
| Asset metadata: tags | `asset_tags: { tier: gold, sla: 60min }` |
| Extra kinds (icons) | `extra_kinds: ["azure", "etl"]` |
| Freshness checks | `freshness_max_lag_minutes: 60` or `freshness_cron: "0 7 * * *"` |
| Upstream Dagster assets (all) | `upstream_asset_keys: ["raw/orders"]` — all imported pipelines wait for these |
| Per-pipeline upstream | `assets_by_pipeline_name.<pipeline>.deps: [...]` — only that pipeline waits |
| Per-pipeline overrides | per-pipeline `key`, `description`, `group_name`, `metadata` |
| Partitioned ADF runs | one ADF run per Dagster partition_key |

## Asset graph: ADF in both directions

The component supports the standard Dagster pattern of upstream/downstream
in **two granularities**:

### A) All-pipelines upstream (broad — applies to every imported pipeline)

Use `upstream_asset_keys` when *every* imported ADF pipeline should wait
for the same set of assets:

```yaml
attributes:
  factory_name: my-data-factory
  import_pipelines: true
  upstream_asset_keys:
    - dbt_marts/orders_clean      # every ADF pipeline waits for this
    - dbt_marts/customers_clean
```

### B) Per-pipeline upstream (precise — different deps per ADF pipeline)

Use `assets_by_pipeline_name.<pipeline_name>.deps` to wire each ADF
pipeline to its own specific upstream Dagster assets. This is the more
common case in production — different pipelines have different inputs:

```yaml
attributes:
  factory_name: my-data-factory
  import_pipelines: true

  assets_by_pipeline_name:
    daily_revenue_pipeline:
      key: marts/daily_revenue
      description: "Daily revenue computed by ADF"
      group_name: revenue
      deps:
        - dbt_staging/orders        # this pipeline waits for orders
        - dbt_staging/refunds       # AND for refunds

    customer_360_pipeline:
      key: marts/customer_360
      group_name: customer
      deps:
        - dbt_staging/customers     # different upstream than the other pipeline
        - external/crm_export
```

When Dagster materializes an ADF pipeline asset, it waits only for *that*
pipeline's listed deps — not the union of all pipelines'.

You can combine both: `upstream_asset_keys` sets a global baseline, and
`assets_by_pipeline_name.<name>.deps` adds pipeline-specific deps on top.

### C) ADF pipeline → other Dagster assets

To declare a downstream Dagster asset that depends on an ADF pipeline,
reference the imported asset by its key. Each pipeline becomes
`adf_pipeline_<pipeline_name>` (or whatever you set via
`assets_by_pipeline_name.<name>.key`). For example:

```python
@asset(deps=[AssetKey("adf_pipeline_daily_revenue_pipeline")])
def revenue_dashboard_data(): ...
```

Same upstream-keying convention as the other registry integrations:
`upstream_asset_keys` (broad) and `assets_by_pipeline_name.<name>.deps`
(precise) are the two standards.

## Per-activity logs and metadata captured

When `capture_activity_metadata: true` (default), every materialization
emits:

- `pipeline_run_id` — ADF run UUID
- `monitor_url` — direct deeplink to the run in the ADF Monitor portal:
  `https://adf.azure.com/en/monitoring/pipelineruns/<run_id>?factory=<factory_uri>`
- `status` — terminal status (Succeeded / Failed / Cancelled)
- `duration_seconds` — wall clock
- `parameters` — what was sent to ADF
- `activity.<name>.status` — per-activity terminal status
- `activity.<name>.duration_seconds`
- `activity.<name>.error` — error message if failed
- `activity.<name>.output_keys` — keys present in the activity output payload
  (useful to discover row counts / file paths without bloating metadata)

Validated against the live demo pipeline:

```
ADF pipeline run started. Run ID: d510f960-496a-11f1-be02-899c9ae40149
Monitor: https://adf.azure.com/en/monitoring/pipelineruns/d510f960-...
  poll: demo_wait_pipeline status=Queued elapsed=0s
  poll: demo_wait_pipeline status=InProgress elapsed=5s
  poll: demo_wait_pipeline status=Succeeded elapsed=15s
  activity: WaitFiveSeconds (Wait) status=Succeeded duration=5.5s
```

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.DataFactory` provider | `az provider register --namespace Microsoft.DataFactory --wait` |
| ADF instance + at least one pipeline | See "Provisioning" below |
| Service principal | scoped to the RG with `Data Factory Contributor` |

## Required env vars (local development)

```bash
export AZURE_TENANT_ID=...
export AZURE_CLIENT_ID=...
export AZURE_CLIENT_SECRET=...
export AZURE_SUBSCRIPTION_ID=...
export ADF_FACTORY_NAME=...
```

## Provisioning (one-time, ~3 min)

```bash
RG=dagster-demo-rg
ADF=dagsterdemoadf$(openssl rand -hex 3)

az group create -n "$RG" -l eastus
az provider register --namespace Microsoft.DataFactory --wait
az datafactory create -g "$RG" --factory-name "$ADF" -l eastus

# Service principal scoped to the RG only
SUB=$(az account show --query id -o tsv)
az ad sp create-for-rbac --name "dagster-adf-sp" \
    --role "Data Factory Contributor" \
    --scopes "/subscriptions/$SUB/resourceGroups/$RG" \
    --years 1 > /tmp/sp.json

# Trivial demo pipeline (Wait 5s)
cat > /tmp/pipe.json <<'EOF'
{"name":"demo_wait_pipeline","properties":{"activities":[
    {"name":"WaitFiveSeconds","type":"Wait","typeProperties":{"waitTimeInSeconds":5}}]}}
EOF
az datafactory pipeline create -g "$RG" --factory-name "$ADF" \
    --name demo_wait_pipeline --pipeline @/tmp/pipe.json

export AZURE_TENANT_ID=$(jq -r .tenant /tmp/sp.json)
export AZURE_CLIENT_ID=$(jq -r .appId /tmp/sp.json)
export AZURE_CLIENT_SECRET=$(jq -r .password /tmp/sp.json)
export AZURE_SUBSCRIPTION_ID=$SUB
export ADF_FACTORY_NAME=$ADF
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_azure_data_factory_demo.sh | bash
cd azure-data-factory-demo
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Discovery | 1 ADF pipeline imported as `adf_pipeline_demo_wait_pipeline` |
| Materialize | ADF pipeline run started, status polled to `Succeeded` in 15s |
| Per-activity metadata | `WaitFiveSeconds (Wait) status=Succeeded duration=5.5s` captured |
| Monitor deeplink | clickable URL to ADF Monitor portal in the materialization metadata |

## Auth: managed identity in Azure compute

When running in Azure Container Apps, AKS, or any Azure compute with a
system-assigned or user-assigned managed identity attached, you can omit
the service principal env vars entirely. The component uses
`DefaultAzureCredential`, which falls back through:

1. Environment variables (`AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET`)
2. Managed identity (when running in Azure)
3. `az login` user (local)
4. Azure CLI / VS Code / etc.

For ACA/AKS the recommended setup is:

```bash
# Grant the managed identity Data Factory access
az role assignment create \
  --assignee <managed-identity-principal-id> \
  --role "Data Factory Contributor" \
  --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.DataFactory/factories/$ADF"
```

Then in `defs.yaml` simply omit `tenant_id_env_var/client_id_env_var/
client_secret_env_var`. The credential chain finds the managed identity
automatically.

## Cost

| Resource | Cost |
|---|---|
| ADF instance itself | $0 |
| Per-activity-run | ~$0.001 (Wait activity for this demo) |
| Pipeline orchestration | $1 per 1000 runs |
| Monitoring data retention (45 days) | $0 |

For this demo's import-only flow you pay nothing.

## Teardown

```bash
az datafactory delete -g dagster-demo-rg --factory-name "$ADF_FACTORY_NAME" --yes
az ad sp delete --id "$AZURE_CLIENT_ID"
# or full RG:
az group delete --name dagster-demo-rg --yes
```

## Variations

- **Mix Dagster + ADF DAGs:** declare the ADF asset as `deps` on
  a downstream Dagster asset (e.g. dbt) — Dagster will run the ADF
  pipeline first, then dbt.
- **Partitioned daily runs:** add `partition_type: daily` +
  `partition_start: "2026-04-01"` + `partition_parameter_name: ODATE`. Each
  Dagster partition_key triggers a fresh ADF run with `ODATE=<key>` set as
  a pipeline parameter.
- **Per-pipeline upstream wiring:** use `assets_by_pipeline_name`
  to pin a specific imported pipeline to a specific upstream — useful when
  one pipeline needs to wait for a dbt model and another doesn't.
- **External Schedules from ADF triggers:** the component can reflect ADF
  triggers as Dagster schedules; toggle with `import_triggers: true`.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
