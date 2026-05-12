#!/usr/bin/env bash
# detect_changes demo — diff today vs yesterday, classify rows as
# insert / update / delete / unchanged.
#
# Pipeline:
#   csv (yesterday) ─┐
#                    ├─→ detect_changes → CSV
#   csv (today)     ─┘

set -euo pipefail
PROJECT_DIR="${1:-detect-changes-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/diff_demo
# Yesterday: 4 customers
cat > /tmp/diff_demo/customers_yesterday.csv <<'EOF'
customer_id,plan_tier,country
C001,free,US
C002,pro,UK
C003,free,DE
C004,enterprise,JP
EOF

# Today: C002 unchanged, C003 upgraded, C004 missing (delete), C005 net-new
cat > /tmp/diff_demo/customers_today.csv <<'EOF'
customer_id,plan_tier,country
C001,free,US
C002,pro,UK
C003,pro,DE
C005,free,FR
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add csv_file_ingestion --auto-install
mkdir -p "src/$PKG/defs/csv_today"  # only needs defs.yaml; component code is in components/csv_file_ingestion/
$CLI add detect_changes     --auto-install
$CLI add dataframe_to_csv   --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: customers_yesterday
  file_path: /tmp/diff_demo/customers_yesterday.csv
  description: Yesterday snapshot
  group_name: diff_demo
EOF

cat > "src/$PKG/defs/csv_today/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: customers_today
  file_path: /tmp/diff_demo/customers_today.csv
  description: Today snapshot
  group_name: diff_demo
EOF

cat > "src/$PKG/defs/detect_changes/defs.yaml" <<EOF
type: $PKG.components.detect_changes.component.DetectChangesComponent
attributes:
  asset_name: customer_changes
  upstream_asset_key: customers_today
  upstream_prior_key: customers_yesterday
  business_key_columns:
    - customer_id
  compare_columns:
    - plan_tier
    - country
  include_unchanged: true
  change_type_column: change_type
  group_name: diff_demo
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: changes_report
  upstream_asset_key: customer_changes
  file_path: /tmp/diff_demo/changes.csv
  include_index: false
  group_name: diff_demo
EOF

cat <<MSG

>>> Setup complete.
Materialize: cd $PROJECT_DIR && uv run dg launch --assets '*'

Expected (5 rows):
  C001 unchanged, C002 unchanged, C003 update, C004 delete, C005 insert
MSG
