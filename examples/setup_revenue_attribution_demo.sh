#!/usr/bin/env bash
# Revenue attribution demo — split conversions across marketing channels.
#
# Generates two synthetic CSVs (marketing campaigns + Stripe revenue),
# fans them in to revenue_attribution, computes campaign-level metrics
# (spend, conversions, ROI, ROAS, CAC), writes a per-campaign report.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion (marketing) ┐
#                                     ├─→ revenue_attribution → dataframe_to_csv
#     csv_file_ingestion (revenue)   ┘

set -euo pipefail

PROJECT_DIR="${1:-revenue-attribution-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating synthetic marketing + revenue CSVs"
uv run python - <<'PY'
import csv, random, time
random.seed(42)
now = int(time.time())

# Marketing campaigns — 5 campaigns over the last 90 days
campaigns = [
    ("Spring_Sale",      8000,  150_000, 4_500, 320),
    ("Brand_Awareness",  12000, 300_000, 3_000, 80),
    ("Retargeting",      4500,   80_000, 6_500, 410),
    ("Black_Friday",     15000, 200_000, 8_000, 580),
    ("Newsletter",       1500,   25_000, 1_200, 210),
]
mrows = []
for name, spend, impr, clicks, conv in campaigns:
    mrows.append({
        'campaign_name': name,
        'spend': spend,
        'impressions': impr,
        'clicks': clicks,
        'conversions': conv,
    })
with open('/tmp/marketing_campaigns.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=mrows[0].keys()); w.writeheader(); w.writerows(mrows)

# Revenue: synthetic Stripe charges
plans = [29, 49, 99, 199, 499]
rrows = []
for i in range(1, 121):
    rrows.append({
        'id': f'ch_{i:04d}',
        '_resource_type': 'charges',
        'customer_id': f'cus_{random.randint(1, 100):03d}',
        'amount': random.choice(plans) * 100,
        'created': now - random.randint(0, 90) * 86400,
        'status': 'succeeded',
    })
with open('/tmp/stripe_revenue.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=rrows[0].keys()); w.writeheader(); w.writerows(rrows)

print(f"wrote /tmp/marketing_campaigns.csv ({len(mrows)} campaigns)")
print(f"wrote /tmp/stripe_revenue.csv ({len(rrows)} charges)")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components into src/$PKG/defs/"
$CLI add csv_file_ingestion       --auto-install
$CLI add revenue_attribution      --auto-install
$CLI add dataframe_to_csv         --auto-install
# Second csv ingest (different target_dir) for the revenue source
$CLI add csv_file_ingestion       --auto-install --target-dir "src/$PKG/defs/csv_revenue"

echo ">>> Writing demo defs.yaml for each component"

# 1a. Ingest marketing
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: marketing_data
  file_path: /tmp/marketing_campaigns.csv
  description: Synthetic marketing campaigns (5 campaigns, 90-day window)
  group_name: ingest
EOF

# 1b. Ingest revenue
cat > "src/$PKG/defs/csv_revenue/defs.yaml" <<EOF
type: $PKG.components.csv_revenue.component.CSVFileIngestionComponent
attributes:
  asset_name: revenue_data
  file_path: /tmp/stripe_revenue.csv
  description: Synthetic Stripe charges (120 events, 90-day window)
  group_name: ingest
EOF

# 2. Attribution
cat > "src/$PKG/defs/revenue_attribution/defs.yaml" <<EOF
type: $PKG.components.revenue_attribution.component.RevenueAttributionComponent
attributes:
  asset_name: campaign_attribution
  marketing_data_asset: marketing_data
  revenue_data_asset: revenue_data
  attribution_model: linear
  attribution_window_days: 30
  join_key: customer_id
  group_name: model
EOF

# 3. Sink
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: campaign_attribution_report
  upstream_asset_key: campaign_attribution
  file_path: /tmp/campaign_attribution.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/campaign_attribution.csv — per-campaign spend, impressions,
clicks, conversions, plus computed ROI / ROAS / CAC where the data
permits.

Inspect:
    cat /tmp/campaign_attribution.csv
MSG
