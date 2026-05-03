#!/usr/bin/env bash
# IoT sensor gap-fill demo — repair a sparse time series, then run a
# cumulative metric on top.
#
# synthetic_data_generator's `sparse_sensors` schema generates 3 sensors
# x 14 days of hourly readings with ~25% rows dropped to simulate flaky
# devices. ts_filler resamples each sensor to a regular hourly grid and
# forward-fills the gaps; running_total then computes a cumulative
# average across the cleaned series.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     synthetic_data_generator → ts_filler → running_total → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-sensor-gapfill-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/components/ + defs/"
$CLI add synthetic_data_generator --auto-install
$CLI add ts_filler                --auto-install
$CLI add running_total            --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: sensor_readings_raw
  schema_type: sparse_sensors
  row_count: 100000   # soft cap, won't truncate this small grid
  random_state: 42
  schema_options:
    sensor_count: 3
    duration_hours: 336    # 14 days * 24
    dropout_rate: 0.25     # leave ~25% gaps
    base_temp: 22.0
    noise_amplitude: 2.0
    start_date: "2026-04-01"
  description: 3 sensors x 14 days of hourly temperature readings with ~25% dropouts
  group_name: ingest
EOF

cat > "src/$PKG/defs/ts_filler/defs.yaml" <<EOF
type: $PKG.components.ts_filler.component.TsFillerComponent
attributes:
  asset_name: sensor_readings_filled
  upstream_asset_key: sensor_readings_raw
  date_column: reading_ts
  frequency: h
  fill_method: forward_fill
  value_columns:
    - temperature_c
  group_by:
    - sensor_id
  group_name: transform
EOF

cat > "src/$PKG/defs/running_total/defs.yaml" <<EOF
type: $PKG.components.running_total.component.RunningTotalComponent
attributes:
  asset_name: sensor_running_avg
  upstream_asset_key: sensor_readings_filled
  value_column: temperature_c
  output_column: running_avg_c
  sort_by: reading_ts
  sort_ascending: true
  agg_function: mean
  group_by:
    - sensor_id
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: sensor_running_avg_report
  upstream_asset_key: sensor_running_avg
  file_path: /tmp/sensor_running_avg.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize headlessly:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Or open the UI:
    cd $PROJECT_DIR && uv run dg dev

Output: /tmp/sensor_running_avg.csv — every hour for every sensor with
a forward-filled temperature_c plus a running average of temperature
since the start of the series.

Inspect — show the running average converging for sensor_a:
    head -1 /tmp/sensor_running_avg.csv
    awk -F, 'NR>1 && \$2=="sensor_a" {print}' /tmp/sensor_running_avg.csv | head -5
    awk -F, 'NR>1 && \$2=="sensor_a" {print}' /tmp/sensor_running_avg.csv | tail -5
MSG
