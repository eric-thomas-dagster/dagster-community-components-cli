#!/usr/bin/env bash
# Forecast Comparison demo — generate a TS, fit two competing models in parallel,
# evaluate which fits better via ts_compare.
#
# Pipeline (8 components, all autoloaded by `dg`):
#                                                    ┌─→ arima_forecast → CSV
#   time_series_generator → ts_filler ──────────────┤
#                                                    ├─→ ets_forecast   → CSV
#                                                    │
#                                                    └─→ ts_compare     → CSV (winner verdict)

set -euo pipefail
PROJECT_DIR="${1:-forecast-comparison-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas statsmodels numpy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add time_series_generator --auto-install
$CLI add ts_filler             --auto-install
$CLI add arima_forecast        --auto-install
$CLI add ets_forecast          --auto-install
$CLI add ts_compare            --auto-install
$CLI add dataframe_to_csv      --auto-install
$CLI add dataframe_to_csv      --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_ets"
$CLI add dataframe_to_csv      --auto-install --target-dir "src/$PKG/defs/dataframe_to_csv_compare"

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/time_series_generator/defs.yaml" <<EOF
type: $PKG.components.time_series_generator.component.TimeSeriesGeneratorComponent
attributes:
  asset_name: revenue_history
  pattern_type: complex
  start_date: "2024-01-01"
  end_date: "2025-12-31"
  frequency: 1d
  base_value: 50000.0
  noise_level: 0.1
  random_state: 42
  metric_name: revenue
  group_name: ingest
EOF

cat > "src/$PKG/defs/ts_filler/defs.yaml" <<EOF
type: $PKG.components.ts_filler.component.TsFillerComponent
attributes:
  asset_name: revenue_filled
  upstream_asset_key: revenue_history
  date_column: timestamp
  frequency: D
  fill_method: forward_fill
  value_columns: [revenue]
  group_name: transform
EOF

cat > "src/$PKG/defs/arima_forecast/defs.yaml" <<EOF
type: $PKG.components.arima_forecast.component.ArimaForecastComponent
attributes:
  asset_name: arima_predictions
  upstream_asset_key: revenue_filled
  date_column: timestamp
  value_column: revenue
  forecast_periods: 30
  order: [2, 1, 2]
  group_name: forecast
EOF

cat > "src/$PKG/defs/ets_forecast/defs.yaml" <<EOF
type: $PKG.components.ets_forecast.component.EtsForecastComponent
attributes:
  asset_name: ets_predictions
  upstream_asset_key: revenue_filled
  date_column: timestamp
  value_column: revenue
  forecast_periods: 30
  trend: add
  seasonal: add
  seasonal_periods: 7
  group_name: forecast
EOF

cat > "src/$PKG/defs/ts_compare/defs.yaml" <<EOF
type: $PKG.components.ts_compare.component.TsCompareComponent
attributes:
  asset_name: model_comparison
  upstream_asset_key: revenue_filled
  date_column: timestamp
  value_column: revenue
  arima_order: [2, 1, 2]
  ets_trend: add
  ets_seasonal: add
  ets_seasonal_periods: 7
  test_periods: 30
  group_name: forecast
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: arima_report
  upstream_asset_key: arima_predictions
  file_path: /tmp/forecast_arima.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_ets/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: ets_report
  upstream_asset_key: ets_predictions
  file_path: /tmp/forecast_ets.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_compare/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: comparison_report
  upstream_asset_key: model_comparison
  file_path: /tmp/forecast_comparison.csv
  include_index: false
EOF

cat <<MSG

>>> Setup complete.
Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Outputs:
  /tmp/forecast_arima.csv      — 30-day ARIMA(2,1,2) forecast
  /tmp/forecast_ets.csv        — 30-day ETS additive forecast
  /tmp/forecast_comparison.csv — head-to-head metrics on a held-out test set
MSG
