#!/usr/bin/env bash
# window_calculation demo — exercises every supported window function.
#
# Synthetic stock-price ticks (3 symbols × 10 days). One window_calculation
# component computes ALL window operations in one pass:
#   row_number, rank, dense_rank, lag, lead, cumsum, moving_avg(3), moving_sum(5)
#
# Pipeline:
#   csv (synthetic) → window_calculation → CSV

set -euo pipefail
PROJECT_DIR="${1:-window-calc-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/window_demo
echo ">>> Generating synthetic stock prices"
uv run python - <<'PY'
import csv, random
random.seed(42)
rows = []
prices = {"AAPL": 180.0, "GOOG": 140.0, "MSFT": 380.0}
for day in range(1, 11):
    for sym, p in prices.items():
        # random walk
        prices[sym] = round(p + random.uniform(-3, 3), 2)
        rows.append({"symbol": sym, "trade_date": f"2025-04-{day:02d}", "close": prices[sym]})
with open("/tmp/window_demo/prices.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader(); w.writerows(rows)
print(f"wrote {len(rows)} rows")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components"
$CLI add file_ingestion   --auto-install
$CLI add window_calculation   --auto-install
$CLI add dataframe_to_csv     --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: stock_prices
  file_path: /tmp/window_demo/prices.csv
  description: 3 symbols × 10 days of synthetic close prices
  group_name: window_demo
EOF

cat > "src/$PKG/defs/window_calculation/defs.yaml" <<EOF
type: $PKG.components.window_calculation.component.WindowCalculationComponent
attributes:
  asset_name: stock_prices_with_windows
  upstream_asset_key: stock_prices
  partition_by:
    - symbol
  order_by:
    - trade_date
  operations:
    - {output: row_num,         func: row_number}
    - {output: close_rank,      func: rank,        column: close}
    - {output: close_drank,     func: dense_rank,  column: close}
    - {output: prev_close,      func: lag,         column: close, periods: 1}
    - {output: next_close,      func: lead,        column: close, periods: 1}
    - {output: cum_close,       func: cumsum,      column: close}
    - {output: rolling_3_avg,   func: moving_avg,  column: close, window: 3}
    - {output: rolling_5_sum,   func: moving_sum,  column: close, window: 5}
  group_name: window_demo
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: window_report
  upstream_asset_key: stock_prices_with_windows
  file_path: /tmp/window_demo/prices_with_windows.csv
  include_index: false
  group_name: window_demo
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Output:
    /tmp/window_demo/prices_with_windows.csv

Expected: 30 rows × 11 cols (symbol, trade_date, close + 8 window outputs).
Per-symbol: row_num goes 1..10. prev_close[day1]=NaN. cum_close strictly grows.
MSG
