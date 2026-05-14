# Microsoft Dynamics 365 / Dataverse → Dagster pipeline blueprint

Pull data from Microsoft Dynamics 365 (Sales / Customer Service / Field Service / Marketing) and Microsoft Dataverse via OData v4. Same `odata_ingestion` component as the SAP examples — just different config.

## Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │ Dynamics 365 / Dataverse                             │
   │   https://<org>.crm.dynamics.com/api/data/v9.2       │
   └──────────────────────────┬───────────────────────────┘
                              │ OData v4 + Bearer token
                              │ (Azure AD OAuth — workload identity in prod)
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ oauth_token_resource (Azure AD)                      │
   │   OR DefaultAzureCredential in a pod with WI          │
   │   → access_token                                      │
   └──────────────────────────┬───────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                              │
   │   GET /api/data/v9.2/accounts?$select=...&$filter=…  │
   │   → pandas DataFrame                                 │
   └──────────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | Azure AD OAuth2 client_credentials grant |
| `odata_ingestion` | community | OData v4 GET against Dataverse (`/api/data/v9.2/...`) → pandas DataFrame |
| `odata_check` | community | Optional — smoke-test tenant + entity-set |
| `dataframe_to_odata` | community | Optional — write back to Dataverse (POST / PATCH / DELETE) |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Auth: Azure AD app registration

Dataverse uses Azure AD. The cleanest M2M path:

1. **Azure portal** → **App registrations** → **New registration**
2. Register a confidential client. Copy: **Tenant ID**, **Client ID**.
3. **Certificates & secrets** → **New client secret**. Copy the value.
4. **API permissions** → **Add a permission** → **Dynamics CRM** → **Delegated/Application** → **user_impersonation** (for delegated) or app-scoped roles.
5. In Dataverse: **Settings** → **Security** → **Users** → **Application Users** → **New** → link your AAD app, assign a Security Role with read on the tables you need.

### Token resource

```yaml
# resources/dataverse_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: dataverse_token
  token_endpoint: https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
  grant_type: client_credentials
  client_id_env_var: DV_CLIENT_ID
  client_secret_env_var: DV_CLIENT_SECRET
  scope: "https://yourorg.crm.dynamics.com/.default"
```

### Workload-identity-preferred (production)

If you're running in AKS/EKS/GKE with workload identity, skip secrets entirely:

```python
# in a custom asset (until odata_ingestion natively wires WI):
from azure.identity import DefaultAzureCredential

@asset(...)
def dataverse_accounts(context):
    cred = DefaultAzureCredential()
    token = cred.get_token("https://yourorg.crm.dynamics.com/.default")
    headers = {"Authorization": f"Bearer {token.token}"}
    # ... GET /api/data/v9.2/accounts ...
```

## defs.yaml — Dataverse Accounts table

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: dv_accounts
  service_url: https://yourorg.crm.dynamics.com/api/data/v9.2
  entity_set: accounts
  odata_version: v4
  auth_type: bearer
  auth_token_env_var: DV_ACCESS_TOKEN
  select: name,accountid,industrycode,revenue,createdon,modifiedon
  filter: modifiedon ge {partition_key}T00:00:00Z
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: dataverse
  kinds: [dynamics365, dataverse, odata]
```

## Common Dataverse entity sets

| Entity Set | What it returns |
|---|---|
| `accounts` | Companies / organizations |
| `contacts` | People |
| `leads` | Sales leads |
| `opportunities` | Sales opportunities |
| `incidents` | Customer service cases |
| `systemusers` | CRM users (internal) |
| `tasks` / `appointments` / `phonecalls` | Activities |
| `<custom>` | Custom tables — pluralized lower-case, e.g. `acme_projects` |

Full list: `GET /api/data/v9.2/EntityDefinitions?$select=EntitySetName`

## `$expand` to flatten lookups

Dynamics uses **lookup columns** (single-valued navigation properties). To pull the related row inline:

```yaml
expand: primarycontactid($select=fullname,emailaddress1)
```

`pandas.json_normalize` flattens: `primarycontactid_fullname`, etc.

## FetchXML alternative

Dataverse supports a SOAP-era query language called **FetchXML**. You can submit FetchXML via OData:

```
GET /api/data/v9.2/accounts?fetchXml=<fetch>...</fetch>
```

Build the FetchXML in **Advanced Find** in Dynamics, click **Download Fetch XML**, paste into `query_params: {fetchXml: <encoded-xml>}`. For complex multi-table joins, FetchXML is more concise than `$expand` chains.

## Partitioning by `modifiedon`

```yaml
filter: modifiedon ge {partition_key}T00:00:00Z and modifiedon lt {partition_key_next}T00:00:00Z
partition_type: daily
partition_start: '2024-01-01'
```

Dataverse responds to `modifiedon` filtering via its indexed date column — fast even on large tables.

## Trade-offs & gotchas

- **AAD token lifetime.** Default 1 hour; refresh handled automatically by `oauth_token_resource`.
- **Plural entity sets.** `accounts` (plural lower-case), not `Account` (the table's display name). Confusing if you're new to Dataverse.
- **API service limits.** ~6,000 req/5min per user per organization. Honor 429s + use `$top` with pagination to chunk large pulls.
- **Choice fields are integers.** `industrycode: 1` not `industrycode: "Manufacturing"`. Join against the LocalizedLabel table or maintain a downstream mapping.
- **Deleted records.** By default `DELETE`d records vanish. Enable Dataverse's "Change Tracking" feature + use `Prefer: odata.track-changes` header for delta queries.

## See also

- [`msgraph_pipeline.md`](msgraph_pipeline.md) — sister Microsoft API (mail, calendar, Teams, OneDrive)
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
- [Dataverse Web API reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/overview)
