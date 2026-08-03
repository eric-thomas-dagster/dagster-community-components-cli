# SAP Integration Suite (CPI) → Dagster pipeline blueprint

**Observe** SAP Integration Suite iFlow runs from Dagster — and optionally **trigger** them. Without Dagster owning the iFlow itself.

SAP Integration Suite (formerly CPI / HCI / Cloud Integration) is the BTP-based middleware that replaces PI/PO. It runs **iFlows** — visual integration pipelines for S/4HANA ↔ SaaS sync, EDI, file-based integration, B2B, etc. Most modern SAP shops run dozens to hundreds of iFlows here.

## Two integration directions

```
   Dagster observes ←──┐                    ┌──→ Dagster triggers
   AssetObservation    │                    │   POST <iflow_endpoint>
   per iFlow run       │                    │
                       │                    │
   ┌───────────────────┴────────────────────┴───────────────────┐
   │                                                            │
   │       SAP Integration Suite — iFlows running on BTP       │
   │                                                            │
   └───────────────────────────┬────────────────────────────────┘
                               │ writes to S/4HANA / SaaS / EDI / files
                               ▼
                       (downstream systems)
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | OAuth client_credentials for Integration Suite |
| `sap_cpi_observation_sensor` | community | Poll MPL → AssetObservations |
| `oauth_rest_ingestion` (optional) | community | POST trigger to an iFlow's HTTPS endpoint |

## Run

### 1. Provision an OAuth client in Integration Suite

1. **Integration Suite UI** → Settings → API Management → **OAuth Clients** → Create
2. Bind to the right subaccount role collection (e.g. `MessagingSend`, `MonitoringDataRead`)
3. Copy `client_id`, `client_secret`, `token_endpoint`, `tmn_host`

### 2. Configure the OAuth token resource

```yaml
# resources/cpi_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: cpi_token
  token_endpoint: https://my-tenant.authentication.eu10.hana.ondemand.com/oauth/token
  grant_type: client_credentials
  client_id_env_var: SAP_CPI_CLIENT_ID
  client_secret_env_var: SAP_CPI_CLIENT_SECRET
  auth_in: basic
```

### 3. Configure the observation sensor

```yaml
# defs/cpi_orders_observer/defs.yaml
type: dagster_community_components.SapCPIObservationSensorComponent
attributes:
  sensor_name: cpi_orders_iflow_observer
  tmn_host: https://my-tenant.it-cpi017.cfapps.us10-002.hana.ondemand.com
  iflow_id: orders_to_s4hana
  asset_key: sap_cpi/orders_to_s4hana
  oauth_token_resource_key: cpi_token
  only_statuses: [COMPLETED, FAILED, RETRY]
  minimum_interval_seconds: 60
  default_status: running
```

### 4. Declare the observed asset

```yaml
# defs/sap_cpi_orders_to_s4hana/defs.yaml
type: dagster_community_components.ExternalAssetComponent  # or another declarative type
attributes:
  asset_key: sap_cpi/orders_to_s4hana
  description: "iFlow on SAP Integration Suite syncing Salesforce Orders → S/4HANA"
  group_name: sap_integration
  kinds: [sap, cpi, integration-suite, iflow]
```

## What the sensor emits

For each new MPL (Message Processing Log) entry, an `AssetObservation` shows up in the Dagster UI on `sap_cpi/orders_to_s4hana` with this metadata:

- `sap_cpi.message_guid` — unique ID (also the sensor's cursor)
- `sap_cpi.status` — COMPLETED / FAILED / RETRY
- `sap_cpi.iflow` — IntegrationFlow ID
- `sap_cpi.log_start` / `sap_cpi.log_end` — timestamps
- `sap_cpi.correlation_id` — cross-system trace ID
- `sap_cpi.application_message_id` — sender-provided message ID

In the Dagster UI: open the asset → Observations tab → see the full iFlow run history with status timelines.

## Triggering iFlows from Dagster

iFlows are HTTPS endpoints — POST to trigger:

```yaml
# defs/trigger_orders_iflow/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent  # repurposed for trigger
attributes:
  asset_name: trigger_orders_iflow
  api_url: https://my-tenant.it-cpi017.cfapps.us10-002.hana.ondemand.com/http/orders-trigger
  oauth_token_resource_key: cpi_token
  pagination: none
