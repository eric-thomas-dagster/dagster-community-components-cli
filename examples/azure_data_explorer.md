# Azure Data Explorer (Kusto)

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

KQL query against an ADX cluster → DataFrame. Distinct from
`azure_log_analytics_query` (different SDK + service): ADX is the
**raw Kusto cluster** you provision yourself for high-volume telemetry,
security analytics, IoT log analysis. Log Analytics is the managed
service built on top.

## Components used

| Component | Category | Role |
|---|---|---|
| `dataframe_from_kusto` | source | Run KQL → DataFrame |
| `dataframe_to_kusto` | sink | DataFrame → Kusto table (queued or streaming ingest) |

## Status

Code-validated against the `azure-kusto-data` SDK spec. To run
end-to-end you need an ADX cluster (the smallest dev cluster is
~$3/hour; there's a free Kusto Cluster tier for prototyping at
[dataexplorer.azure.com/freecluster](https://dataexplorer.azure.com/freecluster)).

## defs.yaml

```yaml
# Read
type: dagster_component_templates.DataframeFromKustoComponent
attributes:
  asset_name: ioc_alerts
  cluster_url: https://mycluster.eastus.kusto.windows.net
  database: SecurityAnalytics
  query: |
    Alerts
    | where Severity == "High"
    | summarize n = count() by bin(TimeGenerated, 5m), AlertName
  tenant_id_env_var: AZURE_TENANT_ID
  client_id_env_var: AZURE_CLIENT_ID
  client_secret_env_var: AZURE_CLIENT_SECRET

# Write
type: dagster_component_templates.DataframeToKustoComponent
attributes:
  asset_name: events_in_kusto
  upstream_asset_key: events_raw
  cluster_url: https://mycluster.eastus.kusto.windows.net
  database: SecurityAnalytics
  table: NormalizedEvents
  use_streaming_ingestion: false
  if_exists: append
```

## When to choose ADX over Log Analytics

| Use case | ADX | Log Analytics |
|---|---|---|
| Bring-your-own data ingestion at high volume | ✓ | partial (limits) |
| Custom retention policies | ✓ | tier-bound |
| Compute-intensive KQL (cross-table joins, ML, time-series functions) | ✓ | ✓ |
| Out-of-the-box for Sentinel / AppInsights | needs setup | ✓ (managed) |
| Cost at scale | $$ (cluster) | $/GB ingestion |

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
