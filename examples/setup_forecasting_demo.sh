#!/usr/bin/env bash
# Forecasting demo — 3 time-series components.
#
# WHAT THIS DEMONSTRATES
#   ARIMA + ETS forecasting on synthetic monthly/weekly data, plus
#   create_samples (the train/val/test 3-way splitter alternative to
#   train_test_splitter when you want a single tagged column).
#
# Asset graph:
#   monthly_revenue  (synthetic 36 months)
#   weekly_orders    (synthetic 52 weeks)
#   churn_dataset    (synthetic 100-row labeled customer churn)
#         │
#         ├── monthly_revenue_forecast   ← arima_forecast (12 periods ahead)
#         ├── weekly_orders_forecast     ← ets_forecast   (8 periods ahead)
#         └── churn_dataset_split        ← create_samples (train/val/test)
#
# COST: \$0 — local statsmodels + pandas.

set -euo pipefail
PROJECT_DIR="${1:-forecasting-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy statsmodels
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 time-series components"
$CLI add arima_forecast    --auto-install
$CLI add ets_forecast      --auto-install
$CLI add create_samples    --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import numpy as np
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest", description="36 months of synthetic retail revenue with seasonality + trend.")
def monthly_revenue() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    months = pd.date_range("2022-01-01", periods=36, freq="MS")
    trend = np.linspace(1000, 1500, 36)
    seasonal = 200 * np.sin(2 * np.pi * np.arange(36) / 12)
    noise = rng.normal(0, 50, 36)
    return pd.DataFrame({
        "sale_date": months.strftime("%Y-%m-%d"),
        "revenue": (trend + seasonal + noise).round(2),
    })


@dg.asset(group_name="ingest", description="52 weeks of synthetic e-commerce order counts.")
def weekly_orders() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    weeks = pd.date_range("2024-01-01", periods=52, freq="W-MON")
    trend = np.linspace(50, 80, 52)
    seasonal = 15 * np.sin(2 * np.pi * np.arange(52) / 13)  # quarterly cycle
    noise = rng.normal(0, 5, 52)
    return pd.DataFrame({
        "week_start": weeks.strftime("%Y-%m-%d"),
        "order_count": np.maximum((trend + seasonal + noise).round(), 0).astype(int),
    })


@dg.asset(group_name="ingest", description="100 customers with binary churn label for create_samples.")
def churn_dataset() -> pd.DataFrame:
    rng = np.random.default_rng(13)
    n = 100
    return pd.DataFrame({
        "customer_id": range(1, n + 1),
        "tenure_months": rng.integers(1, 60, n),
        "monthly_charge": rng.gamma(2, 25, n).round(2),
        "support_tickets": rng.poisson(2, n),
        "churn_label": rng.choice([0, 1], n, p=[0.7, 0.3]),
    })


defs = dg.Definitions(assets=[monthly_revenue, weekly_orders, churn_dataset])
PYEOF

echo ">>> Writing 3 time-series defs.yaml"

cat > "src/$PKG/defs/arima_forecast/defs.yaml" <<EOF
type: $PKG.components.arima_forecast.component.ArimaForecastComponent
attributes:
  asset_name: monthly_revenue_forecast
  upstream_asset_key: monthly_revenue
  date_column: sale_date
  value_column: revenue
  forecast_periods: 12
  order: [1, 1, 1]
  seasonal_order: [1, 1, 1, 12]
  group_name: forecasts
EOF

cat > "src/$PKG/defs/ets_forecast/defs.yaml" <<EOF
type: $PKG.components.ets_forecast.component.EtsForecastComponent
attributes:
  asset_name: weekly_orders_forecast
  upstream_asset_key: weekly_orders
  date_column: week_start
  value_column: order_count
  forecast_periods: 8
  trend: add
  seasonal: add
  group_name: forecasts
EOF

cat > "src/$PKG/defs/create_samples/defs.yaml" <<EOF
type: $PKG.components.create_samples.component.CreateSamplesComponent
attributes:
  asset_name: churn_dataset_split
  upstream_asset_key: churn_dataset
  test_size: 0.2
  validation_size: 0.1
  random_state: 42
  stratify_column: churn_label
  output_split_column: split
  group_name: ml_datasets
EOF

cat <<MSG

>>> Setup complete.

Materialize all 3 + their sources:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000

The forecast assets emit one row per forecast period (point estimate +
confidence interval columns). The split asset emits the original
DataFrame with an added 'split' column tagging each row 'train', 'val',
or 'test'.
MSG
