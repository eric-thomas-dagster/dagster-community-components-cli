# Dagster+ Audit Log → SIEM demo

**This is a Dagster+ demo** — it requires a Dagster+ deployment + a user token.
Pull audit-log entries from your Dagster+ GraphQL API, normalize them to OCSF,
and write them somewhere — CSV first to validate, then swap in a real SIEM
sink (Splunk / Sentinel / Datadog / Sumo / Chronicle / QRadar / Elastic).

Pipeline (3 components, all autoloaded by `dg`):

```
dagster_plus_audit_log_ingestion → siem_event_normalizer (OCSF) → dataframe_to_csv
                                            │
                                            └─→ swap for any SIEM sink
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `dagster_plus_audit_log_ingestion` | ingestion | Pull audit log via Dagster+ GraphQL |
| 2 | `siem_event_normalizer` | transformation | Map to OCSF schema |
| 3 | `dataframe_to_csv` | sink | Write OCSF rows to CSV (swap for real SIEM later) |

## Prerequisites

You need:

1. A Dagster+ deployment (US or EU)
2. A user token — Settings → Tokens → User Tokens in your deployment
3. Your GraphQL endpoint URL:
   - US: `https://<org>.dagster.cloud/<deployment>/graphql`
   - EU: `https://<org>.eu.dagster.cloud/<deployment>/graphql`

## Run

```bash
export DAGSTER_PLUS_USER_TOKEN='your-user-token'
export DAGSTER_PLUS_ENDPOINT_URL='https://my-org.dagster.cloud/prod/graphql'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dagster_plus_audit_demo.sh | bash
cd dagster-plus-audit-demo
uv run dg launch --assets '*'
```

Output: `/tmp/dagster_plus_audit_ocsf.csv` — every LOG_IN event from the last
24 hours, mapped to OCSF (`time`, `actor.user.name`, `activity_name`, `raw_data`).

## Audit-log filters

The Dagster+ audit-log API takes filters via the `filters` GraphQL input:

```yaml
event_types:
  - LOG_IN
  - DEPLOYMENT_CREATED
user_emails:
  - admin@my-org.com
deployment_names:
  - prod
```

Omit any field to "match all". Time window is enforced **client-side** via
`lookback_minutes` — the API itself uses cursor-based pagination.

## Shipping to a real SIEM

Once the CSV looks right, replace `dataframe_to_csv` with one of:

- `audit_logs_to_splunk` — HTTP Event Collector (HEC)
- `audit_logs_to_sentinel` — Microsoft Sentinel via Log Analytics ingestion
- `audit_logs_to_datadog_logs` — Datadog Logs API
- `audit_logs_to_sumo_logic` — Sumo Logic Hosted Collector
- `audit_logs_to_qradar` — IBM QRadar (LEEF/syslog)
- `audit_logs_to_chronicle` — Google Chronicle
- `audit_logs_to_elastic_security` — Elastic Security bulk index

Or skip the multi-component setup and use the single-YAML compound op job:

```yaml
type: dagster_component_templates.DagsterPlusToSiemJobComponent
attributes:
  job_name: dagster_plus_audit_to_splunk
  schedule: "*/15 * * * *"
  endpoint_url: https://my-org.dagster.cloud/prod/graphql
  user_token_env: DAGSTER_PLUS_USER_TOKEN
  event_type: audit_log
  event_types: [LOG_IN]
  lookback_minutes: 15
  normalize_to: ocsf
  sink: splunk
  sink_config:
    hec_url: https://splunk.acme.com:8088/services/collector
    hec_token_env: SPLUNK_HEC_TOKEN
    index: security
```

## A note on the GraphQL schema

The default audit-log query uses field names that match the live Dagster+ API
as of validation, but Dagster+ schema can evolve. If a field is renamed, set
the `query` field on either component to a query you've validated in the
GraphQL playground, and adjust `result_path` accordingly.
