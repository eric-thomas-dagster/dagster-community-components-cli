# Dagster+ → Microsoft Sentinel demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

Pull Dagster+ audit-log entries via GraphQL, normalize to OCSF v1.1, ship to a
Microsoft Sentinel workspace via the Log Analytics ingestion API. Events land
in a Custom Logs table (default: `DagsterPlusAudit_CL`) queryable from
Sentinel's KQL workbench.

```
dagster_plus_audit_log_ingestion → ocsf_normalizer → audit_logs_to_sentinel
                                                              │
                                                              └─→ Log Analytics workspace → Sentinel
```

## Prerequisites

| Need | How to get it |
|---|---|
| Dagster+ deployment + user token | Settings → Tokens → User Tokens |
| Azure subscription | Pay-As-You-Go or higher |
| Azure CLI signed in | `az login` |
| `Microsoft.OperationalInsights` provider | One-time: `az provider register --namespace Microsoft.OperationalInsights --wait` |
| Log Analytics workspace | See "Provisioning" below |
| Sentinel solution on the workspace (optional) | If you want incident/hunt UX. Custom log ingestion works without it. |

## Required env vars

```bash
export DAGSTER_PLUS_USER_TOKEN=<your-token>
export DAGSTER_PLUS_ENDPOINT_URL=https://<org>.dagster.cloud/<deployment>/graphql
                                # or <org>.eu.dagster.cloud for EU
export SENTINEL_WORKSPACE_ID=<workspace-customer-id-guid>
export SENTINEL_WORKSPACE_KEY=<primary-or-secondary-shared-key>
```

## Provisioning the workspace (one-time)

```bash
RG=dagster-demo-rg
LAW=dagster-demo-law

az group create --name "$RG" --location eastus
az monitor log-analytics workspace create \
    -g "$RG" -n "$LAW" -l eastus --sku PerGB2018

export SENTINEL_WORKSPACE_ID=$(az monitor log-analytics workspace show \
    -g "$RG" -n "$LAW" --query customerId -o tsv)
export SENTINEL_WORKSPACE_KEY=$(az monitor log-analytics workspace get-shared-keys \
    -g "$RG" -n "$LAW" --query primarySharedKey -o tsv)
```

To turn the workspace into a Sentinel-enabled one (optional, free):

```bash
az sentinel workspace create -g "$RG" -n "$LAW"   # may require sentinel CLI extension
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `dagster_plus_audit_log_ingestion` | ingestion | GraphQL pull from `auditLog.auditLogEntries` |
| 2 | `ocsf_normalizer` | transformation | Map Dagster+ event types → OCSF v1.1 |
| 3 | `audit_logs_to_sentinel` | sink | POST to Log Analytics ingestion API |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dagster_plus_to_sentinel_demo.sh | bash
cd dagster-plus-sentinel-demo
uv run dg launch --assets '*'
```

## Verify (after ~2-10min ingestion latency)

```bash
az monitor log-analytics query -w "$SENTINEL_WORKSPACE_ID" \
    --analytics-query 'DagsterPlusAudit_CL | count' -o table
```

Validated end-to-end against real production data:

| Source events | Landed in Sentinel | OCSF class_uid distribution |
|---|---|---|
| 176 audit-log entries (last 365d) | 176 ✓ | 6002 (App Lifecycle) ×176 |

KQL queries to try:

```kql
DagsterPlusAudit_CL
| summarize count() by raw_event_type_s
| render piechart

DagsterPlusAudit_CL
| where raw_event_type_s in ('CREATE_USER_TOKEN', 'REVOKE_USER_TOKEN', 'CHANGE_USER_PERMISSIONS')
| project TimeGenerated, raw_event_type_s, ['actor.user.email_addr_s']
| order by TimeGenerated desc

DagsterPlusAudit_CL
| where ['class_uid_d'] == 3002   // OCSF Authentication
| summarize count() by bin(TimeGenerated, 1h)
| render timechart
```

## Cost

Log Analytics: PerGB2018 SKU. **First 5GB/month free.** This demo's 176
records are <100KB. Effectively $0.

## Teardown

```bash
az group delete --name dagster-demo-rg --yes
```

## What this isn't

- **Not a Sentinel-incidents demo.** Custom logs ingest fine without Sentinel
  enabled on the workspace; the incidents/hunts UX is its own setup.
- **Not a real-time stream.** This is a polling pull on `dg launch`. For
  every-15-min cadence, install `cron_schedule` and point it at this asset
  graph, or use the `dagster_plus_to_siem_job` op-job (single-YAML version).

## See also

<!-- TODO: link related walkthroughs -->
