#!/usr/bin/env bash
# SaaS metrics demo — synthetic Stripe data → MRR / ARR / churn / LTV.
#
# Generates a Stripe-shaped CSV with 50 synthetic subscriptions (active +
# trialing + canceled, spread over 18 months), runs subscription_metrics
# to compute MRR / ARR / churn / LTV / ARPU, writes a SaaS-dashboard CSV.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → subscription_metrics → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-saas-metrics-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating a synthetic Stripe-shaped subscriptions CSV"
uv run python - <<'PY'
import csv, random, time
random.seed(42)
now = int(time.time())
# 50 subscriptions spread over the last 540 days
plans = [(10, 'starter'), (29, 'basic'), (49, 'pro'), (99, 'business'), (199, 'enterprise')]
rows = []
for i in range(1, 51):
    days_ago = random.randint(0, 540)
    created = now - days_ago * 86400
    plan_amount, plan_name = random.choices(plans, weights=[3, 4, 3, 2, 1])[0]
    # 65% active, 10% trialing, 25% churned
    r = random.random()
    if r < 0.65:
        status, canceled_at = 'active', ''
        cpe = created + 30 * 86400  # current_period_end
    elif r < 0.75:
        status, canceled_at = 'trialing', ''
        cpe = created + 14 * 86400
    else:
        status = 'canceled'
        canceled_at = created + random.randint(30, 300) * 86400
        cpe = canceled_at
    rows.append({
        'id': f'sub_{i:03d}',
        '_resource_type': 'subscriptions',
        'customer_id': f'cus_{i:03d}',
        'status': status,
        'created': created,
        'canceled_at': canceled_at,
        'current_period_end': cpe,
        'plan_amount': plan_amount * 100,  # cents, like Stripe
        'plan_interval': 'month',
        'plan_nickname': plan_name,
    })
with open('/tmp/stripe_subscriptions.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
print(f"wrote /tmp/stripe_subscriptions.csv with {len(rows)} subscriptions")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion        --auto-install
$CLI add subscription_metrics      --auto-install
$CLI add dataframe_to_csv          --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: stripe_data
  file_path: /tmp/stripe_subscriptions.csv
  description: Synthetic Stripe-shaped subscriptions for a SaaS metrics demo
  group_name: ingest
EOF

cat > "src/$PKG/defs/subscription_metrics/defs.yaml" <<EOF
type: $PKG.defs.subscription_metrics.component.SubscriptionMetricsComponent
attributes:
  asset_name: saas_metrics
  stripe_data_asset: stripe_data
  calculation_period: monthly
  ltv_method: historical
  lookback_months: 12
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
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
