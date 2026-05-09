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
#                  ├── headcount_by_dept       ← pandas
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
$CLI add hris_normalizer  --auto-install
$CLI add dataframe_to_csv --auto-install

# 1) Synthetic raw HRIS export — vendor-y schema
mkdir -p "src/$PKG/defs/employees_raw"
cat > "src/$PKG/defs/employees_raw/definitions.py" <<'PYEOF'
"""Synthetic vendor HRIS export — 20 rows, vendor-y column names."""
import random
import datetime as dt
import pandas as pd
import dagster as dg


_FIRST = ["Maya","Kenji","Aisha","Diego","Priya","Liam","Mei","Jonas","Zara","Felix",
         "Noor","Hugo","Riya","Marco","Yui","Theo","Layla","Anand","Sofia","Mateo"]
_LAST  = ["Patel","Tanaka","Khan","Garcia","Singh","Murphy","Wong","Becker","Ali","Nilsson",
          "Choudhury","Bauer","Verma","Romano","Sato","Walker","Hassan","Rao","Costa","Lopez"]
_DEPT  = ["Engineering","Sales","Marketing","Customer Success","People","Finance","Engineering","Sales"]
_LOC   = ["NYC","SF","London","Berlin","Remote-US","Remote-EMEA","Sydney"]


@dg.asset(
    key=dg.AssetKey(["employees_raw"]),
    description="Synthetic vendor HRIS export (20 rows, vendor-y column names like emp_id / given_name / status='A').",
    group_name="ingest",
    kinds={"pandas"},
)
def employees_raw() -> pd.DataFrame:
    random.seed(42)
    rows = []
    today = dt.date.today()
    for i in range(20):
        first = _FIRST[i]
        last = _LAST[i]
        emp_id = f"E{1000+i:04d}"
        hire = today - dt.timedelta(days=random.randint(30, 365*8))
        terminated = random.random() < 0.20
        term_date = (hire + dt.timedelta(days=random.randint(60, 365*5))) if terminated else None
        rows.append({
            "emp_id":         emp_id,
            "given_name":     first,
            "family_name":    last,
            "work_email":     f"{first.lower()}.{last.lower()}@acme.example",
            "supervisor_id":  None if i % 5 == 0 else f"E{1000 + (i % 5):04d}",
            "dept":           _DEPT[i % len(_DEPT)],
            "position":       random.choice(["Manager","IC","Senior IC","Lead","Director"]),
            "office":         random.choice(_LOC),
            "country_iso":    "US" if "NYC" in _LOC[i % len(_LOC)] or "SF" in _LOC[i % len(_LOC)] else "GB",
            "status":         "T" if terminated else random.choice(["A","A","A","L"]),
            "emp_type":       random.choice(["REG-FT","REG-PT","CONTRACT","REG-FT","REG-FT"]),
            "start_date":     hire.isoformat(),
            "end_date":       term_date.isoformat() if term_date else None,
        })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[employees_raw])
PYEOF

# 2) Normalizer config
mkdir -p "src/$PKG/defs/hris_normalizer"
cat > "src/$PKG/defs/hris_normalizer/defs.yaml" <<EOF
type: $PKG.components.hris_normalizer.component.HrisNormalizerComponent
attributes:
  asset_name: employees_normalized
  upstream_asset_key: employees_raw

  column_map:
    employee_id:         emp_id
    email:               work_email
    first_name:          given_name
    last_name:           family_name
    manager_employee_id: supervisor_id
    department:          dept
    job_title:           position
    location:            office
    country:             country_iso
    employment_status:   status
    employment_type:     emp_type
    hire_date:           start_date
    termination_date:    end_date

  status_map:
    A: active
    T: terminated
    L: on_leave

  type_map:
    REG-FT:    full_time
    REG-PT:    part_time
    CONTRACT:  contractor

  derive_full_name: true
  compute_tenure: true
  derive_is_active: true

  description: Synthetic vendor data normalized to the canonical HR schema.
  group_name: hris
EOF

# 3) Downstream HR analytics
mkdir -p "src/$PKG/defs/hr_metrics"
cat > "src/$PKG/defs/hr_metrics/definitions.py" <<'PYEOF'
"""Headcount + tenure analytics from the canonical HR table."""
import pandas as pd
import dagster as dg
from dagster import AssetExecutionContext, AssetIn


@dg.asset(
    key=dg.AssetKey(["headcount_by_dept"]),
    description="Active vs total headcount per department, plus average tenure days.",
    group_name="analytics",
    kinds={"pandas"},
    ins={"employees_normalized": AssetIn(key=dg.AssetKey(["employees_normalized"]))},
)
def headcount_by_dept(employees_normalized: pd.DataFrame) -> pd.DataFrame:
    df = employees_normalized
    if df.empty:
        return pd.DataFrame()
    grouped = df.groupby("department", dropna=True).agg(
        total_employees=("employee_id", "count"),
        active_employees=("is_active", "sum"),
        avg_tenure_days=("tenure_days", lambda s: round(float(s.dropna().mean()), 1) if s.dropna().any() else None),
        terminated_count=("employment_status", lambda s: (s == "terminated").sum()),
    ).reset_index().sort_values("total_employees", ascending=False)
    grouped["active_pct"] = (grouped["active_employees"] / grouped["total_employees"] * 100).round(1)
    return grouped


defs = dg.Definitions(assets=[headcount_by_dept])
PYEOF

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
    employees_raw          (synthetic 20-row vendor export)
          │
          └── employees_normalized   ← hris_normalizer (canonical schema)
                  │
                  ├── headcount_by_dept            ← pandas (active vs total per dept)
                  └── employees_normalized_csv     ← /tmp/employees_normalized.csv

Materialize all four:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    cat /tmp/employees_normalized.csv
MSG
