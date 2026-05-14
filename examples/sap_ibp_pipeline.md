# SAP IBP (Integrated Business Planning) → Dagster

Pull supply-chain planning data from **SAP IBP** (Integrated Business Planning) into Dagster via OData. IBP is SAP's cloud successor to APO — demand planning, supply planning, S&OP, inventory optimization, response planning.

Same `odata_ingestion` component as the rest of the SAP family — IBP exposes OData v2 endpoints on top of its planning models.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP IBP — planning models, key figures           │
   │   https://<tenant>.scmibp.ondemand.com           │
   │   /sap/opu/odata/IBP/EXTRACT_ODATA_SRV/...       │
   └──────────────────────┬───────────────────────────┘
                          │ OData v2 + basic auth
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                          │
   │   GET key-figure values, master data             │
   │   → pandas DataFrame                             │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `odata_ingestion` | community | OData v2 GET against IBP EXTRACT_ODATA_SRV → pandas DataFrame |
| `dataframe_to_odata` | community | Optional — push actuals INTO IBP (reverse direction) |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## defs.yaml — pull a key figure

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: ibp_forecast_volumes
  service_url: https://my-tenant.scmibp.ondemand.com/sap/opu/odata/IBP/EXTRACT_ODATA_SRV
  entity_set: zXYZ_FCSTQTY    # your custom extracted view, project-specific
  odata_version: v2
  auth_type: basic
  auth_username_env_var: IBP_USER
  auth_password_env_var: IBP_PASSWORD
  select: PRODUCT,LOCATION,WEEK_START,FORECAST_QTY,UNIT
  filter: WEEK_START ge datetime'{partition_key}T00:00:00'
  partition_type: weekly
  partition_start: '2024-01-01'
  group_name: sap_ibp
  kinds: [sap, ibp, odata, planning]
```

## IBP-specific OData services

IBP extracts data via **EXTRACT_ODATA_SRV** (the standard SAP-provided service) — but the entity sets you read are **project-specific**. They're "Output Z-views" defined by your IBP admin via Excel-on-IBP planning views:

1. IBP UI → Excel client → **Manage Output Views** → create a new view
2. Select the planning area, time periods, key figures, attributes
3. The view becomes an OData entity (named with the `z*` prefix typically)
4. Use that entity name in `entity_set:`

For master data:
- `MasterData_*` entities — products, locations, customers, calendars
- `Configuration_*` entities — planning areas, units of measure, attributes

## Common IBP integration patterns

| Pattern | Approach |
|---|---|
| **Pull forecast** | Dagster reads daily/weekly forecast key figures → push to BigQuery / Snowflake for downstream ML |
| **Send actuals** | Dagster pushes sales/inventory actuals into IBP via OData write (`dataframe_to_odata` with `mode: insert`) |
| **Trigger IBP planning job** | Dagster triggers an IBP background job via REST API (custom asset) |

## Writing to IBP

For pushing data INTO IBP (sales actuals, inventory snapshots, override forecasts):

```yaml
type: dagster_community_components.DataframeToODataComponent
attributes:
  asset_name: ibp_sales_actuals_loaded
  upstream_asset_key: weekly_sales_actuals
  service_url: https://my-tenant.scmibp.ondemand.com/sap/opu/odata/IBP/EXTRACT_ODATA_SRV
  entity_set: zSALES_ACTUALS_IN
  mode: insert
  csrf_fetch_path: $metadata    # IBP requires CSRF for writes
  auth_type: basic
  auth_username_env_var: IBP_USER
  auth_password_env_var: IBP_PASSWORD
```

## Trade-offs & gotchas

- **Output views are slow.** IBP renders OData by querying planning areas live. Large queries (>50K records) can take minutes. Aggregate to a coarser time grain where possible.
- **Auth: communication users only.** IBP doesn't accept regular planning users via OData. Set up dedicated Communication Arrangements with the `IBP_Communication_Inbound` scenario.
- **Time formats.** IBP returns dates as `datetime'2024-W12'` (ISO week) for weekly buckets — parse downstream.
- **Master data drift.** Product / location dimensions change. Use `IBP_MasterData_*` entities for the current dimension snapshot — join downstream rather than caching.

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — same OData component, different SAP product
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
- [SAP IBP API documentation](https://help.sap.com/docs/SAP_INTEGRATED_BUSINESS_PLANNING)
