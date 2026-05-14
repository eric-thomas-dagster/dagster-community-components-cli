# SAP Analytics Cloud (SAC) → Dagster

Pull **SAP Analytics Cloud** stories, models, and comments into Dagster. SAC is SAP's cloud BI / planning / dashboarding product — it sits downstream of Datasphere / S/4HANA / BW.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP Analytics Cloud                              │
   │   https://<tenant>.<region>.hcs.cloud.sap        │
   │   /api/v1/...                                    │
   └──────────────────────┬───────────────────────────┘
                          │ REST + OAuth 2.0 (XSUAA)
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ oauth_token_resource + oauth_rest_ingestion      │
   │   → pandas DataFrame                             │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `oauth_token_resource` | community | OAuth2 client_credentials for SAC REST API |
| `oauth_rest_ingestion` | community | GET SAC stories / models / comments metadata |
| `rest_api_fetcher` | community | POST data INTO SAC models (the more valuable direction) |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## When to integrate SAC with Dagster

- **Push data INTO SAC models** for SAC stories to consume
- **Pull SAC catalog metadata** for asset-lineage clarity (which stories use which models)
- **Trigger SAC model refreshes** after upstream data lands
- **Pull dashboard state / comments** for audit trails

For the **read SAC stories rendered** use case, the right approach is usually to query the underlying data source directly (Datasphere / HANA / BW) instead of SAC's rendering API.

## Setup

### 1. Create an OAuth client in SAC

1. SAC UI → **System → Administration → App Integration** → **Add a New OAuth Client**
2. Set Purpose = "API Access", set scopes (e.g. `Public_API`)
3. Copy `client_id`, `client_secret`, `token_endpoint`

### 2. Token resource

```yaml
# resources/sac_token.yaml
type: dagster_community_components.OAuthTokenResourceComponent
attributes:
  resource_key: sac_token
  token_endpoint: https://my-tenant.authentication.eu10.hana.ondemand.com/oauth/token
  grant_type: client_credentials
  client_id_env_var: SAC_CLIENT_ID
  client_secret_env_var: SAC_CLIENT_SECRET
  auth_in: basic
```

### 3. Pull SAC stories metadata

```yaml
# defs/sac_stories/defs.yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: sac_stories
  api_url: https://my-tenant.eu10.hcs.cloud.sap/api/v1/stories
  oauth_token_resource_key: sac_token
  pagination: cursor
  cursor_response_path: '@odata.nextLink'
  cursor_param: skiptoken
  records_path: value
  group_name: sap_sac
  kinds: [sap, sac, analytics, rest]
```

## Useful SAC REST endpoints

| Endpoint | What it returns |
|---|---|
| `/api/v1/stories` | Stories (dashboards) catalog |
| `/api/v1/models` | Models (data dimensions + measures) |
| `/api/v1/connections` | Data source connections |
| `/api/v1/dataimport/<model_id>` | Import data into a model (POST) |
| `/api/v1/dataexport/<model_id>` | Export model data (GET) |
| `/api/v1/responsibilities` | Planning responsibilities (S&OP) |
| `/api/v1/comments` | Story / cell comments |
| `/api/v1/users` | SAC users + groups |

## Push data INTO a SAC model

This is the most valuable Dagster→SAC pattern: refresh a SAC model after upstream ETL completes:

```yaml
# defs/sac_revenue_model_loaded/defs.yaml
type: dagster_community_components.RestApiFetcherComponent  # or build a custom asset
attributes:
  asset_name: sac_revenue_model_loaded
  api_url: https://my-tenant.eu10.hcs.cloud.sap/api/v1/dataimport/<model_id>
  method: POST
  # ... auth via oauth_token_resource bearer header ...
```

Or write a custom asset for richer body control:

```python
@asset(required_resource_keys={"sac_token"})
def sac_revenue_model_loaded(context, upstream_revenue: pd.DataFrame):
    import requests
    h = {"Authorization": context.resources.sac_token.get_authorization_header()}
    # Convert DataFrame to SAC's expected JSON format
    body = {
        "Data": upstream_revenue.to_dict(orient="records"),
        "Mappings": {col: col for col in upstream_revenue.columns},
    }
    r = requests.post(
        f"https://my-tenant.eu10.hcs.cloud.sap/api/v1/dataimport/<model_id>",
        headers=h, json=body
    )
    r.raise_for_status()
    return r.json()
```

## Trigger a SAC model refresh after upstream load

If the SAC model is connected to Datasphere / HANA / BW (live or imported), you don't push data — you trigger a refresh:

```yaml
# defs/sac_refresh_trigger/defs.yaml — custom POST to import job endpoint
```

## Trade-offs & gotchas

- **API surface is partial.** Not every SAC feature has a REST API. Some operations (catalog edits, story layout) are UI-only.
- **Live vs imported models.** Live models query the source on-demand — no SAC import needed. Imported models cache in SAC's HANA — push-and-refresh pattern applies.
- **Quotas.** SAC API has request quotas per tenant. Bulk imports go through chunked POST (split DataFrames into ~10K-row batches).
- **The PUSH side is often more valuable than PULL.** Dagster's role is usually "land data into SAC's models" rather than "extract from SAC".
- **Don't replicate SAC stories in Dagster.** SAC stories are visualization — let SAC do what it does. Use Dagster for the data prep + freshness gate.

## See also

- [`sap_datasphere_pipeline.md`](sap_datasphere_pipeline.md) — SAC's typical upstream
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
- [SAP Analytics Cloud REST API reference](https://api.sap.com/package/SAPAnalyticsCloud/rest)
