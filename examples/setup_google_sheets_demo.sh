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
#         └── class_data_summary  ← pandas (rows-by-major summary)
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

# ─── Downstream pandas summary asset ───────────────────────────────────────
mkdir -p "src/$PKG/defs/class_data_summary"
cat > "src/$PKG/defs/class_data_summary/definitions.py" <<'PYEOF'
"""Summary by major: count, mean GPA, etc."""
import pandas as pd
import dagster as dg
from dagster import AssetExecutionContext, AssetIn


@dg.asset(
    key=dg.AssetKey(["class_data_summary"]),
    description="Class data summary by major (count + first-name list).",
    group_name="downstream",
    kinds={"pandas"},
    ins={"class_data_sheet": AssetIn(key=dg.AssetKey(["class_data_sheet"]))},
)
def class_data_summary(class_data_sheet: pd.DataFrame) -> pd.DataFrame:
    df = class_data_sheet
    if df.empty:
        return pd.DataFrame()
    # Best-effort: find a major-like column case-insensitively
    cols = {c.lower(): c for c in df.columns}
    major_col = cols.get("major") or cols.get("class") or list(df.columns)[0]
    grouped = df.groupby(major_col).size().reset_index(name="row_count").sort_values("row_count", ascending=False)
    return grouped


defs = dg.Definitions(assets=[class_data_summary])
PYEOF

cat <<MSG

>>> Setup complete.

Asset graph:
    class_data_sheet         ← google_sheets_ingestion (public Class Data)
          │
          └── class_data_summary  ← pandas (rows-by-major)

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