```

For more control (custom body, error handling) write a custom asset that uses the same `oauth_token_resource`:

```python
@asset(required_resource_keys={"cpi_token"})
def trigger_orders_iflow(context):
    import requests
    h = {"Authorization": context.resources.cpi_token.get_authorization_header()}
    r = requests.post(
        "https://my-tenant.it-cpi017.cfapps.us10-002.hana.ondemand.com/http/orders-trigger",
        headers=h,
        json={"trigger_id": context.run_id},
    )
    r.raise_for_status()
    return r.json()
```

## Wiring trigger + observation together

The mature pattern: **Dagster triggers the iFlow, then waits for completion via the observation sensor.**

```
   ┌─────────────────────────────────────────────┐
   │ Upstream asset → trigger_orders_iflow       │
   │   POSTs to iFlow's HTTPS endpoint           │
   │   captures `correlation_id` in run tags     │
   └─────────────────────────┬───────────────────┘
                             │
                             ▼  (sensor waits + emits observation)
   ┌─────────────────────────────────────────────┐
   │ sap_cpi_observation_sensor                  │
   │   filters MPLs by ApplicationMessageId      │
   │   = the captured correlation_id             │
   │   emits AssetObservation on completion      │
   └─────────────────────────┬───────────────────┘
                             │
                             ▼
   ┌─────────────────────────────────────────────┐
   │ Downstream asset depends on the observed    │
   │ asset — fires after observation             │
   └─────────────────────────────────────────────┘
```

Pair the components: trigger sets a `correlation_id` (run tag), sensor filters by it (configure `only_statuses` + add MPL-side filter), downstream fires on completion.

## Common iFlow patterns customers run

| Pattern | What it does |
|---|---|
| **Salesforce → S/4HANA orders** | Sync Salesforce Opportunity → S/4 Sales Order |
| **S/4HANA → SuccessFactors employee** | Sync employee data both directions |
| **Bank → S/4HANA payment file** | EDI / pain.001 / camt.053 file processing |
| **S/4HANA → external warehouse (3PL)** | Outbound delivery messages |
| **API trigger → SOAP call to legacy system** | Bridge between modern apps and older systems |
| **Concur / Ariba → S/4 invoices** | Vendor invoice automation |

## Trade-offs & gotchas

- **MPL retention.** Integration Suite retains MPLs for 30/60/90 days depending on plan. For long-term audit, archive observations downstream (e.g. write to a BigQuery / Snowflake audit table).
- **Cursor accuracy.** The sensor uses `MessageGuid` as a string cursor — MPL GUIDs are timestamp-based so this works correctly. If you ever reset the sensor cursor, you'll re-observe history.
- **Polling latency.** Default `minimum_interval_seconds: 60`. For high-volume iFlows (~1000 runs/min), bump `batch_size: 100` and consider parallel sensors filtered by `iflow_id`.
- **OAuth scope.** Different MPL endpoints require different scopes: `MonitoringDataRead` for MPL OData; `IntegrationFlowProcess` for trigger; `MessagingSend` for HTTP-receiver iFlows. Set up scope-specific OAuth clients.
- **TMN host vs Runtime URL.** TMN URL is the management endpoint (used by this sensor). iFlow runtime endpoints are at `<tenant>-iflmap.hcisbt.<region>.hana.ondemand.com` — different hostname.

## See also

- `oauth_token_resource`
- [`sap_event_mesh_pipeline.md`](sap_event_mesh_pipeline.md) — companion for event-driven SAP integration
- [`airflow_dag_observation_sensor` walkthrough](https://dagster-component-ui.vercel.app/) — same pattern, Airflow side
- [SAP Integration Suite docs](https://help.sap.com/docs/integration-suite)
- [MPL OData API reference](https://api.sap.com/api/IntegrationContent/overview)
