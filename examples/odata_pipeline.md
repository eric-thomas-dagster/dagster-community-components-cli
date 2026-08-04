# OData → Dagster pipeline blueprint
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Generic OData v2/v4 ingestion → transform → write-back, demonstrated end-to-end against the **public Northwind sample** at services.odata.org. No credentials required.

> **Validation status:** validated end-to-end 2026-05-13. Fresh `uvx create-dagster project`, installed `odata_ingestion`, pointed it at `services.odata.org/V4/Northwind/Northwind.svc/Customers` with `$top=10` and `$select=CustomerID,CompanyName,ContactName,Country,City`. Result: **10 rows × 5 cols read, `RUN_SUCCESS`** — no auth, no setup, ~3 seconds.

This pipeline is the **canonical "does the component work?"** demo. Once you've got it running, swap the `service_url` + `entity_set` + `auth_type` to point at S/4HANA, SuccessFactors, Dynamics 365, MS Graph, Oracle Fusion, Epicor, IFS Cloud, or anything else that speaks OData.

## Why this matters

OData is **the dominant machine-interface protocol across enterprise ERP**. SAP's API Business Hub lists thousands of OData services; Microsoft Dataverse + Graph use OData v4 under the hood; Oracle Fusion / Epicor / IFS all expose OData endpoints. One component covers all of them.

| Vendor | Products with OData |
|---|---|
| **SAP** | S/4HANA Cloud + on-prem, SuccessFactors, Datasphere, Commerce Cloud, Marketing/Service/CDC, Concur (newer endpoints), Ariba (newer endpoints) |
| **Microsoft** | Dynamics 365 / Dataverse (Sales, Customer Service, Field Service, Marketing), Microsoft Graph, SharePoint Online, Business Central, Project Server |
| **Oracle** | Fusion Cloud Applications (Financials, HCM, SCM) |
| **Other ERP** | Epicor ERP (v10+), IFS Cloud, Infor M3 / ION, JD Edwards (newer) |

## Architecture

```
   ┌────────────────────────────────────────────┐
   │ OData service                              │
   │   services.odata.org/V4/Northwind/...      │  ← public test endpoint
   │   (or your S/4HANA / SuccessFactors / ...) │
   └─────────────────┬──────────────────────────┘
                     │ $filter, $select, pagination
                     ▼
   ┌────────────────────────────────────────────┐
   │ odata_ingestion (asset)                    │
   │   GET <entity_set>?$format=json&$top=N     │
   │   → flattened pandas DataFrame             │
   └─────────────────┬──────────────────────────┘
                     │
                     ▼
   ┌────────────────────────────────────────────┐
   │ summarize (asset)                          │
   │   group_by [Country] → count, avg, sum     │
   └─────────────────┬──────────────────────────┘
                     │
                     ▼
   ┌────────────────────────────────────────────┐
   │ dataframe_to_parquet (sink asset)          │
   │   writes curated parquet to                │
   │   ./output/customers_by_country.parquet    │
   └────────────────────────────────────────────┘
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_odata_pipeline_demo.sh | bash
cd odata-pipeline-demo
uv run dg launch --assets northwind_customers
```

You should see 10 rows materialized within ~3 seconds, with `RUN_SUCCESS` at the end.

Open the UI to inspect: `uv run dg dev`, then http://localhost:3000.

## Components used

| Component | Source | Role |
|---|---|---|
| `odata_ingestion` | community | OData v2/v4 GET → pandas DataFrame. Handles pagination, `$filter`, `$select`, etc. |
| `summarize` | community | Per-group aggregations on the DataFrame. |
| `dataframe_to_parquet` | community | Write curated DataFrame to Parquet locally or to S3/GCS/ADLS via fsspec. |

Plus optional companions if you need them:

| Component | When |
|---|---|
| `odata_resource` | Multiple components reading from the SAME OData tenant — register the connection once |
| `dataframe_to_odata` | Reverse direction: push DataFrame rows BACK to an OData entity set (POST / PATCH / DELETE). For SAP write APIs, set `csrf_fetch_path: $metadata` |
| `odata_check` | Asset check: validate the entity set is reachable + has expected columns / row count |
| `oauth_token_resource` | When the OData backend needs OAuth (Datasphere, Dynamics 365 via Azure AD, etc.) |

## defs.yaml — the validated config

