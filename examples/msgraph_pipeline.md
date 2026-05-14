# Microsoft Graph → Dagster pipeline blueprint

Pull data from Microsoft Graph (Outlook / Calendar / Teams / OneDrive / SharePoint / Azure AD) via OData v4. Same `odata_ingestion` component as everything else.

## Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │ Microsoft Graph                                      │
   │   https://graph.microsoft.com/v1.0                   │
   │   https://graph.microsoft.com/beta (preview)         │
   └──────────────────────────┬───────────────────────────┘
                              │ OData v4 + Bearer token
                              │ Azure AD app permissions
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ oauth_token_resource (Azure AD)                      │
   │   OR DefaultAzureCredential (workload identity)      │
   └──────────────────────────┬───────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                              │
   │   GET /users / /groups / /me/messages / etc.         │
   │   → pandas DataFrame                                 │
   └──────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | Azure AD OAuth2 client_credentials grant (or workload identity) |
| `odata_ingestion` | community | OData v4 GET against `graph.microsoft.com` → pandas DataFrame |
| `odata_check` | community | Optional — smoke-test Graph endpoint reachability |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Permissions: delegated vs application

Microsoft Graph has two permission models:

| | Delegated | Application |
|---|---|---|
| **Acts as** | Logged-in user | App identity |
| **Scope** | What that user can see | Tenant-wide |
| **Grant type** | `authorization_code` or `refresh_token` | `client_credentials` |
| **Use for headless Dagster** | ❌ Requires user dance | ✅ Pure M2M |

**For Dagster pipelines, always use Application permissions.** They run unattended and don't tie data access to a single human's account.

## Azure AD app setup

1. **Azure portal** → **App registrations** → **New registration**
2. **API permissions** → **Add** → **Microsoft Graph** → **Application permissions** → pick scopes (e.g. `User.Read.All`, `Mail.Read`, `Calendars.Read`)
3. Click **Grant admin consent** (requires tenant admin)
4. **Certificates & secrets** → **New client secret** → copy value
5. Copy **Tenant ID** + **Client ID** from app overview

## Token resource

```yaml
# resources/msgraph_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: msgraph_token
  token_endpoint: https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
  grant_type: client_credentials
  client_id_env_var: GRAPH_CLIENT_ID
  client_secret_env_var: GRAPH_CLIENT_SECRET
  scope: "https://graph.microsoft.com/.default"
```

## defs.yaml — All AAD users

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: aad_users
  service_url: https://graph.microsoft.com/v1.0
  entity_set: users
  odata_version: v4
  auth_type: bearer
  auth_token_env_var: GRAPH_ACCESS_TOKEN
  select: id,userPrincipalName,displayName,mail,jobTitle,department,createdDateTime
  filter: accountEnabled eq true
  group_name: msgraph
  kinds: [msgraph, dynamics365, odata, microsoft]
