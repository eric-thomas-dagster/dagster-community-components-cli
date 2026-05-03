#!/usr/bin/env bash
# Revenue attribution demo — split conversions across marketing channels.
#
# Marketing campaigns are a 5-row literal table (no synthesis needed).
# Revenue is generated as 120 synthetic Stripe charges via the registry's
# synthetic_data_generator (schema_type: stripe_charges). Both fan in to
# revenue_attribution, which computes per-campaign metrics
# (spend, conversions, ROI, ROAS, CAC) and writes a per-campaign report.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion (marketing)        ┐
#                                             ├─→ revenue_attribution → dataframe_to_csv
#     synthetic_data_generator (stripe rev) ┘

set -euo pipefail

PROJECT_DIR="${1:-revenue-attribution-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing the 5-row marketing campaigns literal table"
cat > /tmp/marketing_campaigns.csv <<'EOF'
campaign_name,spend,impressions,clicks,conversions
Spring_Sale,8000,150000,4500,320
Brand_Awareness,12000,300000,3000,80
Retargeting,4500,80000,6500,410
Black_Friday,15000,200000,8000,580
Newsletter,1500,25000,1200,210
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components into src/$PKG/components/ + defs/"
$CLI add csv_file_ingestion       --auto-install
$CLI add synthetic_data_generator --auto-install
$CLI add revenue_attribution      --auto-install
$CLI add dataframe_to_csv         --auto-install

echo ">>> Writing demo defs.yaml for each component"

# 1a. Ingest marketing — literal CSV
cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: marketing_data
  file_path: /tmp/marketing_campaigns.csv
  description: Marketing campaigns (5 campaigns, 90-day window)
  group_name: ingest
EOF

# 1b. Generate Stripe-shaped revenue from the registry
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: revenue_data
  schema_type: stripe_charges
  row_count: 120
  random_state: 42
  schema_options:
    plans: [29, 49, 99, 199, 499]
    lookback_days: 90
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
