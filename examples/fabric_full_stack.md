# Microsoft Fabric Full-Stack demo

End-to-end Fabric pipeline using all 6 Fabric components in the registry:
write a DataFrame to a Lakehouse, query it from the Fabric SQL endpoint,
and trigger a Fabric Data Pipeline — all from Dagster.

```
synthetic_data_generator
   │
   ▼  (50 synthetic orders DataFrame)
fabric_lakehouse_io_manager  ── auto-serializes as Delta on OneLake (Lakehouse: SalesLakehouse)
   │
   ▼  (Lakehouse table 'orders' auto-registers in SQL endpoint)
dataframe_from_sql           ── querying the Fabric Warehouse SQL endpoint
                                (mssql+pyodbc URL)
   │
   ▼  (aggregated DataFrame: revenue by category)
dataframe_to_csv             ── final report

Plus:
   fabric_workspace          ── one Dagster external asset per Fabric pipeline / lakehouse / etc.
   fabric_pipeline_trigger_job ── op-job that triggers ONE specific Fabric pipeline on schedule
```

## All 6 Fabric components in the registry

| Component | Category | Purpose |
|---|---|---|
| `fabric_workspace` | integration | Discover Fabric workspace items (lakehouses, warehouses, notebooks, pipelines) as Dagster assets |
| `fabric_workspace_resource` | resource | Reusable Fabric REST API client for ad-hoc calls from custom ops |
| `fabric_lakehouse_resource` | resource | OneLake URL helper + storage options for Delta access |
| `fabric_lakehouse_io_manager` | io_manager | Auto-serialize asset DataFrames as Delta tables in a Lakehouse |
| `dataframe_to_fabric_lakehouse` | sink | Explicit per-table sink (one DataFrame → one Delta table) |
| `fabric_pipeline_trigger_job` | infrastructure | Op-job to trigger ONE Fabric pipeline / notebook / dataflow on a schedule |

## Reuse-vs-new

Fabric Data Warehouse uses a T-SQL endpoint compatible with SQL Server
tooling — so existing components handle it without Fabric-specific code:

| Need | Existing component | Fabric URL pattern |
|---|---|---|
| Read from Fabric Warehouse | `dataframe_from_sql` | `mssql+pyodbc://<id>.datawarehouse.fabric.microsoft.com:1433/<db>?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryServicePrincipal&UID=<sp>&PWD=<secret>` |
| Write to Fabric Warehouse | `dataframe_to_table` | same URL |
| Cross-warehouse query | `dataframe_from_sql` with arbitrary CTEs / joins | same |

## Prerequisites

| Need | How to get it |
|---|---|
| Azure subscription | Pay-As-You-Go or higher |
| `Microsoft.Fabric` provider | `az provider register --namespace Microsoft.Fabric --wait` |
| Fabric tenant enabled | Power BI Admin Portal → Tenant settings → enable Fabric for your org/group |
| Fabric capacity | F2 minimum (~$0.21/hr); see "Provisioning" |
| Workspace + Lakehouse + Warehouse | Created via Fabric portal or REST API |
| Service principal | Granted Contributor on the workspace |

## Provisioning (~5 min once tenant is unblocked)

```bash
RG=dagster-demo-rg
FAB_CAP=dgfabric$(openssl rand -hex 4)
ME_OID=$(az ad signed-in-user show --query id -o tsv)

az provider register --namespace Microsoft.Fabric --wait
az fabric capacity create -g "$RG" -n "$FAB_CAP" -l eastus \
    --sku '{"name":"F2","tier":"Fabric"}' \
    --administration "{\"members\":[\"$ME_OID\"]}"
```

⚠️ If `az fabric capacity create` returns `Unauthorized`, your tenant
hasn't enabled Fabric yet. A Power BI tenant admin needs to:

1. Go to admin.powerbi.com → Tenant settings
2. Find "Microsoft Fabric" → set "Apply to: entire organization" (or a specific security group)
3. Wait ~15 min for the setting to propagate

After capacity is created, the workspace + lakehouse + warehouse must be
created via the Fabric portal (UI) or Fabric REST API:

```bash
TOKEN=$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)

# Create workspace
WS_RESP=$(curl -s -X POST https://api.fabric.microsoft.com/v1/workspaces \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"displayName":"DagsterDemo","capacityId":"'"$FAB_CAP_ID"'"}')
WORKSPACE_ID=$(echo "$WS_RESP" | jq -r .id)

# Create Lakehouse
curl -s -X POST "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/items" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"displayName":"SalesLakehouse","type":"Lakehouse"}'

# Add SP as Contributor
curl -s -X POST "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/roleAssignments" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"role":"Contributor","principal":{"id":"'"$SP_OID"'","type":"ServicePrincipal"}}'
```

