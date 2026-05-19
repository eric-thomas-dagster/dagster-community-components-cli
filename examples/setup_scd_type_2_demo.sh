#!/usr/bin/env bash
# SCD Type 2 demo — Slowly Changing Dimension Type 2 (history-tracking).
#
# Validates the scd_type_2 transform end-to-end using two CSV snapshots:
#   - customers_yesterday.csv (today's "current" dimension)
#   - customers_today.csv     (incoming snapshot with changes)
#
# Expected behavior after the merge:
#   - C001: unchanged          → 1 row, is_current=True
#   - C002: plan_tier upgraded → 2 rows (1 expired, 1 new is_current)
#   - C003: removed in today   → 1 row, is_current=True (kept as-is)
#   - C004: brand-new in today → 1 row, is_current=True
#
# Pipeline (3 components):
#   csv (yesterday) ─┐
#                    ├─→ scd_type_2 → CSV
#   csv (today)     ─┘

set -euo pipefail
PROJECT_DIR="${1:-scd-type-2-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating synthetic snapshots"
mkdir -p /tmp/scd_demo

# Yesterday — pre-existing SCD2 dimension
cat > /tmp/scd_demo/customers_yesterday.csv <<'EOF'
customer_id,name,plan_tier,billing_address,effective_from,effective_to,is_current
C001,Alice,free,"123 Main St",2025-01-01,,True
C002,Bob,free,"456 Oak Ave",2025-01-15,,True
C003,Charlie,pro,"789 Pine Rd",2025-02-01,,True
EOF

# Today — incoming. C002 upgraded, C003 missing, C004 net-new
cat > /tmp/scd_demo/customers_today.csv <<'EOF'
customer_id,name,plan_tier,billing_address
C001,Alice,free,"123 Main St"
C002,Bob,pro,"456 Oak Ave"
C004,Diana,enterprise,"321 Elm St"
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add file_ingestion --auto-install
mkdir -p "src/$PKG/defs/csv_today"  # only needs defs.yaml; component code is in components/file_ingestion/
$CLI add scd_type_2         --auto-install
$CLI add dataframe_to_csv   --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: customers_yesterday
  file_path: /tmp/scd_demo/customers_yesterday.csv
  description: Pre-existing SCD2 dimension snapshot
  group_name: scd_demo
EOF

cat > "src/$PKG/defs/csv_today/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: customers_today
  file_path: /tmp/scd_demo/customers_today.csv
  description: Incoming snapshot — C002 upgraded, C003 missing, C004 new
  group_name: scd_demo
EOF

cat > "src/$PKG/defs/scd_type_2/defs.yaml" <<EOF
type: $PKG.components.scd_type_2.component.ScdType2Component
attributes:
  asset_name: customers_scd2
  upstream_asset_key: customers_today
  upstream_target_key: customers_yesterday
  business_key_columns:
    - customer_id
  track_columns:
    - plan_tier
    - billing_address
  effective_from_column: effective_from
  effective_to_column: effective_to
  is_current_column: is_current
  group_name: scd_demo
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: scd2_report
  upstream_asset_key: customers_scd2
  file_path: /tmp/scd_demo/customers_scd2_output.csv
  include_index: false
  group_name: scd_demo
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Output:
    /tmp/scd_demo/customers_scd2_output.csv

Expected rows: 5 (C001 unchanged, C002 expired+new, C003 unchanged-because-missing, C004 new)
MSG
