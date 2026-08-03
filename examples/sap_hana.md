# SAP HANA via SQLAlchemy

**Code-validated only** — components built from each vendor's SDK / API spec; end-to-end validation requires vendor credentials.

SAP HANA database resource — provides a SQLAlchemy URL helper.
Works with HANA Cloud, on-prem HANA, and Azure HANA (preview as a
partner offering).

## Components used

| Component | Category | Role |
|---|---|---|
| `sap_hana_resource` | resource | SQLAlchemy URL builder + engine factory |
| `dataframe_to_table` (existing) | sink | Write via `hana://` URL |
| `dataframe_from_sql` (existing) | source | Query via `hana://` URL |

## Status

**Code-validated** against the `sqlalchemy-hana` dialect spec. End-to-end
validation requires a HANA instance, which is harder than other Azure
services because of SAP's licensing model.

### Why we couldn't auto-validate

| Path | Blocker |
|---|---|
| HANA Express Docker | Image is x86_64-only; incompatible with arm64 hosts |
| HANA Cloud trial | Requires SAP BTP signup with manual verification (~24h) |
| HANA on Azure (partner) | SAP-licensed, multi-thousand-dollar minimum cost |

## Provisioning options

**A) HANA Cloud (recommended for trying out):**
1. Sign up at https://account.hanatrial.ondemand.com (free trial, 30 days)
2. Create a HANA Cloud database
3. Note the host URL (e.g. `myhana.hanacloud.ondemand.com`)
4. Get DBADMIN password from the trial UI

**B) Azure HANA Partner (for prod-grade):**
1. Marketplace → SAP HANA → Configure
2. Pick instance size (S192xm, etc.)
3. Wait 30-60min for provisioning
4. Connect via the public IP / private link

## defs.yaml

```yaml
type: dagster_component_templates.SapHanaResourceComponent
attributes:
  resource_key: sap_hana
  host: myhana.hanacloud.ondemand.com
  port: 443                    # 443 for HANA Cloud, 30015 for on-prem default
  user: DBADMIN
  password_env_var: HANA_PASSWORD
  encrypt: true
  validate_certificate: true
```

## Use with existing SQL components

```yaml
# Build the URL once via the resource, expose as DATABASE_URL,
# then use the existing dataframe_to_table component:
type: dagster_component_templates.DataframeToTableComponent
attributes:
  database_url_env_var: HANA_URL          # set by an upstream op using sap_hana resource
  table_name: dagster_aggregations
  if_exists: replace
```

## Common HANA on Azure use cases

- Pull data from SAP S/4HANA / BW into Dagster pipelines for analytics
- Push aggregated results back into HANA Calculation Views
- Sync HANA tables to a data lake (ADLS Gen2) for ML
- Replicate HANA → Azure SQL via an ETL job for reporting cost

## See also

<!-- TODO: link related walkthroughs -->
