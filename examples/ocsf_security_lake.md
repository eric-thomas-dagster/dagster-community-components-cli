# OCSF + Security Lake demo

Synthetic Dagster+ audit events through the **full asset pipeline**: raw → OCSF
normalize → conformance check → Parquet (mocking AWS Security Lake's layout, no
AWS required).

Validates:
- `ocsf_normalizer` correctly maps Dagster+ event types to OCSF class_uid /
  category_uid / activity_id (Authentication=3002, Account Change=3005, User
  Access=3006, App Lifecycle=6002).
- `ocsf_validator` (asset_check) catches conformance issues — required fields,
  severity range, known class_uid set.
- `dataframe_to_parquet` writes the OCSF rows.

```
csv (synthetic) → ocsf_normalizer → ocsf_validator (asset_check)
                                  → dataframe_to_parquet
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | 25 synthetic Dagster+ audit events |
| 2 | `ocsf_normalizer` | transformation | Map source events → OCSF v1.1 |
| 3 | `ocsf_validator` | check | Asset check: OCSF v1.x conformance |
| 4 | `dataframe_to_parquet` | sink | Write OCSF rows (snappy Parquet) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_ocsf_security_lake_demo.sh | bash
cd ocsf-security-lake-demo && uv run dg launch --assets '*'
```

## Expected mapping

The synthetic data uses 7 Dagster+ event types; each maps to a specific OCSF class:

| event_type | class_uid | category_uid | OCSF class |
|---|---|---|---|
| LOG_IN, LOG_OUT | 3002 | 3 | Authentication |
| USER_INVITED, TOKEN_CREATED | 3005 | 3 | Account Change |
| ROLE_GRANTED | 3006 | 3 | User Access Management |
| DEPLOYMENT_CREATED, DEPLOYMENT_UPDATED | 6002 | 6 | Application Lifecycle |

Inspect the result:

```bash
uv run python -c "
import pandas as pd
df = pd.read_parquet('/tmp/ocsf_demo/dagster_plus_audit_ocsf.parquet')
print(df.groupby('raw_event_type')['class_uid'].first())
"
```

## What this isn't

The demo writes Parquet to `/tmp` instead of an actual Security Lake bucket so it
runs offline. To target the real AWS Security Lake layout, swap
`dataframe_to_parquet` for `dataframe_to_security_lake` — see
`dagster_plus_security_lake_demo` for that flow.
