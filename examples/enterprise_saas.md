# Enterprise SaaS resources (Workday / Marketo / Intercom / Plaid)

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

Resource components for major enterprise SaaS platforms. Each
provides a thin authenticated REST client that custom ops can use to
pull data into pipelines.

## Components used

| Resource | Auth | Use case |
|---|---|---|
| `workday_resource` | OAuth refresh token | HR data — workers, departments, comp |
| `marketo_resource` | OAuth client credentials | Marketing — leads, campaigns, programs |
| `intercom_resource` | API token (Bearer) | Support — contacts, conversations |
| `plaid_resource` | client_id + secret (no OAuth, just credentials) | Fintech — accounts, transactions, identity |

## Status

All four are code-validated against each vendor's published API. None
have free programmatic signup, so end-to-end validation requires user
credentials.

## Workday setup

Workday's OAuth is per-tenant. Get it from your Workday admin:
1. Create an Integration System User (ISU)
2. Register an API Client (System → Integrations → API Clients)
3. Use refresh-token grant to get an access token

```yaml
type: dagster_component_templates.WorkdayResourceComponent
attributes:
  resource_key: workday
  tenant_url: https://wd5-impl-services1.workday.com/ccx/api/v1/<tenant>
  client_id_env_var: WORKDAY_CLIENT_ID
  client_secret_env_var: WORKDAY_CLIENT_SECRET
  refresh_token_env_var: WORKDAY_REFRESH_TOKEN
```

## Marketo setup

Marketo uses OAuth client_credentials grant. Get the LaunchPoint service
client from Admin → LaunchPoint:

```yaml
type: dagster_component_templates.MarketoResourceComponent
attributes:
  resource_key: marketo
  rest_url: https://123-ABC-456.mktorest.com   # munchkin REST URL
  client_id_env_var: MARKETO_CLIENT_ID
  client_secret_env_var: MARKETO_CLIENT_SECRET
```

## Intercom setup

```yaml
type: dagster_component_templates.IntercomResourceComponent
attributes:
  resource_key: intercom
  api_token_env_var: INTERCOM_API_TOKEN
```

## Plaid setup

Plaid has three environments (sandbox / development / production):

```yaml
type: dagster_component_templates.PlaidResourceComponent
attributes:
  resource_key: plaid
  environment: sandbox
  client_id_env_var: PLAID_CLIENT_ID
  secret_env_var: PLAID_SECRET
```

## Custom op pattern

```python
@asset
def workday_workers(workday: WorkdayResource) -> pd.DataFrame:
    rows = workday.get("Workers")
    return pd.DataFrame(rows.get("data", []))
```

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
