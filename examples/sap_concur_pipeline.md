# SAP Concur → Dagster pipeline blueprint

Pull expense reports, travel itineraries, and invoice approvals from SAP Concur via OAuth 2.0 refresh-token flow. **Headless from day one** — refresh-token rotation handled via writeback callback.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ SAP Concur                                          │
   │   us.api.concursolutions.com (US datacenter)        │
   │   eu.api.concursolutions.com (EU)                   │
   └─────────────────────────────┬───────────────────────┘
                                 │ OAuth 2.0 (refresh_token grant)
                                 │ + Bearer access_token (cached)
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ oauth_token_resource (resource)                     │
   │   client_id + client_secret + refresh_token         │
   │   → access_token (auto-refreshed, in-memory cache)  │
   │   → NEW refresh_token written back to AWS SM        │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ oauth_rest_ingestion (asset)                        │
   │   GET /expensereports/v4/reports                    │
   │   Pagination: next_url ({NextPage: 'https://...'})  │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ summarize → expense_by_department                   │
   └─────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | OAuth2 refresh-token grant + rotation writeback for headless ops |
| `oauth_rest_ingestion` | community | Paginated REST GET (Concur `NextPage` style) → pandas DataFrame |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Headless OAuth refresh-token rotation (the actual hard part)

Concur **rotates refresh tokens on every refresh**. If you don't persist the new one back to your secret store, your next run fails. This pipeline configures the writeback so it survives unattended.

### Step 1: bootstrap the refresh token (once, on a laptop)

```bash
# Interactive OAuth flow — happens once
curl -X POST https://us.api.concursolutions.com/oauth2/v0/token \
  -d "grant_type=password" \
  -d "client_id=$CONCUR_CLIENT_ID" \
  -d "client_secret=$CONCUR_CLIENT_SECRET" \
  -d "username=$CONCUR_USERNAME" \
  -d "password=$CONCUR_PASSWORD"
# Response includes initial refresh_token — copy it
```

Drop the initial refresh token into AWS Secrets Manager (or Azure KV, Vault, etc.):

```bash
aws secretsmanager create-secret \
  --name concur/refresh_token \
  --secret-string '<refresh-token-from-above>'
```

### Step 2: configure the Dagster resource

```yaml
# resources/concur_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: concur_token
  token_endpoint: https://us.api.concursolutions.com/oauth2/v0/token
  grant_type: refresh_token
  client_id_env_var: CONCUR_CLIENT_ID
  client_secret_env_var: CONCUR_CLIENT_SECRET
  refresh_token_env_var: CONCUR_REFRESH_TOKEN
  # Persist NEW refresh tokens back to AWS SM:
  refresh_writeback_command_env_var: CONCUR_REFRESH_WRITEBACK_CMD
  timeout_seconds: 30
  early_refresh_seconds: 60
```

### Step 3: set the writeback command in your runtime env

```bash
# AWS Secrets Manager:
export CONCUR_REFRESH_WRITEBACK_CMD='aws secretsmanager update-secret --secret-id concur/refresh_token --secret-string {token}'

# Azure Key Vault:
export CONCUR_REFRESH_WRITEBACK_CMD='az keyvault secret set --vault-name myvault --name concur-refresh-token --value {token}'

# HashiCorp Vault:
export CONCUR_REFRESH_WRITEBACK_CMD='vault kv put secret/concur refresh_token={token}'
```

The `{token}` placeholder is substituted at refresh time. Dagster Daemon / k8s pod runs this command unattended whenever Concur returns a new refresh token (which is every refresh).

### Step 4: load the refresh token at startup

```bash
# k8s init container, ECS init script, or systemd ExecStartPre:
export CONCUR_REFRESH_TOKEN=$(aws secretsmanager get-secret-value --secret-id concur/refresh_token --query SecretString --output text)
```

The Dagster process reads `CONCUR_REFRESH_TOKEN` once at startup; from then on, the resource handles refresh + writeback automatically.

## defs.yaml — Expense reports

```yaml
# defs/concur_expense_reports/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: concur_expense_reports
  api_url: https://us.api.concursolutions.com/expensereports/v4/reports
  oauth_token_resource_key: concur_token   # references the resource above
  pagination: next_url
  next_url_path: NextPage
  records_path: Items
  query_params:
    modifiedAfter: "{partition_key}T00:00:00Z"
    limit: "100"
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: concur
  kinds: [concur, rest, sap]
```

## Concur API endpoints worth ingesting

| Endpoint | What it returns |
|---|---|
| `/expensereports/v4/reports` | Expense reports (submitted, approved, paid) |
| `/expensereports/v4/users/{userID}/reports` | Per-user expense history |
| `/travel/v1/itineraries` | Booked trips |
| `/api/v3.0/expense/entries` | Individual expense line items |
| `/api/v3.0/expense/expensereports` | Older Expense Reports v3 (still supported) |
| `/invoices/v1/invoices` | Invoice management |
| `/common/v3.0/users` | User directory |
| `/list/v3.0/lists` | List items (departments, cost centers, etc.) |

## Pagination — `NextPage` (next_url pattern)

Concur returns:
```json
{
  "Items": [{...}, {...}],
  "NextPage": "https://us.api.concursolutions.com/expensereports/v4/reports?continuationToken=..."
}
```

Set `pagination: next_url` + `next_url_path: NextPage` and `records_path: Items`. The component follows links automatically until `NextPage` disappears or `max_pages` (default 1000) is hit.

## Partitioning by `modifiedAfter`

```yaml
query_params:
  modifiedAfter: "{partition_key}T00:00:00Z"
  modifiedBefore: "{partition_key_next}T00:00:00Z"  # if your version supports it
partition_type: daily
partition_start: '2024-01-01'
```

Daily backfill works cleanly — re-running a partition pulls the same date window.

## Trade-offs & gotchas

- **Refresh-token rotation is the actual headache.** Without writeback, every run is also the last run. Test the writeback command works (`echo 'new_token' | xargs -I{} <your-command-template-with-{token}-substituted>`) before deploying.
- **Refresh tokens have a max lifetime** even with rotation (Concur's max is ~6 months from initial issue). Build a calendar reminder to re-bootstrap before expiry.
- **EU vs US datacenter.** `us.api.concursolutions.com` vs `eu.api.concursolutions.com` — wrong DC returns 404 or auth errors.
- **Rate limits.** Default ~5 req/sec per OAuth app. Set `retry_policy_max_retries: 5` and consider larger `limit` values to reduce request count.
- **PII.** Expense reports contain employee names, amounts, vendor names. Tag assets with `pii: true`; route through masking transforms downstream.

## Why not use `client_credentials` instead?

Concur supports it for some endpoints (the **Application APIs**), but most user-facing data (Expense, Travel, Invoice) requires user-context tokens — which means refresh_token flow. If your scenario is purely admin-side (settings, lists), check whether the `client_credentials` path is available — it's much simpler.

## See also

- [`sap_ariba_pipeline.md`](sap_ariba_pipeline.md) — Ariba uses `client_credentials` (no rotation problem)
- [`odata_pipeline.md`](odata_pipeline.md) — read-protocol primitives
- [Concur Developer Center](https://developer.concur.com/api-reference/)
