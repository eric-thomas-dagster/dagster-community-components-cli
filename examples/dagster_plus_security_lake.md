# Dagster+ Audit → AWS Security Lake demo

**This is a Dagster+ demo** — pulls real audit-log entries from your Dagster+
deployment via GraphQL, normalizes to OCSF v1.1, optionally lands in AWS
Security Lake. Lineage tracked end-to-end.

If you don't have Dagster+ creds and just want to see the OCSF normalizer
+ validator working, see [OCSF + Security Lake](ocsf_security_lake.md) — same
asset pipeline driven by synthetic data.

```
dagster_plus_audit_log_ingestion → ocsf_normalizer
                                      ├─→ ocsf_validator (asset_check)
                                      └─→ dataframe_to_security_lake (or local Parquet)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `dagster_plus_audit_log_ingestion` | ingestion | GraphQL pull from `auditLog.auditLogEntries` |
| 2 | `ocsf_normalizer` | transformation | Map Dagster+ event types → OCSF v1.1 |
| 3 | `ocsf_validator` | check | OCSF conformance (required fields, severity, class_uid) |
| 4 | `dataframe_to_parquet` (local) **or** `dataframe_to_security_lake` (AWS) | sink | Write OCSF Parquet |

## Prerequisites

- Dagster+ user token: Settings → Tokens → User Tokens
- Endpoint: `https://<org>.dagster.cloud/<deployment>/graphql` (or `<org>.eu.dagster.cloud` for EU)
- For Security Lake mode: AWS creds, custom-source bucket, AWS account ID

## Run (local Parquet — no AWS required)

```bash
export DAGSTER_PLUS_USER_TOKEN='your-user-token'
export DAGSTER_PLUS_ENDPOINT_URL='https://my-org.dagster.cloud/prod/graphql'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dagster_plus_security_lake_demo.sh | bash
cd dagster-plus-security-lake-demo && uv run dg launch --assets '*'
```

Output: `/tmp/dagster_plus_audit_ocsf.parquet`

## Run (AWS Security Lake)

```bash
bash setup_dagster_plus_security_lake_demo.sh dagster-plus-security-lake-demo security_lake
# Edit src/.../dataframe_to_security_lake/defs.yaml:
#   - bucket: aws-security-data-lake-us-east-1-<id>
#   - account_id: <aws-account-id>
cd dagster-plus-security-lake-demo && uv run dg launch --assets '*'
```

Output: `s3://<bucket>/ext-dagster-plus-audit/region=<r>/accountId=<a>/eventDay=<YYYYMMDD>/*.parquet`

## What this validated

This demo is the reason we found 7 bugs in the Dagster+ pull components:
- Wrong auth header (`Dagster-Cloud-User-Token` → `Dagster-Cloud-Api-Token`)
- Wrong query nesting (`auditLogs` → `auditLog { auditLogEntries }`)
- Wrong field names (`userEmail` → `authorUserEmail`, `metadata` → `eventMetadata`)
- Missing required server-side time filter (`afterDatetime`/`beforeDatetime`)
- Wrong pagination (cursor = last entry's `id`, not a separate field)
- OCSF mapping rebuilt against the live 43-value AuditLogEventType enum
- OCSF normalizer auto-detect now includes `authorUserEmail`

End-to-end run against a real Dagster+ deployment pulled 176 audit-log entries
correctly.
