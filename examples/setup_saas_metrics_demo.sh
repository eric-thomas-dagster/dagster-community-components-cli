#!/usr/bin/env bash
# SaaS metrics demo — synthetic Stripe data → MRR / ARR / churn / LTV.
#
# Generates a Stripe-shaped CSV with 50 synthetic subscriptions (active +
# trialing + canceled, spread over 18 months), runs subscription_metrics
# to compute MRR / ARR / churn / LTV / ARPU, writes a SaaS-dashboard CSV.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     synthetic_data_generator → subscription_metrics → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-saas-metrics-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/components/ + defs/"
$CLI add synthetic_data_generator  --auto-install
$CLI add subscription_metrics      --auto-install
$CLI add dataframe_to_csv          --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: stripe_data
  schema_type: stripe_subscriptions
  row_count: 50
  random_state: 42
  schema_options:
    plans:
      - [10, "starter"]
      - [29, "basic"]
      - [49, "pro"]
      - [99, "business"]
      - [199, "enterprise"]
    plan_weights: [3, 4, 3, 2, 1]
    status_mix: {active: 0.65, trialing: 0.10, canceled: 0.25}
    lookback_days: 540
  description: Synthetic Stripe-shaped subscriptions for a SaaS metrics demo
  group_name: ingest
EOF

cat > "src/$PKG/defs/subscription_metrics/defs.yaml" <<EOF
type: $PKG.components.subscription_metrics.component.SubscriptionMetricsComponent
attributes:
  asset_name: saas_metrics
  stripe_data_asset_key: stripe_data
  calculation_period: monthly
  ltv_method: historical
  lookback_months: 12
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: saas_metrics_report
  upstream_asset_key: saas_metrics
  file_path: /tmp/saas_metrics.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/saas_metrics.csv — current MRR / ARR / churn / LTV /
ARPU snapshot computed from the synthetic Stripe data.

Inspect:
    cat /tmp/saas_metrics.csv
MSG
