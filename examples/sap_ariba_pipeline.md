# SAP Ariba → Dagster pipeline blueprint

Pull purchase requisitions, suppliers, contracts, and sourcing events from SAP Ariba via OAuth 2.0 **client_credentials** flow. Easier headless story than Concur — no refresh-token rotation to manage.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ SAP Ariba (Procurement / Sourcing / Supplier Mgmt)  │
   │   api.ariba.com/api/* (region-specific subdomains)  │
   └─────────────────────────────┬───────────────────────┘
                                 │ OAuth 2.0 (client_credentials)
                                 │ + Bearer access_token (auto-refreshed)
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ oauth_token_resource (resource)                     │
   │   client_id + client_secret → access_token          │
   │   No rotation needed — pure M2M                     │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ oauth_rest_ingestion (asset)                        │
   │   GET /reporting/view/.../RequisitionList           │
   │   Pagination: cursor (pageToken response field)     │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Downstream transforms + sinks (Snowflake, BQ, ...)  │
   └─────────────────────────────────────────────────────┘
```

## Headless OAuth — client_credentials (the easy case)

Ariba uses **pure machine-to-machine OAuth**: client_id + client_secret → access_token. No user identity, no refresh tokens, no rotation.

### Step 1: register the app in Ariba Developer Portal

1. Go to **https://developer.ariba.com** → log in with your Ariba account
2. **Applications** → **Create Application** → name + description
3. Once approved by your admin, you receive **OAuth Client ID** + **OAuth Secret**
4. Store both in your secret manager:

```bash
aws secretsmanager create-secret --name ariba/client_id --secret-string '<id>'
aws secretsmanager create-secret --name ariba/client_secret --secret-string '<secret>'
```

### Step 2: configure the token resource

```yaml
# resources/ariba_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: ariba_token
  token_endpoint: https://api.ariba.com/v2/oauth/token
  grant_type: client_credentials
  client_id_env_var: ARIBA_CLIENT_ID
  client_secret_env_var: ARIBA_CLIENT_SECRET
  auth_in: basic    # Ariba expects HTTP Basic with client_id:client_secret
  scope: "OperationalReporting"   # scope per API family
```

That's it. No writeback callback, no bootstrapping. The resource auto-mints tokens (~1hr lifetime) and refreshes when they expire.

### Step 3: load creds at startup

```bash
# k8s init container / systemd ExecStartPre:
export ARIBA_CLIENT_ID=$(aws secretsmanager get-secret-value --secret-id ariba/client_id --query SecretString --output text)
export ARIBA_CLIENT_SECRET=$(aws secretsmanager get-secret-value --secret-id ariba/client_secret --query SecretString --output text)
```

## defs.yaml — Purchase requisitions

Ariba's **Operational Reporting API** is the workhorse for analytics:

```yaml
# defs/ariba_requisitions/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: ariba_requisitions
  api_url: https://api.ariba.com/api/operational-reporting-view/v1/prod/RequisitionList
  oauth_token_resource_key: ariba_token

  pagination: cursor
  cursor_response_path: PageToken
  cursor_param: pageToken
  records_path: Records

  query_params:
    realm: "MyAcmeRealm"          # your Ariba realm
    filters: "createDate >= '{partition_key}T00:00:00'"

  partition_type: daily
  partition_start: '2024-01-01'
  group_name: ariba
  kinds: [ariba, rest, sap, procurement]
```

## Ariba API families

| Family | Path prefix | What it exposes |
|---|---|---|
| **Operational Reporting** | `/api/operational-reporting-view/v1/prod/` | Read-only analytics views (requisitions, POs, invoices, suppliers) |
| **Procurement** | `/procurement/` | Sourcing requests, purchase orders, contracts |
| **Sourcing** | `/sourcing/` | RFx events, auctions, contracts |
| **Supplier Lifecycle Management** | `/slpm/` | Supplier registration + qualification |
| **Buying** | `/buying/` | Catalog + guided buying |
| **Contracts** | `/contracts/` | Contract authoring + lifecycle |
| **Approval** | `/approval/` | Approval workflows |

Each requires a separate scope on your OAuth app — request them in the Developer Portal.

## Common entity URLs

Pattern: `https://api.ariba.com/api/operational-reporting-view/v1/prod/<view>?realm=<realm>`

| View | Typical use |
|---|---|
| `RequisitionList` | Purchase requisitions |
| `PurchaseOrderList` | POs |
| `InvoiceReconciliationList` | AP / invoice matching |
| `SupplierList` | Vendor master |
| `ContractList` | Contract metadata |
| `ProjectList` | Sourcing projects |
| `EventList` | RFx events |

## Pagination — cursor pattern

Ariba responses look like:
```json
{
  "Records": [{...}, {...}],
  "PageToken": "opaque-cursor-string"
}
```

Set `pagination: cursor` + `cursor_response_path: PageToken` + `cursor_param: pageToken`. The component sends the cursor as a query param on subsequent calls and stops when the response omits it.

## Filtering at the API level

Ariba supports inline filter expressions:

```yaml
query_params:
  filters: "createDate >= '{partition_key}T00:00:00' and status = 'Approved'"
```

Quote-escape multi-condition filters with care; Ariba's filter parser is whitespace-tolerant but operator-strict (`>=` not `>=`, `=` not `==`).

## Asset check — connection health

The OAuth REST stack doesn't have a direct equivalent of `odata_check` yet (it's REST, not OData). For a smoke test:

```yaml
# defs/ariba_smoketest/defs.yaml — a minimal ingestion asset
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: ariba_smoketest
  api_url: https://api.ariba.com/api/operational-reporting-view/v1/prod/SupplierList
  oauth_token_resource_key: ariba_token
  pagination: none
  query_params: {realm: "MyAcmeRealm", limit: "1"}
  records_path: Records
  group_name: ariba
```

Schedule this every 15 minutes — if it fails, your Ariba auth or API access is broken.

## Trade-offs & gotchas

- **Scope mismatch is the most common error.** "401 Unauthorized" usually means your OAuth app doesn't have the right scope for that API family. Re-issue the OAuth app with more scopes via Developer Portal → Applications → Edit.
- **Realm is mandatory.** Most Ariba APIs require `realm=<yourRealmName>` query param — get it from your Ariba Network admin.
- **Sandbox vs prod.** `https://openapi.ariba.com` (test) vs `https://api.ariba.com` (prod). Wrong endpoint returns 404s that look like auth errors.
- **Rate limits.** Default 4 req/sec per OAuth app. Honor `Retry-After` on 429 (`oauth_rest_ingestion` does this automatically).
- **Operational Reporting lag.** Data freshness is typically 15-30 min behind transactional Ariba. Don't expect real-time accuracy.

## Why not OData?

Some newer Ariba APIs DO speak OData (especially Network APIs). If you have access to those, swap to [`odata_ingestion`](https://dagster-component-ui.vercel.app/c/odata_ingestion) with `auth_type: bearer` + a token minted via the same `oauth_token_resource`. Same auth pipe, different read mechanism.

## See also

- [`sap_concur_pipeline.md`](sap_concur_pipeline.md) — Concur uses `refresh_token` (rotation problem)
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
- [Ariba Developer Portal](https://developer.ariba.com/)
