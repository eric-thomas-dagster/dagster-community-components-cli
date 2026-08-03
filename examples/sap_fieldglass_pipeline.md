# SAP Fieldglass → Dagster

Pull contingent workforce / SOW / job-posting data from **SAP Fieldglass** (Vendor Management System) into Dagster.

Fieldglass manages contractors, statements of work, and contingent labor across global enterprises. Its REST API is OAuth-protected — same `oauth_rest_ingestion` pattern as Concur/Ariba.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP Fieldglass (cloud)                           │
   │   https://www.fieldglass.net/integration/...     │
   └──────────────────────┬───────────────────────────┘
                          │ REST + OAuth 2.0 (refresh_token)
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ oauth_token_resource + oauth_rest_ingestion      │
   │   → pandas DataFrame                             │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | OAuth2 refresh-token grant + rotation writeback |
| `oauth_rest_ingestion` | community | Paginated REST GET (page-based) → pandas DataFrame |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Run

### 1. OAuth setup in Fieldglass

1. Fieldglass admin UI → **Integrations** → **OAuth Clients** → Create
2. Capture: `client_id`, `client_secret`, plus a **refresh token** generated via the one-time consent flow
3. Store the refresh token in your secret manager (it rotates on each use — needs writeback)

### 2. Token resource with refresh-token rotation

```yaml
# resources/fg_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: fg_token
  token_endpoint: https://www.fieldglass.net/oauth2/token
  grant_type: refresh_token
  client_id_env_var: FG_CLIENT_ID
  client_secret_env_var: FG_CLIENT_SECRET
  refresh_token_env_var: FG_REFRESH_TOKEN
  # Fieldglass rotates refresh tokens — persist the new one:
  refresh_writeback_command_env_var: FG_REFRESH_WRITEBACK_CMD
```

In your runtime env:
```bash
export FG_REFRESH_WRITEBACK_CMD='aws secretsmanager update-secret --secret-id fieldglass/refresh_token --secret-string {token}'
```

### 3. Ingestion

```yaml
# defs/fg_workers/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: fg_workers
  api_url: https://www.fieldglass.net/integration/v1/workers
  oauth_token_resource_key: fg_token
  pagination: page
  page_param: page
  page_start: 1
  records_path: data
  query_params:
    pageSize: "100"
    modifiedSince: "{partition_key}T00:00:00Z"
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_fieldglass
  kinds: [sap, fieldglass, contingent-workforce, rest]
```

## Useful Fieldglass endpoints

| Endpoint | What it returns |
|---|---|
| `/integration/v1/workers` | Active + historical contingent workers |
| `/integration/v1/jobpostings` | Open job postings (requisitions) |
| `/integration/v1/sows` | Statements of work |
| `/integration/v1/timesheets` | Worker timesheets |
| `/integration/v1/expenses` | Worker expense submissions |
| `/integration/v1/invoices` | Vendor invoices |
| `/integration/v1/suppliers` | Approved supplier list |
| `/integration/v1/costcenters` | Cost center hierarchy |

## Common analytics scenarios

| Use case | Pipeline shape |
|---|---|
| **Total contingent spend by department** | Workers + timesheets + expenses + cost centers → join → aggregate by dept |
| **Spend vs SOW budget** | SOWs + worker time → variance |
| **Time-to-fill by job category** | Job postings + worker fills → cycle-time analysis |
| **Vendor performance** | Workers + suppliers + ratings → vendor scorecard |
| **Cross-system join with workday/HR** | Fieldglass contingent + Workday FTE → unified workforce view |

## Trade-offs & gotchas

- **Refresh-token rotation.** Without writeback, the next run fails. The `oauth_token_resource` handles this — set up your writeback command at deployment time.
- **Rate limits.** Fieldglass enforces per-client limits (~5 req/sec typical). Honor 429 + `Retry-After`. Set `retry_policy_max_retries: 5`.
- **PII.** Worker records contain SSNs, address data. Tag `pii: true` + route through masking before downstream consumption.
- **Multi-tenant.** Each Fieldglass tenant (production / sandbox) needs its own OAuth client — separate `oauth_token_resource` instances.
- **Date filters.** `modifiedSince` is the right filter for incremental ingestion. `createdSince` misses records that were updated but not created in the window.

## See also

- [`sap_concur_pipeline.md`](sap_concur_pipeline.md) — sister: Concur expense management (same OAuth-refresh pattern)
- [`sap_ariba_pipeline.md`](sap_ariba_pipeline.md) — sister: Ariba procurement (OAuth client_credentials)
- `oauth_token_resource`
- [SAP Fieldglass docs](https://help.sap.com/docs/SAP_FIELDGLASS_VMS)
