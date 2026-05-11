# Azure Log Analytics KQL Query

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

Run a KQL query against an Azure Log Analytics workspace and
materialize the result as a DataFrame asset. Counterpart to
[`audit_logs_to_sentinel`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sinks/audit_logs_to_sentinel) (write side) — write logs first, then query
them back.

## Components used

| Component | Category | Role |
|---|---|---|
| [`azure_log_analytics_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/azure_log_analytics_query) | source | KQL query → DataFrame |

## Status

Component is **code-validated** against the `azure-monitor-query` SDK
spec. To run end-to-end, you need a Log Analytics workspace (or a
Sentinel workspace which is built on Log Analytics).

## Setup

```bash
# Create workspace if you don't have one
RG=dagster-demo-rg
WS=dgloganal$(openssl rand -hex 3)
az group create -n "$RG" -l eastus 2>/dev/null || true
az monitor log-analytics workspace create -g "$RG" -n "$WS" -l eastus
WORKSPACE_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$WS" --query customerId -o tsv)

# Grant the principal Log Analytics Reader
ME=$(az ad signed-in-user show --query id -o tsv)
SUB=$(az account show --query id -o tsv)
az role assignment create --assignee "$ME" \
    --role "Log Analytics Reader" \
    --scope "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.OperationalInsights/workspaces/$WS"
```

## defs.yaml

```yaml
type: dagster_component_templates.AzureLogAnalyticsQueryComponent
attributes:
  asset_name: signin_failures_last_hour
  workspace_id: "<WORKSPACE_ID>"
  timespan_iso8601: "PT1H"        # last hour
  query: |
    SigninLogs
    | where ResultType != 0
    | summarize failure_count = count() by bin(TimeGenerated, 5m), UserPrincipalName
    | order by failure_count desc
    | take 100
  tenant_id_env_var: AZURE_TENANT_ID
  client_id_env_var: AZURE_CLIENT_ID
  client_secret_env_var: AZURE_CLIENT_SECRET
```

## KQL examples

```kql
// Last 24h Azure activity, top 100
AzureActivity
| where TimeGenerated > ago(24h)
| summarize n = count() by Caller, ResourceProvider
| order by n desc | take 100

// Sentinel incidents
SecurityIncident
| where TimeGenerated > ago(7d)
| project Title, Severity, Status, CreatedTime

// Custom Dagster+ audit logs (after running audit_logs_to_sentinel)
DagsterPlusAudit_CL
| where TimeGenerated > ago(1d)
| summarize n = count() by event_type_s
```
