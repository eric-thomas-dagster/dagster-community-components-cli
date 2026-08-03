# SAP Commerce Cloud (Hybris) → Dagster

Pull e-commerce data from **SAP Commerce Cloud** (formerly Hybris) into Dagster via the **OCC (Omni Commerce Connect)** API. Orders, products, customers, carts — all expose OData/REST endpoints.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP Commerce Cloud (Hybris) — storefront         │
   │   https://api.<env>.<tenant>.commerce.ondemand.sap│
   │   /occ/v2/<basesite>/...                         │
   └──────────────────────┬───────────────────────────┘
                          │ REST + OAuth 2.0
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ oauth_rest_ingestion (asset)                     │
   │   → pandas DataFrame                             │
   └──────────────────────────────────────────────────┘
```

OCC speaks JSON REST, not pure OData — pagination is via `currentPage` / `pageSize` / `totalPages`. Use `oauth_rest_ingestion` with `pagination: page`.

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | OAuth2 client_credentials grant for OCC |
| `oauth_rest_ingestion` | community | Paginated REST GET (page-based) → pandas DataFrame |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Run

### 1. Create an OAuth client in Commerce Cloud

1. SAP Commerce Cloud Backoffice → **Customer Support** (or similar admin) → **OAuth Clients**
2. Create a client with scopes for the OCC APIs you need
3. Copy `client_id`, `client_secret`, `token_endpoint`

### 2. Configure the token resource

```yaml
# resources/cc_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: cc_token
  token_endpoint: https://api.<env>.<tenant>.commerce.ondemand.sap/authorizationserver/oauth/token
  grant_type: client_credentials
  client_id_env_var: SAP_CC_CLIENT_ID
  client_secret_env_var: SAP_CC_CLIENT_SECRET
  auth_in: form
```

### 3. Configure the ingestion

```yaml
# defs/cc_orders/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: cc_orders
  api_url: https://api.<env>.<tenant>.commerce.ondemand.sap/occ/v2/electronics/orders
  oauth_token_resource_key: cc_token
  pagination: page
  page_param: currentPage
  page_start: 0
  records_path: orders
  query_params:
    pageSize: "100"
    fields: FULL
    statuses: COMPLETED
    user: "current"
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_commerce
  kinds: [sap, commerce-cloud, hybris, rest]
```

## OCC API endpoints worth ingesting

Replace `electronics` with your basesite ID.

| Endpoint | What it returns |
|---|---|
| `/occ/v2/<basesite>/orders` | Orders (filter by date, status, user) |
| `/occ/v2/<basesite>/products` | Product catalog |
| `/occ/v2/<basesite>/products/search?query=<q>` | Faceted product search |
| `/occ/v2/<basesite>/users/<id>/orders` | Per-user order history |
| `/occ/v2/<basesite>/customergroups` | Customer segments |
| `/occ/v2/<basesite>/carts` | Active carts |
| `/occ/v2/<basesite>/promotions` | Promotions |
| `/occ/v2/<basesite>/stores` | Store locations |
| `/occ/v2/<basesite>/catalogs` | Product catalogs |

## Pagination

OCC uses zero-indexed `currentPage` + `pageSize`. Response includes:
```json
{
  "orders": [...],
  "pagination": {
    "currentPage": 0,
    "pageSize": 100,
    "totalPages": 47,
    "totalResults": 4621
  }
}
```

The component's `pagination: page` mode increments `currentPage` until results are empty. Configure `page_start: 0` (OCC is zero-indexed) and `page_param: currentPage`.

## Date filtering

OCC orders accept `placedAfter` / `placedBefore` parameters:

```yaml
query_params:
  placedAfter: "{partition_key}T00:00:00+0000"
  pageSize: "100"
```

## Trade-offs & gotchas

- **OCC vs OData.** Hybris also exposes some legacy OData services — most modern customers use OCC REST. Choose based on what your basesite exposes.
- **Anonymous vs user-scoped.** Some endpoints (orders, carts) need user-scoped tokens. `client_credentials` only works for catalog/product/store-level data. For per-user data, use a service-account OAuth flow with impersonation.
- **Pagination caps.** OCC caps `pageSize` at 100. For 10K+ records, you'll page heavily — consider Hybris **Solr export feeds** for bulk extraction instead.
- **Multi-basesite.** Each storefront is a separate basesite ID. To pull across all storefronts, you'll need multiple ingestion assets (or a custom asset that loops).
- **`fields: FULL`.** Returns expanded relationships (line items, customer, addresses). Without it you get summary projections — usually too thin for analytics.

## See also

- [`odata_pipeline.md`](odata_pipeline.md) — generic OData walkthrough
- `oauth_token_resource`
- [SAP Commerce Cloud OCC docs](https://help.sap.com/docs/SAP_COMMERCE)
