#!/usr/bin/env bash
# Google Calendar demo — list events from a shared calendar → CSV.
#
# WHAT THIS DEMONSTRATES
#   The new google_calendar_ingestion component pulling real events
#   from a Google Calendar via service-account auth, then a downstream
#   pandas summary asset, then a CSV sink writing $PROJECT_ABS/out/calendar_events.csv.
#
# Asset graph:
#   upcoming_events     ← google_calendar_ingestion
#         │
#         └── upcoming_events_csv  ← dataframe_to_csv ($PROJECT_ABS/out/calendar_events.csv)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  Path to service-account JSON.
#                                   The SA must have been shared on the
#                                   target calendar (Calendar settings →
#                                   Share with specific people → SA email
#                                   → "See all event details").
#   GOOGLE_CALENDAR_ID              The owner's email or a named cal ID.
#
# COST while running
#   \$0. Calendar API is free up to 1M req/day.

set -euo pipefail
PROJECT_DIR="${1:-google-calendar-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"
  exit 1
fi
if [ -z "${GOOGLE_CALENDAR_ID:-}" ]; then
  echo "ERROR: set GOOGLE_CALENDAR_ID (owner's email, e.g. you@gmail.com)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-api-python-client
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing google_calendar_ingestion + dataframe_to_csv + dataframe_to_bigquery"
$CLI add google_calendar_ingestion --auto-install
$CLI add dataframe_to_csv          --auto-install
$CLI add dataframe_to_bigquery     --auto-install

mkdir -p "src/$PKG/defs/google_calendar_ingestion"
cat > "src/$PKG/defs/google_calendar_ingestion/defs.yaml" <<EOF
type: $PKG.components.google_calendar_ingestion.component.GoogleCalendarIngestionComponent
attributes:
  asset_name: upcoming_events
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  calendar_id: "$GOOGLE_CALENDAR_ID"
  days_ahead: 30
  max_results: 250
  single_events: true
  description: Upcoming events from a shared Google Calendar.
  group_name: calendar
EOF

# ─── CSV sink ───────────────────────────────────────────────────────────
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: upcoming_events_csv
  upstream_asset_key: upcoming_events
  file_path: out/calendar_events.csv
  include_index: false
  description: Local CSV export of upcoming events (works on local dev — for Dagster+ cloud use the BQ sink below).
  group_name: sink
EOF

# ─── Cloud-friendly BigQuery sink (works on Dagster+ Cloud) ─────────────
mkdir -p "src/$PKG/defs/dataframe_to_bigquery"
cat > "src/$PKG/defs/dataframe_to_bigquery/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_bigquery.component.DataframeToBigqueryComponent
attributes:
  asset_name: upcoming_events_bq
  upstream_asset_key: upcoming_events
  table_id: ${BQ_TARGET_TABLE:-servicepulse-490502.dagster_demo.calendar_events}
  write_disposition: WRITE_TRUNCATE
  credentials_env_var: GOOGLE_APPLICATION_CREDENTIALS
  location: US
  description: Cloud-friendly sink — lands the calendar events as a BigQuery table.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    upcoming_events     ← google_calendar_ingestion ($GOOGLE_CALENDAR_ID)
          │
          ├── upcoming_events_csv  ← dataframe_to_csv  ($PROJECT_ABS/out/calendar_events.csv — local dev)
          └── upcoming_events_bq   ← dataframe_to_bigquery  (cloud-friendly: lands a BQ table)

Materialize all three:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

After running:
    # local sink
    cat $PROJECT_ABS/out/calendar_events.csv
    # cloud sink — query the BQ table the SA wrote
    bq query --nouse_legacy_sql 'SELECT COUNT(*) FROM dagster_demo.calendar_events'

For Dagster+ Cloud deployments, drop the local CSV asset; the BigQuery sink
is the cloud-friendly path. Override the target table via:
    BQ_TARGET_TABLE=my-project.my_dataset.my_table ./setup_google_calendar_demo.sh
MSG
