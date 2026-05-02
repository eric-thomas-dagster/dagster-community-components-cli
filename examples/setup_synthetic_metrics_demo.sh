#!/usr/bin/env bash
# Synthetic time-series demo — registry-native generator.
#
# Uses the registry's time_series_generator (no upstream — generates from
# scratch from start_date/end_date/frequency/pattern) to produce a
# 30-day hourly metric, runs anomaly detection on it, writes a CSV.
#
# Demonstrates that synthetic data doesn't need inline Python — the
# registry has a real component for it.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     time_series_generator → anomaly_detection → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-synthetic-metrics-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas numpy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add time_series_generator    --auto-install
$CLI add anomaly_detection        --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1. Generate — 30 days of hourly metrics with a complex pattern + noise + spikes
cat > "src/$PKG/defs/time_series_generator/defs.yaml" <<EOF
type: $PKG.components.time_series_generator.component.TimeSeriesGeneratorComponent
attributes:
  asset_name: synthetic_metrics
  pattern_type: complex
  start_date: "2024-01-01"
  end_date: "2024-01-31"
  frequency: 1h
  base_value: 100.0
  noise_level: 0.15
  random_seed: 42
  metric_name: cpu_pct
  description: 30 days of synthetic hourly CPU% metrics with seasonality + noise
  group_name: synth
EOF

# 2. Detect anomalies — z-score with a 2.5σ threshold
cat > "src/$PKG/defs/anomaly_detection/defs.yaml" <<EOF
type: $PKG.components.anomaly_detection.component.AnomalyDetectionComponent
attributes:
  asset_name: metrics_with_anomalies
  upstream_asset_key: synthetic_metrics
  metric_column: cpu_pct
  detection_method: z_score
  threshold: 2.5
  group_name: model
EOF

# 3. Sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: metrics_report
  upstream_asset_key: metrics_with_anomalies
  file_path: /tmp/synthetic_metrics_anomalies.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/synthetic_metrics_anomalies.csv — every hour, the synthetic
cpu_pct value, plus is_anomaly + anomaly_score.

Inspect:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/synthetic_metrics_anomalies.csv')
    print(f'rows: {len(df)}, anomalies: {df.is_anomaly.sum()}')
    print(df[df.is_anomaly].head(8).to_string())
    "
MSG