## Configure (defs/)

### 1. Bronze layer — synthetic data

```yaml
type: dagster_component_templates.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 100
  group_name: bronze
```

### 2. Silver layer — Delta in Lakehouse via IO manager

Set the Fabric Lakehouse IO manager as the workspace default. Now any
asset returning a DataFrame lands as a Delta table:

```yaml
# defs/iomgr/defs.yaml
type: dagster_component_templates.FabricLakehouseIOManagerComponent
attributes:
  resource_key: io_manager
  workspace_id: "12345678-1234-1234-1234-123456789012"
  lakehouse_name: SalesLakehouse
  if_exists: overwrite
  tenant_id_env_var: AZURE_TENANT_ID
  client_id_env_var: AZURE_CLIENT_ID
  client_secret_env_var: AZURE_CLIENT_SECRET
```

`orders_raw` (DataFrame) → auto-serialized to
`abfss://{ws_id}@onelake.dfs.fabric.microsoft.com/SalesLakehouse.Lakehouse/Tables/orders_raw`

### 3. Gold layer — query via Fabric Warehouse SQL endpoint

The Lakehouse table auto-registers in the Fabric SQL endpoint. We can
query it via `dataframe_from_sql` using the warehouse URL pattern:

```yaml
# defs/orders_revenue/defs.yaml
type: dagster_component_templates.DataframeFromSqlComponent
attributes:
  asset_name: orders_revenue
  database_url_env_var: FABRIC_WAREHOUSE_URL  # mssql+pyodbc://...
  query: |
    SELECT category, COUNT(*) AS n, SUM(total) AS revenue
    FROM orders_raw
    GROUP BY category
  deps: [orders_raw]
  group_name: gold
```

The URL:

```
mssql+pyodbc://{sp_app_id}@{tenant_id}:<urlencoded-secret>@{warehouse_id}.datawarehouse.fabric.microsoft.com:1433/{warehouse_name}?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryServicePrincipal
```

### 4. CSV report

```yaml
type: dagster_component_templates.DataframeToCsvComponent
attributes:
  asset_name: orders_revenue_report
  upstream_asset_key: orders_revenue
  file_path: /tmp/fabric_orders_report.csv
  group_name: report
```

### 5. Bonus — Fabric workspace import + scheduled pipeline trigger

```yaml
# defs/fabric_workspace/defs.yaml — discover all workspace items as Dagster assets
type: dagster_component_templates.FabricWorkspaceComponent
attributes:
  workspace_id: "12345678-1234-1234-1234-123456789012"
  import_lakehouses: true
  import_pipelines: true
  group_name: fabric

# defs/scheduled_pipeline/defs.yaml — trigger a specific Fabric pipeline daily
type: dagster_component_templates.FabricPipelineTriggerJobComponent
attributes:
  job_name: fabric_daily_etl
  workspace_id: "12345678-1234-1234-1234-123456789012"
  item_id: "abcdef12-3456-7890-abcd-ef1234567890"
  item_type: DataPipeline
  schedule: "0 2 * * *"
```

## Validation status

This walkthrough is **code-validated** against:
- The Fabric REST API spec (https://learn.microsoft.com/rest/api/fabric/)
- The OneLake URL spec (`abfss://...@onelake.dfs.fabric.microsoft.com/...`)
- delta-rs storage options for Azure
- The Fabric Warehouse SQL endpoint connection string format

End-to-end validation requires a Fabric capacity AND tenant Fabric
enablement (via Power BI tenant settings). My subscription has Fabric
provider registered but the tenant Power BI admin hasn't enabled Fabric
yet, so capacity provisioning returns `Unauthorized`. Once your tenant
admin enables Fabric, the same components ship will work end-to-end.

## Cost

| Resource | Cost |
|---|---|
| F2 Fabric capacity | ~$0.21/hr (~$154/mo if always on) |
| F4 | ~$0.42/hr |
| OneLake storage | Included in capacity |
| Warehouse compute | Included in capacity |
| Trial capacity | $0 for 60 days (activate via Power BI) |

Pause the capacity overnight + on weekends to halve cost — same pattern
as `synapse_sql_pool_admin_job`.

## Teardown

```bash
az fabric capacity delete -g dagster-demo-rg -n <capacity-name> --yes
```

## Variations

- **Use the workspace resource** for custom ops that need to do something
  the dedicated components don't cover (e.g., create a new Lakehouse on
  the fly, list workspace permissions)
- **Use the lakehouse resource** in a custom transformation op if you
  need direct Delta read/write without going through the IO manager
- **Pair with `lineage_to_purview`** to publish the full Dagster lineage
  graph (including Fabric assets) into Purview for organization-wide
  data governance
