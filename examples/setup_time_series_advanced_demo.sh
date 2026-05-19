#!/usr/bin/env bash
# Time-series advanced demo — 4 model-factory & comparison components.
#
# WHAT THIS DEMONSTRATES
#   The advanced ts_* family in analytics/. Single-series, model-comparison,
#   covariate-aware, and per-group forecast factory. All statsmodels,
#   no SaaS.
#
# Asset graph:
#   monthly_sales (synthetic 36 months, trend + seasonality)
#   monthly_sales_with_covariates (adds marketing_spend + holiday flag)
#   product_sales (synthetic per-product time series, 5 products × 36 months)
#         │
#         ├── monthly_sales_forecast    ← ts_forecast (auto-selects ARIMA/ETS)
#         ├── arima_vs_ets_comparison   ← ts_compare (hold-out test, picks winner)
#         ├── covariate_forecast        ← ts_covariate_forecast (SARIMAX with exog)
#         └── per_product_forecasts     ← ts_model_factory (one model per group)
#
# COST: \$0 — statsmodels + pandas, all local.

set -euo pipefail
PROJECT_DIR="${1:-time-series-advanced-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy statsmodels
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 time-series components"
$CLI add ts_forecast            --auto-install
$CLI add ts_compare             --auto-install
$CLI add ts_covariate_forecast  --auto-install
$CLI add ts_model_factory       --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import numpy as np
import pandas as pd
import dagster as dg


def _seasonal_series(n: int, seed: int, base: float = 1000.0, slope: float = 15.0):
    rng = np.random.default_rng(seed)
    trend = np.linspace(base, base + slope * n, n)
    seasonal = 200 * np.sin(2 * np.pi * np.arange(n) / 12)
    noise = rng.normal(0, 50, n)
    return (trend + seasonal + noise).round(2)


@dg.asset(group_name="ingest", description="36 months of synthetic monthly revenue.")
def monthly_sales() -> pd.DataFrame:
    months = pd.date_range("2022-01-01", periods=36, freq="MS")
    return pd.DataFrame({
        "month": months.strftime("%Y-%m-%d"),
        "revenue": _seasonal_series(36, 42),
    })


@dg.asset(group_name="ingest", description="Same 36 months + 2 covariates (marketing_spend, holiday_flag).")
def monthly_sales_with_covariates() -> pd.DataFrame:
    months = pd.date_range("2022-01-01", periods=36, freq="MS")
    rng = np.random.default_rng(11)
    return pd.DataFrame({
        "month": months.strftime("%Y-%m-%d"),
        "revenue": _seasonal_series(36, 42),
        "marketing_spend": rng.gamma(2, 200, 36).round(2),
        "holiday_flag": [(m.month in (11, 12)) for m in months],
    })


@dg.asset(group_name="ingest", description="Per-product monthly sales — 5 products × 36 months.")
def product_sales() -> pd.DataFrame:
    months = pd.date_range("2022-01-01", periods=36, freq="MS")
    rows = []
    for pid, base, slope in [
        ("widget_a", 500, 10),
        ("widget_b", 300, 5),
        ("gadget_x", 200, 20),
        ("sprocket_y", 150, 2),
        ("doohickey_z", 80, 1),
    ]:
        units = _seasonal_series(36, hash(pid) & 0xFFFF, base=base, slope=slope)
        for m, u in zip(months, units):
            rows.append({
                "month": m.strftime("%Y-%m-%d"),
                "product_id": pid,
                "units_sold": int(max(u, 0)),
            })
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[monthly_sales, monthly_sales_with_covariates, product_sales])
PYEOF

echo ">>> Writing 4 time-series defs.yaml"

cat > "src/$PKG/defs/ts_forecast/defs.yaml" <<EOF
type: $PKG.components.ts_forecast.component.TsForecastComponent
attributes:
  asset_name: monthly_sales_forecast
  upstream_asset_key: monthly_sales
  date_column: month
  value_column: revenue
  forecast_periods: 12
  model: auto
  group_name: forecasts
EOF

cat > "src/$PKG/defs/ts_compare/defs.yaml" <<EOF
type: $PKG.components.ts_compare.component.TsCompareComponent
attributes:
  asset_name: arima_vs_ets_comparison
  upstream_asset_key: monthly_sales
  date_column: month
  value_column: revenue
  test_periods: 6
  arima_order: [1, 1, 1]
  ets_trend: add
  group_name: forecasts
EOF

cat > "src/$PKG/defs/ts_covariate_forecast/defs.yaml" <<EOF
type: $PKG.components.ts_covariate_forecast.component.TSCovariateForecastComponent
attributes:
  asset_name: covariate_forecast
  upstream_asset_key: monthly_sales_with_covariates
  date_column: month
  value_column: revenue
  covariate_columns: [marketing_spend, holiday_flag]
  n_periods: 6
  order: [1, 1, 1]
  group_name: forecasts
EOF

cat > "src/$PKG/defs/ts_model_factory/defs.yaml" <<EOF
type: $PKG.components.ts_model_factory.component.TsModelFactoryComponent
attributes:
  asset_name: per_product_forecasts
  upstream_asset_key: product_sales
  date_column: month
  value_column: units_sold
  group_column: product_id
  forecast_periods: 6
  model: ets
  group_name: forecasts
EOF

cat <<MSG

>>> Setup complete.

Materialize all 4 time-series components + their sources:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000
MSG