```yaml
# defs/northwind_customers/defs.yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: northwind_customers

  service_url: https://services.odata.org/V4/Northwind/Northwind.svc
  entity_set: Customers
  odata_version: v4
  auth_type: none

  top: 10
  select: CustomerID,CompanyName,ContactName,Country,City

  group_name: northwind
  kinds: [odata]
```

```yaml
# defs/customers_by_country/defs.yaml
type: dagster_community_components.SummarizeComponent
attributes:
  asset_name: customers_by_country
  upstream_asset_key: northwind_customers
  group_by: [Country]
  aggregations:
    n_customers: {col: CustomerID, agg: count}
  group_name: northwind
```

```yaml
# defs/customers_by_country_parquet/defs.yaml
type: dagster_community_components.DataframeToParquetComponent
attributes:
  asset_name: customers_by_country_parquet
  upstream_asset_key: customers_by_country
  file_path: "./output/customers_by_country.parquet"
  compression: snappy
  group_name: northwind
```

## Swapping to a real SAP/Dynamics tenant

Change just the connection + auth fields:

### S/4HANA Cloud (basic auth)
```yaml
service_url: https://my300000.s4hana.cloud.sap/sap/opu/odata/sap/API_BUSINESS_PARTNER
entity_set: A_BusinessPartner
odata_version: v2
auth_type: basic
auth_username_env_var: S4HANA_USER
auth_password_env_var: S4HANA_PASSWORD
extra_headers:
  sap-client: "100"
```

### SAP API Business Hub sandbox (free signup)
```yaml
service_url: https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap/API_BUSINESS_PARTNER
entity_set: A_BusinessPartner
odata_version: v2
auth_type: none
extra_headers:
  APIKey: "{{ env('SAP_API_HUB_KEY') }}"
```
Get a free API key at https://api.sap.com → Profile → Show API Key.

### SuccessFactors (basic auth)
```yaml
service_url: https://api4.successfactors.com/odata/v2
entity_set: PerPerson
odata_version: v2
auth_type: basic
auth_username_env_var: SF_USERNAME       # format: APIUser@CompanyID
auth_password_env_var: SF_PASSWORD
```

### Microsoft Dynamics 365 / Dataverse (OAuth via Azure AD)
```yaml
service_url: https://yourorg.crm.dynamics.com/api/data/v9.2
entity_set: accounts
odata_version: v4
auth_type: bearer
auth_token_env_var: DATAVERSE_ACCESS_TOKEN  # from oauth_token_resource
```

### Microsoft Graph (OAuth, workload identity preferred)
```yaml
service_url: https://graph.microsoft.com/v1.0
entity_set: users
odata_version: v4
auth_type: bearer
auth_token_env_var: GRAPH_ACCESS_TOKEN
```

## Partitioning

Partition by date and let the `$filter` template substitute the partition key:

```yaml
filter: CreationDate ge datetime'{partition_key}T00:00:00'
partition_type: daily
partition_start: '2024-01-01'
```

Each daily partition fetches only that day's rows. Re-running a partition pulls the same window again.

## Pagination

The component handles OData v2 (`__next` links) and v4 (`@odata.nextLink`) automatically. For SAP write APIs with strict `$top` limits, set:

```yaml
top: 1000   # request 1000 per page
max_pages: 100  # safety cap — abort after 100 follow-throughs
```

## Trade-offs & gotchas

- **Field names in OData are case-sensitive** (`A_BusinessPartner`, not `a_businesspartner`). Use the same casing as `$metadata`.
- **`$filter` syntax differs slightly between v2 and v4**: v2 uses `datetime'2024-01-01T00:00:00'`, v4 uses `2024-01-01T00:00:00Z`. Check your endpoint's docs.
- **`$expand` flattening**: `pandas.json_normalize` flattens with `_` separator. Nested entities become `EmployeeAddress_City`, etc.
- **CSRF for writes**: read-only ingestion doesn't need CSRF. For `dataframe_to_odata` writes against SAP, set `csrf_fetch_path: $metadata`.

## See also

- [`oauth_pipeline.md`](#) — OAuth-flow demos: Concur, Ariba, Datasphere
- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — production-shape walkthrough with real S/4HANA sandbox
- [`dremio_pipeline.md`](dremio_pipeline.md) — Dremio SQL via REST + Apache Arrow Flight
