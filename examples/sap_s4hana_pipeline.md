# SAP S/4HANA → Dagster pipeline blueprint

Pull data from SAP S/4HANA Cloud or on-premise into Dagster via the **OData APIs** that SAP exposes on every modern S/4 deployment. Three auth flavors (sandbox, basic, OAuth) — the rest of the pipeline is identical.

## Testable today: SAP API Business Hub sandbox

SAP runs an actual S/4HANA sandbox at **`sandbox.api.sap.com`** with real sample data. **Free signup, real SAP semantics.**

1. Go to **https://api.sap.com** → sign up (free SAP account)
2. Pick "SAP S/4HANA Cloud" → "Business Partner (A2X)" → "Try Out"
3. Top-right user menu → **Show API Key** — copy it
4. `export SAP_API_HUB_KEY=<your-key>`

Walkthrough config below works against this endpoint with no other setup.

## Architecture

```
   ┌────────────────────────────────────────────────────┐
   │ SAP S/4HANA                                        │
   │   • api.sap.com sandbox (free, sample data)        │
   │   • Your tenant: my300000.s4hana.cloud.sap         │
   │   • On-prem: https://s4-prod.acme.com:8000/sap/... │
   └─────────────────────────┬──────────────────────────┘
                             │ OData v2 over HTTPS
                             ▼
   ┌────────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                            │
   │   GET /sap/opu/odata/sap/API_BUSINESS_PARTNER/...  │
   │   Paginated; $filter / $select / $expand           │
   │   → pandas DataFrame                               │
   └─────────────────────────┬──────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────┐
   │ summarize (transform asset)                        │
   │   Per-country business-partner counts              │
   └─────────────────────────┬──────────────────────────┘
                             │
                             ▼
   ┌────────────────────────────────────────────────────┐
   │ dataframe_to_snowflake / parquet / table (sink)    │
   │   Persist to your downstream warehouse             │
   └────────────────────────────────────────────────────┘

   Optional reverse direction:
   ┌─────────────────────────────────────────────────────┐
   │ dataframe_to_odata (sink)                           │
   │   POST / PATCH / DELETE rows BACK to S/4HANA        │
   │   (set csrf_fetch_path: $metadata for SAP CSRF)     │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| [`odata_ingestion`](https://dagster-component-ui.vercel.app/c/odata_ingestion) | community | Read from any S/4HANA OData service |
| [`odata_resource`](https://dagster-component-ui.vercel.app/c/odata_resource) | community | Optional — register the connection once if you read from multiple entity sets |
| [`odata_check`](https://dagster-component-ui.vercel.app/c/odata_check) | community | Asset check — tenant reachability + row count + expected columns |
| [`dataframe_to_odata`](https://dagster-component-ui.vercel.app/c/dataframe_to_odata) | community | Reverse direction: write back to S/4HANA (with CSRF handling) |
| `summarize`, `dataframe_to_*` | community | Standard transform + sink |

## defs.yaml — three connection modes

### Mode A: SAP API Business Hub sandbox (testable today)

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: business_partners
  service_url: https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_BUSINESS_PARTNER
  entity_set: A_BusinessPartner
  odata_version: v2
  auth_type: none
  extra_headers:
    APIKey: "{{ env('SAP_API_HUB_KEY') }}"
  select: BusinessPartner,BusinessPartnerName,Country,CreationDate
  top: 50
  group_name: sap_s4hana
  kinds: [s4hana, odata, sap]
```

> The sandbox uses an `APIKey` HEADER (not basic auth) — SAP's API Hub convention.

### Mode B: S/4HANA Cloud — basic auth (Communication User)

S/4HANA Cloud customers expose APIs via **Communication Arrangements**. Each communication user has a username + password tied to specific scenarios (Business Partner Replication, Cost Center, etc.).

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: business_partners
  service_url: https://my300000.s4hana.cloud.sap/sap/opu/odata/sap/API_BUSINESS_PARTNER
  entity_set: A_BusinessPartner
  odata_version: v2
  auth_type: basic
  auth_username_env_var: S4HANA_COMM_USER     # e.g. 'CC0001_RFC'
  auth_password_env_var: S4HANA_COMM_PASSWORD
  extra_headers:
    sap-client: "100"
  select: BusinessPartner,BusinessPartnerName,Country,CreationDate
  filter: CreationDate ge datetime'{partition_key}T00:00:00'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_s4hana
  kinds: [s4hana, odata, sap]
```

### Mode C: S/4HANA Cloud — OAuth 2.0 (recommended for production)

For tenants set up with OAuth-based Communication Arrangements (newer ICF setup):

```yaml
# resources/sap_s4_oauth.yaml — provides the access-token lifecycle
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: sap_s4_token
  token_endpoint: https://my300000.authentication.eu10.hana.ondemand.com/oauth/token
  grant_type: client_credentials
  client_id_env_var: SAP_S4_CLIENT_ID
  client_secret_env_var: SAP_S4_CLIENT_SECRET
  auth_in: basic    # SAP UAA expects HTTP Basic with client_id:client_secret
```

```yaml
# In your asset's defs.yaml — reference the resource via a Python wrapper, OR
# use the simpler `bearer` mode with auth_token_env_var (mint the token in a
# sidecar process / pre-job step):
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: business_partners
  service_url: https://my300000.s4hana.cloud.sap/sap/opu/odata/sap/API_BUSINESS_PARTNER
  entity_set: A_BusinessPartner
  odata_version: v2
  auth_type: bearer
  auth_token_env_var: SAP_S4_ACCESS_TOKEN
  extra_headers:
    sap-client: "100"
