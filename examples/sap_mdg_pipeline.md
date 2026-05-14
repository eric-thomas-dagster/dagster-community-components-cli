# SAP MDG (Master Data Governance) → Dagster

Pull master data records (customers, suppliers, materials, finance) and **change requests / governance workflow events** from **SAP MDG** into Dagster.

MDG runs **on** S/4HANA / ECC as the governance layer for master data — it's where the "golden record" lives, where change requests flow through approvals, and where mass-changes are orchestrated. For data teams it's the **source of truth** for any reference dimension.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP MDG (on S/4HANA or ECC)                      │
   │   • Active records (golden records)              │
   │   • Inactive change requests + approvals         │
   │   • Mass-change campaigns                        │
   │   • Replication out to subscriber systems        │
   │                                                  │
   │   https://<host>/sap/opu/odata/sap/MDG_*_SRV     │
   └──────────────────────┬───────────────────────────┘
                          │ OData v2 + basic auth
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ odata_ingestion (asset) → DataFrame              │
   │   Active records (or change requests)            │
   └──────────────────────┬───────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ Distribution                                     │
   │   • Snowflake / BQ for analytics                 │
   │   • Trigger downstream pipelines on CR approval  │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `odata_ingestion` | community | OData v2 GET against MDG_* services → pandas DataFrame |
| `dataframe_to_odata` | community | Optional — open change requests by writing CR records |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## defs.yaml — pull active customer records

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: mdg_customers
  service_url: https://my-mdg.acme.com/sap/opu/odata/sap/MDG_BS_BP_API_SRV
  entity_set: BusinessPartner
  odata_version: v2
  auth_type: basic
  auth_username_env_var: MDG_USER
  auth_password_env_var: MDG_PASSWORD
  extra_headers:
    sap-client: "100"
  select: BusinessPartner,BusinessPartnerName,Category,CreatedOnDateTime,LastChangedOnDateTime
  filter: LastChangedOnDateTime ge datetime'{partition_key}T00:00:00'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_mdg
  kinds: [sap, mdg, master-data, odata]
```

## defs.yaml — pull pending change requests

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: mdg_change_requests_open
  service_url: https://my-mdg.acme.com/sap/opu/odata/sap/MDG_CHANGE_REQUEST_API_SRV
  entity_set: ChangeRequest
  odata_version: v2
  auth_type: basic
  auth_username_env_var: MDG_USER
  auth_password_env_var: MDG_PASSWORD
  extra_headers:
    sap-client: "100"
  filter: Status ne 'CLOSED' and CreatedOnDateTime ge datetime'{partition_key}T00:00:00'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_mdg
```

## Common MDG OData services

| Service | What it exposes |
|---|---|
| `MDG_BS_BP_API_SRV` | Business Partners (customers + suppliers + employees) |
| `MDG_BS_MAT_API_SRV` | Material master |
| `MDG_BS_FIN_API_SRV` | Finance master (GL accounts, cost centers, profit centers) |
| `MDG_CHANGE_REQUEST_API_SRV` | Change requests (the workflow layer) |
| `MDG_DATA_REPLICATION_SRV` | Replication framework status |
| `MDG_CONSOLIDATION_SRV` | Mass-change campaigns + data consolidation |

## MDG workflow integration patterns

### Pattern A: Pull approved CRs → trigger downstream

A Change Request gets approved in MDG. You want Dagster to refresh downstream dimensions that depend on that customer/material.

```yaml
# Sensor pattern: poll the CR endpoint for newly-CLOSED status
type: dagster_community_components.SapCPIObservationSensorComponent  # adapt — or write a custom sensor
attributes:
  sensor_name: mdg_cr_approval_sensor
  # Custom approach — adapt the OData polling pattern for CR status changes
```

For now: poll `mdg_change_requests_open` on a schedule; downstream assets depend on it and re-run when new closures appear.

### Pattern B: Mass-extract for an analytics dimension

Daily pull of all "active" Business Partners → load into `dim_customer` in Snowflake. Partition by `LastChangedOnDateTime` for cheap incremental updates.

### Pattern C: Write back to MDG via CR

You've enriched a master record downstream (e.g. ML-derived customer segment) and want to push it back. Open a CR via `dataframe_to_odata` against `MDG_CHANGE_REQUEST_API_SRV`:

```yaml
type: dagster_community_components.DataframeToODataComponent
attributes:
  asset_name: mdg_segment_writeback
  upstream_asset_key: customer_segments
  service_url: https://my-mdg.acme.com/sap/opu/odata/sap/MDG_CHANGE_REQUEST_API_SRV
  entity_set: ChangeRequest
  mode: insert
  csrf_fetch_path: $metadata
  auth_type: basic
  auth_username_env_var: MDG_USER
  auth_password_env_var: MDG_PASSWORD
```

The CR enters MDG's workflow; a steward approves → it propagates to the active master.

## Trade-offs & gotchas

- **MDG is read-mostly from Dagster.** Master data changes via workflow — pushing direct updates is rare. The CR write-back pattern is the right shape.
- **Multi-process MDG.** Many MDG implementations have process-step-specific entity sets — read the customer's own data model, not the generic one.
- **Replication status.** Don't assume MDG's "active" record has propagated to all downstream SAP systems. Check `MDG_DATA_REPLICATION_SRV` for replication completion before downstream extraction.
- **Volume.** MDG-managed dimensions (1M+ customers) need partition pushdown via `$filter`. Full-dimension dumps are slow.
- **PII.** Business Partners contain PII. Tag assets `pii: true` + apply masking before downstream.

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — same OData component
- [`odata_pipeline.md`](odata_pipeline.md) — protocol walkthrough
- [SAP MDG documentation](https://help.sap.com/docs/SAP_MASTER_DATA_GOVERNANCE)