```

## Useful Graph endpoints

### Identity / org structure

| Endpoint | Returns |
|---|---|
| `/users` | All AAD users |
| `/groups` | All AAD groups |
| `/groups/{id}/members` | Group membership |
| `/directoryRoles` | Admin roles |
| `/auditLogs/signIns` | Sign-in events (premium licensing) |

### Mail (Outlook)

| Endpoint | Returns |
|---|---|
| `/users/{id}/messages` | Email inbox |
| `/users/{id}/mailFolders` | Folder structure |
| `/users/{id}/mailFolders/inbox/messages` | Specific folder |

### Calendar

| Endpoint | Returns |
|---|---|
| `/users/{id}/calendars` | Calendars |
| `/users/{id}/events` | Events on default calendar |
| `/users/{id}/calendarView?startDateTime=...&endDateTime=...` | Time-bounded events |

### Teams

| Endpoint | Returns |
|---|---|
| `/teams` | All teams (admin scope) |
| `/teams/{id}/channels` | Channels |
| `/teams/{id}/channels/{id}/messages` | Channel messages (requires Premium licensing for chat) |
| `/chats` | 1:1 + group chats |
| `/communications/callRecords` | Call records (analytics) |

### OneDrive / SharePoint

| Endpoint | Returns |
|---|---|
| `/users/{id}/drive` | Personal OneDrive metadata |
| `/users/{id}/drive/root/children` | Top-level files |
| `/sites` | SharePoint sites |
| `/sites/{id}/drives` | Document libraries |

### Reports (heavy usage data)

| Endpoint | Returns |
|---|---|
| `/reports/getOffice365ActiveUserDetail(period='D7')` | M365 usage per user (7-day window) |
| `/reports/getEmailActivityUserDetail(period='D30')` | Email send/receive counts |
| `/reports/getSharePointSiteUsageDetail(period='D7')` | SP site usage |

Reports endpoints return CSVs by default — set `extra_headers: {Accept: application/json}` to coerce JSON.

## Pagination — `@odata.nextLink`

Graph paginates with `@odata.nextLink` in the response body. `odata_ingestion` (v4 mode) follows these automatically. Default page size is 100; bump via `top: 999` (the max).

## Filter quirks

Graph's `$filter` is **less feature-complete than typical OData**:

- ✅ `eq`, `ne`, `gt`, `lt`, `ge`, `le`, `and`, `or`, `not`, `in`
- ✅ `startswith`, `endswith`, `contains`
- ❌ No arithmetic (`add`, `sub`, etc.)
- ❌ Limited support for complex types — some properties aren't filterable
- ⚠️ Many filters require `ConsistencyLevel: eventual` header + `$count: true` query param (set in `extra_headers` / `extra_query`)

For complex filtering, prefer `extra_headers: {ConsistencyLevel: eventual}` and `extra_query: {$count: "true"}`:

```yaml
extra_headers: {ConsistencyLevel: eventual}
extra_query: {$count: "true"}
filter: startswith(displayName, 'A')
```

## Delta queries — incremental sync

Graph supports `delta` endpoints for entities that change frequently:

```yaml
service_url: https://graph.microsoft.com/v1.0
entity_set: users/delta
odata_version: v4
auth_type: bearer
auth_token_env_var: GRAPH_ACCESS_TOKEN
```

First call returns all users + a `@odata.deltaLink`. Persist that link, use it as `service_url` on next run — Graph returns only changes since last call. Best paired with Dagster's **dynamic partitions** where each "partition" is a snapshot of the delta cursor.

## Workload identity (production)

Running in AKS with **Azure Workload Identity** on the pod:

```python
# Skip oauth_token_resource entirely:
from azure.identity import DefaultAzureCredential

@asset(...)
def aad_users(context):
    cred = DefaultAzureCredential()
    token = cred.get_token("https://graph.microsoft.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}
    # ... GET /users ...
```

`DefaultAzureCredential` auto-discovers workload identity → no stored secrets, no rotation.

## Trade-offs & gotchas

- **App permissions vs delegated permissions.** Different scopes show up under each. For Dagster (headless), always Application.
- **License-gated endpoints.** Teams `/chats/{id}/messages`, advanced auditing, and several reports require specific M365 tenant SKUs. Test in dev tenant before relying.
- **Rate limits.** Graph enforces per-app + per-tenant quotas. Honor 429 + `Retry-After`. For high-volume pulls, set `retry_policy_max_retries: 10`.
- **PII.** Email, calendar, message bodies are highly sensitive. Tag assets `pii: true`; route through masking before downstream.
- **Deleted items.** By default they vanish. Use `/directory/deletedItems/microsoft.graph.user` for soft-deleted users (30-day retention).

## See also

- [`dynamics365_pipeline.md`](dynamics365_pipeline.md) — sister Microsoft API (CRM data)
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level
- [Microsoft Graph reference](https://learn.microsoft.com/en-us/graph/overview)