```

## Common S/4HANA OData services

| Service | What it exposes | Doc |
|---|---|---|
| `API_BUSINESS_PARTNER` | Customers + suppliers + employees | [api.sap.com link](https://api.sap.com/api/API_BUSINESS_PARTNER) |
| `API_SALES_ORDER_SRV` | Sales orders + items | API Hub |
| `API_PURCHASEORDER_PROCESS_SRV` | Purchase orders | API Hub |
| `API_PRODUCT_SRV` | Materials / products | API Hub |
| `API_BILLING_DOCUMENT_SRV` | Invoices | API Hub |
| `API_GLACCOUNTINCHARTOFACCOUNTS_SRV` | GL accounts | API Hub |
| `API_COSTCENTER_SRV` | Cost centers | API Hub |
| `API_PRODUCT_CATEGORY_SRV` | Product hierarchy | API Hub |
| `API_PHYSICAL_INVENTORY_DOC_SRV` | Inventory | API Hub |
| `API_RAW_ON_ACCOUNT_PAYMENTS_SRV` | Payments | API Hub |

For your tenant's full list: **SAP Cloud Communication Management → Communication Scenarios → API Reference**.

## Partitioning by `CreationDate` / `LastChangeDate`

OData `$filter` supports SAP datetime literals (v2):

```yaml
filter: CreationDate ge datetime'{partition_key}T00:00:00' and CreationDate lt datetime'{partition_key_next}T00:00:00'
partition_type: daily
partition_start: '2024-01-01'
```

For v4 endpoints (newer S/4HANA APIs), use ISO format without the `datetime` prefix:

```yaml
filter: CreationDate ge '{partition_key}T00:00:00Z' and CreationDate lt '{partition_key_next}T00:00:00Z'
```

## Writing back to S/4HANA (`dataframe_to_odata`)

For Master Data sync or replication scenarios where Dagster pushes records BACK to S/4HANA:

```yaml
type: dagster_community_components.DataframeToODataComponent
attributes:
  asset_name: business_partners_posted
  upstream_asset_key: new_business_partners
  service_url: https://my300000.s4hana.cloud.sap/sap/opu/odata/sap/API_BUSINESS_PARTNER
  entity_set: A_BusinessPartner
  mode: insert
  # SAP write APIs require x-csrf-token — auto-fetched from $metadata:
  csrf_fetch_path: $metadata
  auth_type: basic
  auth_username_env_var: S4HANA_COMM_USER
  auth_password_env_var: S4HANA_COMM_PASSWORD
  extra_headers:
    sap-client: "100"
```

Modes: `insert` (POST), `upsert` (PATCH with `key_column`), `delete` (DELETE with `key_column`).

## Asset check — tenant reachability

```yaml
# asset_checks/sap_s4_reachable/defs.yaml
type: dagster_community_components.ODataCheckComponent
attributes:
  asset_key: sap_s4hana/business_partners
  service_url: https://my300000.s4hana.cloud.sap/sap/opu/odata/sap/API_BUSINESS_PARTNER
  entity_set: A_BusinessPartner
  odata_version: v2
  auth_type: basic
  auth_username_env_var: S4HANA_COMM_USER
  auth_password_env_var: S4HANA_COMM_PASSWORD
  extra_headers:
    sap-client: "100"
  min_rows: 1
  expect_columns: [BusinessPartner, BusinessPartnerName]
  severity: ERROR
```

Run alongside the ingestion asset. If S/4HANA is unreachable or the entity set changed shape, the check fails before downstream pipelines waste effort.

## Trade-offs & gotchas

- **`sap-client` header is sticky.** Set the right client number (`100` for prod, `200` for QA, etc.) — wrong client returns "no authorization" not "wrong tenant", which is confusing.
- **CSRF only for writes.** Read-only ingestion never needs CSRF; the `csrf_fetch_path` field on `odata_ingestion` exists for completeness but doesn't do anything on GETs.
- **Basic auth on Cloud is for Communication Users only**, not regular SAP users. Set up Communication Arrangements via Fiori Launchpad: `Maintain Communication Users` → `Maintain Communication Systems` → `Communication Arrangements`.
- **`$batch` for writes is faster but not universal.** S/4HANA supports it; some Cloud edges don't. Default `dataframe_to_odata` uses per-row POSTs — turn on `batch_mode: true` only after benchmarking.
- **OAuth tokens from XSUAA last ~12 hours.** For long-running runs, the `oauth_token_resource` auto-refreshes — no extra config needed.
- **Bandwidth.** Large entity sets with `$expand` can run into hundreds of MBs. Use `$select` aggressively and partition on date columns.

## See also

- [`odata_pipeline.md`](odata_pipeline.md) — generic OData walkthrough (validated against public Northwind)
- [`sap_successfactors_pipeline.md`](sap_successfactors_pipeline.md) — SuccessFactors HRIS data
- [`sap_concur_pipeline.md`](sap_concur_pipeline.md) — Concur expense reports (OAuth refresh-token)
- [`sap_ariba_pipeline.md`](sap_ariba_pipeline.md) — Ariba procurement (OAuth client_credentials)
- [`sap_datasphere_pipeline.md`](sap_datasphere_pipeline.md) — SAP Datasphere analytics
- [`sap_hana_pipeline.md`](sap_hana_pipeline.md) — direct HANA SQL (under S/4HANA when you need raw tables vs OData)
