#!/usr/bin/env bash
# Google Sheets demo — google_sheets_ingestion against a public sample sheet.
#
# WHAT THIS DEMONSTRATES
#   The google_sheets_ingestion component pulling rows from a public
#   Google Sheets spreadsheet via service-account auth. Validates:
#     - service-account JSON loads correctly
#     - Sheets API call returns rows (or 403s, which tells us we
#       need to share the sheet with the SA email)
#
# Asset graph:
#   class_data_sheet  ← google_sheets_ingestion (Class Data sample, public)
#         │
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  Path to service-account JSON.
#
# COST while running
#   \$0. Sheets API has a 300 req/min/project free tier.

set -euo pipefail
PROJECT_DIR="${1:-google-sheets-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON path."
  exit 1
fi
if [ ! -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  echo "ERROR: \$GOOGLE_APPLICATION_CREDENTIALS=$GOOGLE_APPLICATION_CREDENTIALS does not exist."
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas dlt google-auth google-api-python-client
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing google_sheets_ingestion"
$CLI add google_sheets_ingestion --auto-install

# Public "Class Data" sample sheet from the Sheets API quickstart
SHEET_ID="${GOOGLE_SHEET_ID:-1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms}"

mkdir -p "src/$PKG/defs/google_sheets_ingestion"
cat > "src/$PKG/defs/google_sheets_ingestion/defs.yaml" <<EOF
type: $PKG.components.google_sheets_ingestion.component.GoogleSheetsIngestionComponent
attributes:
  asset_name: class_data_sheet
  spreadsheet_id: "$SHEET_ID"
  sheet_names: ["Class Data"]
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  description: "Public Google Sheets 'Class Data' sample, fetched via service-account auth."
  group_name: google_sheets
EOF

cat <<MSG

>>> Setup complete (100% components, no custom Python in defs/).

Asset:
    class_data_sheet         ← google_sheets_ingestion (public Class Data)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    uv run dg dev   # http://localhost:3000

If the Sheets API returns 403, the service account does NOT have read
access to the sheet. Either:
  - Use a sheet you own, then share it with the SA email
    (test-496@servicepulse-490502.iam.gserviceaccount.com)
  - Set GOOGLE_SHEET_ID=<your_sheet_id> when re-running this script

The demo's default target is Google's public Class Data sample, which
SHOULD be readable. If not, swap to a sheet you own.
MSG
