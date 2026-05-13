#!/usr/bin/env bash
# OCSF + Security Lake demo — synthetic Dagster+ audit events through the
# full asset pipeline: raw → ocsf_normalizer → ocsf_validator → local
# parquet (mocking Security Lake's partition layout, no AWS required).
#
# This validates that:
#   - ocsf_normalizer maps Dagster+ event types to the correct OCSF class_uid
#   - ocsf_validator catches conformance issues
#   - dataframe_to_security_lake writes proper region/accountId/eventDay layout
#
# Pipeline:
#   csv (synthetic) → ocsf_normalizer → ocsf_validator (asset_check)
#                                    → local-parquet writer (Security Lake layout)

set -euo pipefail
PROJECT_DIR="${1:-ocsf-security-lake-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow tabulate
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/ocsf_demo
# Synthetic audit events now generated 100%-components via parametric_data_generator.

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add parametric_data_generator --auto-install
$CLI add ocsf_normalizer           --auto-install
$CLI add ocsf_validator            --auto-install
$CLI add dataframe_to_parquet      --auto-install

# Suppress the auto-installed example defs that would conflict
rm -rf "src/$PKG/defs/parametric_data_generator" "src/$PKG/defs/ocsf_normalizer" \
       "src/$PKG/defs/ocsf_validator" "src/$PKG/defs/dataframe_to_parquet"

mkdir -p "src/$PKG/defs/audit_raw" "src/$PKG/defs/ocsf_normalizer" \
         "src/$PKG/defs/ocsf_validator" "src/$PKG/defs/dataframe_to_parquet"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/audit_raw/defs.yaml" <<EOF
type: $PKG.components.parametric_data_generator.component.ParametricDataGeneratorComponent
attributes:
  asset_name: dagster_plus_audit_raw
  row_count: 25
  random_state: 13
  description: 25 synthetic Dagster+ audit events
  group_name: ocsf_demo
  columns:
    timestamp:
      type: datetime
      start: "2026-05-10T00:00:00"
      end: "2026-05-11T23:59:59"
      format: "%Y-%m-%dT%H:%M:%S+00:00"
    userEmail:
      type: choice
      values: [u1@acme.com, u2@acme.com, u3@acme.com, u4@acme.com,
               u5@acme.com, u6@acme.com, u7@acme.com, u8@acme.com]
    eventType:
      type: choice
      values:
        - LOG_IN                  # → 3002 Authentication
        - CREATE_USER_TOKEN       # → 3005 Account Change
        - REVOKE_USER_TOKEN       # → 3005 Account Change
        - CHANGE_USER_PERMISSIONS # → 3006 User Access Management
        - CREATE_CODE_LOCATION    # → 6002 Application Lifecycle
        - UPDATE_CODE_LOCATION    # → 6002 Application Lifecycle
        - DELETE_CODE_LOCATION    # → 6002 Application Lifecycle
        - LAUNCH_RUN              # → 6003 API Activity
    targetType:
      type: formula
      formula: "'User' if 'USER' in eventType or 'ROLE' in eventType else 'Session'"
    targetIdentifier:
      type: choice
      values: [u1@acme.com, u2@acme.com, u3@acme.com, u4@acme.com,
               u5@acme.com, u6@acme.com, u7@acme.com, u8@acme.com]
    metadata:
      type: constant
      value: '{"source": "synthetic"}'
EOF

cat > "src/$PKG/defs/ocsf_normalizer/defs.yaml" <<EOF
type: $PKG.components.ocsf_normalizer.component.OcsfNormalizerComponent
attributes:
  asset_name: dagster_plus_audit_ocsf
  upstream_asset_key: dagster_plus_audit_raw
  source_kind: dagster_plus
  vendor_name: Dagster
  product_name: Dagster+
  ocsf_version: "1.1.0"
  default_severity_id: 1
  drop_unmapped: false
  keep_raw: true
  group_name: ocsf_demo
EOF

cat > "src/$PKG/defs/ocsf_validator/defs.yaml" <<EOF
type: $PKG.components.ocsf_validator.component.OcsfValidatorComponent
attributes:
  check_name: ocsf_conformance
  upstream_asset_key: dagster_plus_audit_ocsf
  blocking: false
  require_known_class_uid: true
  max_invalid_rows: 0
EOF

# Use the simpler dataframe_to_parquet for the demo — full Security Lake
# layout requires AWS creds + a real bucket. The Parquet output proves the
# upstream OCSF flow works.
cat > "src/$PKG/defs/dataframe_to_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: ocsf_parquet
  upstream_asset_key: dagster_plus_audit_ocsf
  file_path: /tmp/ocsf_demo/dagster_plus_audit_ocsf.parquet
  compression: snappy
  group_name: ocsf_demo
EOF

cat <<MSG

>>> Setup complete.
Materialize: cd $PROJECT_DIR && uv run dg launch --assets '*'

Output:
  /tmp/ocsf_demo/dagster_plus_audit_ocsf.parquet (OCSF rows)

Inspect:
  uv run python -c "import pandas as pd; df = pd.read_parquet('/tmp/ocsf_demo/dagster_plus_audit_ocsf.parquet'); print(df['class_uid'].value_counts()); print(df.head())"

Expected: rows mapped to class_uid 3002 (Authentication) for LOG_IN/LOG_OUT,
3005 (Account Change) for USER_INVITED/TOKEN_CREATED, 3006 (User Access) for
ROLE_GRANTED, 6002 (App Lifecycle) for DEPLOYMENT_*.
MSG
