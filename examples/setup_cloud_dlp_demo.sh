#!/usr/bin/env bash
# Cloud DLP demo — both flavors of PII detection on synthetic support tickets.
#
# 100% components — no custom Python in defs/.
#
#   synthetic_data_generator (support_tickets schema, with embedded PII)
#         │
#         ├── tickets_dlp_scanned     ← cloud_dlp_inspect_asset
#         │                            (adds dlp_finding_count, dlp_infotypes, dlp_findings)
#         │
#         └── [asset check]           ← cloud_dlp_pii_check
#                                       (fails on emails/phones at POSSIBLE)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#
# REQUIRED API
#   Cloud DLP  https://console.cloud.google.com/apis/library/dlp.googleapis.com
#
# REQUIRED IAM
#   roles/dlp.user
#
# COST: < $0.01.

set -euo pipefail
PROJECT_DIR="${1:-cloud-dlp-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy google-auth google-cloud-dlp
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator   --auto-install 2>&1 | tail -2
$CLI add cloud_dlp_inspect_asset    --auto-install 2>&1 | tail -2
$CLI add cloud_dlp_pii_check        --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import CloudDlpInspectAssetComponent
__all__ = ["CloudDlpInspectAssetComponent"]' > "src/$PKG/components/cloud_dlp_inspect_asset/__init__.py"
echo 'from .component import CloudDlpPiiCheckComponent
__all__ = ["CloudDlpPiiCheckComponent"]' > "src/$PKG/components/cloud_dlp_pii_check/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/cloud_dlp_inspect_asset" "src/$PKG/defs/cloud_dlp_pii_check"

# 1) Synthetic support tickets with embedded PII (names, emails, phones, CC fragments)
mkdir -p "src/$PKG/defs/support_tickets"
cat > "src/$PKG/defs/support_tickets/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

# 2) Inspect asset — augments DataFrame with finding columns
mkdir -p "src/$PKG/defs/tickets_dlp_scanned"
cat > "src/$PKG/defs/tickets_dlp_scanned/defs.yaml" <<EOF
type: $PKG.components.cloud_dlp_inspect_asset.component.CloudDlpInspectAssetComponent
attributes:
  asset_name: tickets_dlp_scanned
  upstream_asset_key: support_tickets
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  text_columns: [ticket_text]
  info_types: [EMAIL_ADDRESS, PHONE_NUMBER, CREDIT_CARD_NUMBER, PERSON_NAME]
  min_likelihood: POSSIBLE
  include_quote: false
  group_name: compliance
EOF

# 3) PII gate check — fails on emails/phones at POSSIBLE threshold
mkdir -p "src/$PKG/defs/tickets_pii_check"
cat > "src/$PKG/defs/tickets_pii_check/defs.yaml" <<EOF
type: $PKG.components.cloud_dlp_pii_check.component.CloudDlpPiiCheckComponent
attributes:
  asset_key: support_tickets
  check_name: no_contact_pii_in_tickets
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  text_columns: [ticket_text]
  forbidden_info_types: [EMAIL_ADDRESS, PHONE_NUMBER]
  min_likelihood: POSSIBLE
  max_allowed_findings: 0
  severity: ERROR
  blocking: false   # set true to block downstream materializations on failure
EOF

cat <<MSG

>>> Setup complete (100% components, no custom Python in defs/).

Asset graph:
    support_tickets              ← synthetic_data_generator (support_tickets, 20 rows)
          │
          ├── tickets_dlp_scanned  ← cloud_dlp_inspect_asset
          │
          └── [asset check] no_contact_pii_in_tickets  ← cloud_dlp_pii_check

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected: tickets_dlp_scanned materializes; the check FAILS (synthetic tickets
embed names + emails + phones — exactly what the check forbids).
MSG
