#!/usr/bin/env bash
# HRIS normalizer demo — vendor-agnostic employee schema mapping.
#
# WHAT THIS DEMONSTRATES
#   The new hris_normalizer component mapping a synthetic vendor-export
#   CSV (with vendor-y column names: emp_id, given_name, status='A', etc.)
#   to the canonical employee schema. Downstream pandas asset reports
#   headcount by department; CSV sink for inspection.
#
# Asset graph:
#   employees_raw   ← custom asset (synthetic 20-employee CSV)
#         │
#         └── employees_normalized  ← hris_normalizer
#                  │
#                  └── employees_normalized_csv ← dataframe_to_csv
#
# REQUIRED ENV VAR
#   None. Pure local synthetic data.
#
# COST while running
#   \$0.

set -euo pipefail
PROJECT_DIR="${1:-hris-normalizer-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing hris_normalizer + dataframe_to_csv"
$CLI add synthetic_data_generator --auto-install
$CLI add hris_normalizer          --auto-install
$CLI add dataframe_to_csv         --auto-install

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"

# 1) Synthetic raw HRIS export — synthetic_data_generator (employees schema)
mkdir -p "src/$PKG/defs/employees_raw"
cat > "src/$PKG/defs/employees_raw/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: employees_raw
  schema_type: employees
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

# 2) Normalizer config — map the schema's vendor-y columns + values to canonical
mkdir -p "src/$PKG/defs/hris_normalizer"
cat > "src/$PKG/defs/hris_normalizer/defs.yaml" <<EOF
type: $PKG.components.hris_normalizer.component.HrisNormalizerComponent
attributes:
  asset_name: employees_normalized
  upstream_asset_key: employees_raw

  column_map:
    employee_id:       employee_number
    email:             work_email
    first_name:        first_name
    last_name:         last_name
    department:        department
    employment_status: status
    employment_type:   employment_type
    hire_date:         hire_dt

  case_insensitive_map: true

  # Generic value_maps — keys lowercased before lookup
  value_maps:
    employment_status:
      active:     active
      terminated: terminated
      term:       terminated
      "on leave": on_leave
    employment_type:
      ft:          full_time
      "full-time": full_time
      full_time:   full_time
      pt:          part_time
      "part-time": part_time
      part_time:   part_time
      contractor:  contractor
      intern:      intern

  derive_full_name: true
  compute_tenure: true
  derive_is_active: true

  description: Synthetic vendor data normalized to the canonical HR schema.
  group_name: hris
EOF

# 4) CSV sink
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: employees_normalized_csv
  upstream_asset_key: employees_normalized
  file_path: /tmp/employees_normalized.csv
  include_index: false
  description: CSV export of the normalized employee table.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    employees_raw          ← synthetic_data_generator (employees, 20 rows)
          │
          └── employees_normalized   ← hris_normalizer (canonical schema)
                  │
                  └── employees_normalized_csv     ← /tmp/employees_normalized.csv

Materialize all three:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/employees_normalized.csv
MSG
