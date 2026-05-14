# SAP Datasphere → Dagster pipeline blueprint

Pull data from SAP Datasphere (the rebranded SAP Data Warehouse Cloud) into Dagster via OData consumption APIs.

Datasphere sits between operational SAP systems (S/4HANA, BW/4HANA) and downstream analytics — it federates + models data via Analytic Models. The OData consumption API exposes those models as queryable views.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ SAP Datasphere — Analytic Models                    │
   │   <tenant>.<region>.hcs.cloud.sap                   │
   │   /sap/api/spaces/<space>/analytic-models/<model>/  │
   └─────────────────────────────┬───────────────────────┘
                                 │ OData v4 + Bearer token
                                 │ (XSUAA OAuth client_credentials)
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ oauth_token_resource (XSUAA)                        │
   │   client_id + client_secret → access_token          │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                             │
   │   auth_type: bearer + Bearer from XSUAA token       │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Downstream pipeline (warehouse, BI extract, ML)     │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | XSUAA OAuth2 client_credentials grant |
| `odata_ingestion` | community | OData v4 GET against Datasphere consumption endpoint → pandas DataFrame |
| `odata_check` | community | Optional — health-check the analytic model |
| `dataframe_to_*` | community | Downstream sinks |

## Auth — XSUAA client_credentials

Datasphere uses **SAP BTP's XSUAA** (eXtended Services for User Account and Authentication) for OAuth. Each Datasphere space can have its own OAuth client.

### Step 1: create an OAuth client in Datasphere

1. Datasphere UI → **System** → **Administration** → **App Integration** → **Add OAuth Client**
2. Set redirect URI (any value — we don't use it for client_credentials)
3. Save → copy **Client ID**, **Client Secret**, **Authorization URL**, **Token URL**

The Token URL looks like: `https://<tenant>.authentication.<region>.hana.ondemand.com/oauth/token`

### Step 2: configure the token resource

```yaml
# resources/datasphere_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: datasphere_token
  token_endpoint: https://my-tenant.authentication.eu10.hana.ondemand.com/oauth/token
  grant_type: client_credentials
  client_id_env_var: DATASPHERE_CLIENT_ID
  client_secret_env_var: DATASPHERE_CLIENT_SECRET
  auth_in: basic     # XSUAA expects HTTP Basic
```

## defs.yaml — query an Analytic Model

```yaml
# defs/dsp_sales_summary/defs.yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: dsp_sales_summary
  service_url: https://my-tenant.eu10.hcs.cloud.sap/api/v1/dwc/consumption/relational/MY_SPACE/SALES_SUMMARY
  entity_set: SalesByRegion   # the entity inside the analytic model
  odata_version: v4
  auth_type: bearer
  auth_token_env_var: DATASPHERE_ACCESS_TOKEN
  select: Region,ProductCategory,NetSales,Quantity
  filter: PostingDate ge 2024-01-01T00:00:00Z
  group_name: datasphere
  kinds: [datasphere, odata, sap]
```

> Until `odata_ingestion` natively reads tokens from `oauth_token_resource`, mint the token in a sidecar / pre-run step:
>
> ```bash
> # Pre-run step (e.g., a `before_run` op or a wrapper script):
> export DATASPHERE_ACCESS_TOKEN=$(curl -s -u "$DATASPHERE_CLIENT_ID:$DATASPHERE_CLIENT_SECRET" \
>   -d "grant_type=client_credentials" \
>   https://my-tenant.authentication.eu10.hana.ondemand.com/oauth/token | jq -r .access_token)
> ```

For long-running flows where the 12-hour XSUAA token expires mid-run, use the resource pattern with a custom asset that reads from `context.resources.datasphere_token.get_access_token()`.

## Datasphere URL anatomy

```
https://<tenant>.<region>.hcs.cloud.sap
   /api/v1/dwc/consumption/relational         ← consumption endpoint
   /<space>                                    ← the Datasphere space
   /<analytic_model_or_view>                   ← model name
```

| Region | Subdomain pattern |
|---|---|
| EU10 | `<tenant>.eu10.hcs.cloud.sap` |
| US10 | `<tenant>.us10.hcs.cloud.sap` |
| AP10 | `<tenant>.ap10.hcs.cloud.sap` |
| JP10 | `<tenant>.jp10.hcs.cloud.sap` |

## Spaces, models, views

Datasphere organizes data into **Spaces** (governance boundary, like a schema). Each Space contains:

- **Tables** — raw imported data
- **Views** — modeled SQL views
- **Analytic Models** — semantic layer (measures, dimensions, hierarchies) — the typical Dagster ingestion target

Browse what's exposed: `GET /api/v1/dwc/consumption/relational/<space>/`

## Partitioning by date dimension

```yaml
filter: PostingDate ge {partition_key}T00:00:00Z and PostingDate lt {partition_key_next}T00:00:00Z
partition_type: daily
partition_start: '2024-01-01'
```

Daily partitions hit Datasphere's date-dimension indexes cleanly; full-table scans are slow.

## Asset check — model reachable

```yaml
type: dagster_community_components.ODataCheckComponent
attributes:
  asset_key: datasphere/sales_summary
  service_url: https://my-tenant.eu10.hcs.cloud.sap/api/v1/dwc/consumption/relational/MY_SPACE/SALES_SUMMARY
  entity_set: SalesByRegion
  odata_version: v4
  auth_type: bearer
  auth_token_env_var: DATASPHERE_ACCESS_TOKEN
  min_rows: 1
  expect_columns: [Region, NetSales]
```

## Trade-offs & gotchas

- **Space access scope.** OAuth clients are scoped to a single Space at creation time. To read across multiple Spaces, create multiple OAuth clients (one per space) and multiple `oauth_token_resource` instances.
- **Consumption endpoint vs catalog endpoint.** `/consumption/relational/` is read-only OData. `/catalog/` exposes metadata. Pull from consumption for analytics.
- **Result-set caching.** Datasphere caches analytic-model results for performance. Subsequent identical queries are fast; novel filter combinations are slow on first hit.
- **Spaces with row-level security.** RLS rules apply per-OAuth-client. Test with a least-privilege client to validate visibility before production.
- **Federated sources.** Datasphere can federate live to SAP HANA / S/4HANA / etc. — those queries inherit the latency of the underlying source.

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — read S/4HANA directly via OData
- [`sap_hana_pipeline.md`](sap_hana_pipeline.md) — read HANA tables under Datasphere
- [`odata_pipeline.md`](odata_pipeline.md) — generic OData walkthrough
- [Datasphere consumption API docs](https://help.sap.com/docs/SAP_DATASPHERE)
