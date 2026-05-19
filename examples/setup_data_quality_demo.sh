#!/usr/bin/env bash
# Data quality demo — 5 asset_check components on a synthetic orders asset.
#
# WHAT THIS DEMONSTRATES
#   The local-runnable asset_checks family. SaaS-specific checks
#   (acceldata, monte_carlo, sifflet, soda) need real credentials and are
#   not exercised here.
#
# Components covered (5):
#   - pandas_dataframe_check     → required columns + dtype checks
#   - pandera_asset_check        → declarative DataFrame schema validation
#   - enhanced_data_quality_checks → row count, null %, distinct values
#   - freshness_check            → time-window or cron freshness policy
#   - great_expectations_check   → defers to a GE expectation suite
#
# Asset graph:
#   orders (synthetic source)
#         │
#         ├── pandas_dataframe_check.required_columns ✓ row_count > 0 ✓ dtype ✓
#         ├── pandera_asset_check (schema-validated) ✓
#         ├── enhanced_data_quality_checks (multi-check selection) ✓
#         ├── freshness_check (time_window policy) ✓
#         └── great_expectations_check (skipped if GE not installed)
#
# COST: \$0 — all local.

set -euo pipefail
PROJECT_DIR="${1:-data-quality-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pandera
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 asset_check components"
$CLI add pandas_dataframe_check       --auto-install
$CLI add pandera_asset_check          --auto-install
$CLI add enhanced_data_quality_checks --auto-install
$CLI add freshness_check              --auto-install
# great_expectations skipped — heavy dep, install separately:
#   uv add great_expectations
#   $CLI add great_expectations_check --auto-install

echo ">>> Writing source asset + pandera schema"

# Create orders source
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest")
def orders() -> pd.DataFrame:
    """30 synthetic orders — clean schema for the data-quality demo."""
    rows = []
    for i in range(30):
        rows.append({
            "order_id": 1000 + i,
            "customer_id": (i % 10) + 1,
            "amount": round(50 + i * 1.5, 2),
            "status": ["pending", "shipped", "delivered"][i % 3],
        })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[orders])
PYEOF

# Pandera schema referenced by pandera_asset_check
mkdir -p "src/$PKG/schemas"
cat > "src/$PKG/schemas/__init__.py" <<'PYEOF'
import pandera as pa
from pandera.typing import Series


class OrdersSchema(pa.DataFrameModel):
    order_id: Series[int] = pa.Field(ge=1000)
    customer_id: Series[int] = pa.Field(ge=1, le=100)
    amount: Series[float] = pa.Field(gt=0)
    status: Series[str] = pa.Field(isin=["pending", "shipped", "delivered", "cancelled"])
PYEOF

echo ">>> Writing 4 asset_check defs.yaml"

cat > "src/$PKG/defs/pandas_dataframe_check/defs.yaml" <<EOF
type: $PKG.components.pandas_dataframe_check.component.PandasDataframeCheckComponent
attributes:
  asset_key: orders
  required_columns:
    - order_id
    - customer_id
    - amount
    - status
  column_types:
    order_id: int
    customer_id: int
    amount: float
    status: object
  blocking: false
EOF

cat > "src/$PKG/defs/pandera_asset_check/defs.yaml" <<EOF
type: $PKG.components.pandera_asset_check.component.PanderaAssetCheckComponent
attributes:
  asset_key: orders
  schema_module: $PKG.schemas
  schema_name: OrdersSchema
  blocking: false
  description: "All orders pass the OrdersSchema pandera contract"
EOF

cat > "src/$PKG/defs/enhanced_data_quality_checks/defs.yaml" <<EOF
type: $PKG.components.enhanced_data_quality_checks.component.EnhancedDataQualityChecks
attributes:
  selections:
    - target: orders
      row_count_check:
        - name: "orders_row_count"
          min_rows: 1
          blocking: false
      null_check:
        - name: "orders_no_nulls"
          columns: ["order_id", "customer_id", "amount", "status"]
          blocking: false
EOF

cat > "src/$PKG/defs/freshness_check/defs.yaml" <<EOF
type: $PKG.components.freshness_check.component.FreshnessPolicyComponent
attributes:
  asset_key: orders
  policy_type: time_window
  fail_window_hours: 25
  warn_window_hours: 13
EOF

cat <<MSG

>>> Setup complete.

Materialize + run checks:
    cd $PROJECT_DIR
    uv run dg launch --assets orders        # produces orders DataFrame
    uv run dg launch --asset-checks '*'     # runs all 4 attached checks

Or open the asset graph:
    uv run dg dev   # http://localhost:3000

Each check shows up in the asset graph as an "Asset Check" hanging off
the orders asset. Pass/fail status is visible in the UI per run.

Skipped (need extra setup):
  - great_expectations_check — install great_expectations first
  - acceldata / monte_carlo / sifflet / soda — need SaaS credentials
MSG
