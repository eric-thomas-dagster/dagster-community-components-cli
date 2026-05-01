#!/usr/bin/env bash
# Churn prediction demo — synthetic per-customer aggregates → risk scores.
#
# Generates a customer-level CSV (200 customers, recent + dormant mixed),
# runs churn_prediction (rule-based: inactivity threshold + risk factors
# from order count, revenue, lifetime), writes a per-customer CSV with
# `is_at_risk` flag + risk score.
#
# Pipeline (3 components, all autoloaded by `dg`):
#     csv_file_ingestion → churn_prediction → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-churn-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating synthetic customer-level CSV"
uv run python - <<'PY'
import csv, random
from datetime import datetime, timedelta
random.seed(42)
today = datetime(2026, 5, 1)
rows = []
for i in range(1, 201):
    # Mix of fresh, dormant, and churned customers
    days_since = random.choices(
        [random.randint(0, 30), random.randint(31, 90), random.randint(91, 365)],
        weights=[5, 3, 2],
    )[0]
    last_activity = today - timedelta(days=days_since)
    lifetime_days = random.randint(60, 800)
    total_orders = max(1, int(random.gauss(15, 8)))
    total_revenue = round(total_orders * random.uniform(20, 250), 2)
    rows.append({
        'customer_id': f'cus_{i:04d}',
        'last_activity': last_activity.strftime('%Y-%m-%d'),
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'lifetime_days': lifetime_days,
    })
with open('/tmp/customer_metrics.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
print(f"wrote /tmp/customer_metrics.csv with {len(rows)} customers")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 3 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add churn_prediction      --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.defs.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: customer_metrics
  file_path: /tmp/customer_metrics.csv
  description: 200 synthetic customer aggregates (last_activity, total_orders, revenue, lifetime)
  group_name: ingest
EOF

cat > "src/$PKG/defs/churn_prediction/defs.yaml" <<EOF
type: $PKG.defs.churn_prediction.component.ChurnPredictionComponent
attributes:
  asset_name: customers_with_churn_risk
  upstream_asset_key: customer_metrics
  inactivity_threshold_days: 60
  lookback_days: 365
  include_risk_factors: true
  customer_id_field: customer_id
  last_activity_field: last_activity
  total_orders_field: total_orders
  total_revenue_field: total_revenue
  lifetime_days_field: lifetime_days
  group_name: model
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.defs.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: churn_report
  upstream_asset_key: customers_with_churn_risk
  file_path: /tmp/churn_predictions.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/churn_predictions.csv — every customer, days since last
activity, risk score, is_at_risk flag, plus contributing risk factors.

Inspect — risk-level distribution + top 5 highest risks:
    uv run python -c "
    import pandas as pd
    df = pd.read_csv('/tmp/churn_predictions.csv')
    print(f'customers: {len(df)}')
    print(df.churn_risk_level.value_counts().to_string())
    print()
    cols = ['customer_id','days_inactive','churn_risk_score','churn_risk_level','recommended_action']
    print(df.sort_values('churn_risk_score', ascending=False).head(5)[cols].to_string(index=False))
    "
MSG
