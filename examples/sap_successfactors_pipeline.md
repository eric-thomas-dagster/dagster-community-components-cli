# SAP SuccessFactors → Dagster pipeline blueprint

Pull HRIS data (employees, departments, jobs, performance reviews, comp plans) from SuccessFactors into Dagster via the **OData v2** API.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP SuccessFactors                               │
   │   https://api<dc>.successfactors.com/odata/v2    │
   │   (dc = datacenter: 4, 5, 6, 8, 10, 12 typical)  │
   └─────────────────────┬────────────────────────────┘
                         │ OData v2 + basic auth (User@CompanyID)
                         ▼
   ┌──────────────────────────────────────────────────┐
   │ odata_ingestion (asset)                          │
   │   GET /odata/v2/User?$expand=manager,department  │
   └─────────────────────┬────────────────────────────┘
                         │
                         ▼
   ┌──────────────────────────────────────────────────┐
   │ summarize → headcount_by_org                     │
   └─────────────────────┬────────────────────────────┘
                         │
                         ▼
   ┌──────────────────────────────────────────────────┐
   │ dataframe_to_snowflake / parquet                 │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `odata_ingestion` | community | OData v2 GET against SuccessFactors entity sets → pandas DataFrame |
| `odata_check` | community | Optional — smoke-test SF tenant + entity-set reachability |
| `summarize`, `dataframe_join`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Auth: User@CompanyID

SuccessFactors uses a non-standard basic-auth format: the username is `<APIUser>@<CompanyID>`. Example: `dagster_api@ACME01`.

```yaml
auth_type: basic
auth_username_env_var: SF_USERNAME    # set to 'dagster_api@ACME01'
auth_password_env_var: SF_PASSWORD
```

For OAuth (newer setup with SAML assertion grant), set up OAuth Client in **SuccessFactors Provisioning → Manage OAuth2 Client Applications**, then use `oauth_token_resource` with a JWT assertion.

## defs.yaml — Employee data

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: sf_employees
  service_url: https://api4.successfactors.com/odata/v2
  entity_set: PerPerson
  odata_version: v2
  auth_type: basic
  auth_username_env_var: SF_USERNAME
  auth_password_env_var: SF_PASSWORD
  select: personIdExternal,firstName,lastName,businessEmail,countryOfBirth,nationality
  expand: employmentNav/personalInfoNav,employmentNav/jobInfoNav
  filter: lastModifiedDateTime ge datetime'{partition_key}T00:00:00'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: successfactors
  kinds: [successfactors, odata, sap, hris]
```

## Common SuccessFactors entity sets

| Entity | What it exposes |
|---|---|
| `PerPerson` | Person record (top-level identity) |
| `EmpJob` | Job assignments (current + historical) |
| `EmpEmployment` | Employment events (hires, transfers, terminations) |
| `EmpCompensation` | Comp data |
| `User` | Login user + email + phone + manager |
| `FOCompany` | Foundation: legal entities |
| `FODepartment` | Foundation: departments |
| `FOLocation` | Foundation: locations |
| `FOPayGroup` | Foundation: pay groups |
| `Goal_<n>` | Goal Management |
| `PerformanceReview` | Performance reviews |
| `FormHeader` | Generic form (review / comp / etc.) headers |
| `LearningHistory` | Learning Management System |

> SuccessFactors module families (Employee Central, Recruiting, Onboarding, Learning, Performance, Comp) each have their own entity sets. Browse with `GET /odata/v2/$metadata`.

## Effective-dated entities

Most SF "Per*" entities are **effective-dated** — each row has `startDate` + `endDate`. To get the current state:

```yaml
filter: |
  startDate le datetime'{partition_key}T23:59:59' and
  endDate ge datetime'{partition_key}T00:00:00'
```

For historical analytics, pull ALL rows and join on `personIdExternal` + date range downstream.

## `$expand` to flatten the org graph

SF data is heavily relational — Person → Employment → Job → Department → Manager. `$expand` traverses these in one request:

```yaml
expand: employmentNav/jobInfoNav/departmentNav,employmentNav/jobInfoNav/managerEmployeeNav
```

`pandas.json_normalize` flattens with `_` separator: `employmentNav_jobInfoNav_departmentNav_name`.

## Asset check — verify schema

```yaml
type: dagster_community_components.ODataCheckComponent
attributes:
  asset_key: successfactors/employees
  service_url: https://api4.successfactors.com/odata/v2
  entity_set: PerPerson
  odata_version: v2
  auth_type: basic
  auth_username_env_var: SF_USERNAME
  auth_password_env_var: SF_PASSWORD
  expect_columns: [personIdExternal, firstName, lastName, businessEmail]
  min_rows: 1
```

## Trade-offs & gotchas

- **Rate limits.** SF tenants have request quotas (varies by license tier). The component honors HTTP 429 with `Retry-After`; set `retry_policy_max_retries: 5` for resilience.
- **`$select` is mandatory at scale.** Default response includes ALL nullable fields including dozens of empty strings. Cut down via `$select` aggressively.
- **Datacenter routing.** `api4.successfactors.com` ≠ `api5.successfactors.com`. Check your tenant's URL.
- **Termination handling.** Terminated employees stay in `PerPerson` indefinitely. Filter on `employmentNav/employmentStatus eq 'A'` for active-only.
- **PII.** SuccessFactors data is sensitive. Tag assets with `pii: true` and consider routing through a masking transform before downstream.

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — same `odata_ingestion`, ERP master data
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough (Northwind, validated)
- [SF OData reference](https://help.sap.com/docs/SAP_SUCCESSFACTORS_PLATFORM/d599f15995d348a1b45ba5603e2aba9b/03e1fc3791684367a6a76a614a2916de.html)
