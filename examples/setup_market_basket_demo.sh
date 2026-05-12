#!/usr/bin/env bash
# Market Basket demo — apriori association rules on synthetic e-commerce baskets.
#
# Generates 200 synthetic shopping baskets with realistic item co-occurrence,
# runs apriori to find frequent itemsets + derive association rules
# (support / confidence / lift), filters to high-lift rules, summarizes by
# antecedent count, and writes the strong rules to CSV.
#
# Pipeline (7 components, all autoloaded by `dg`):
#   csv_file_ingestion → market_basket_rules ─┬─→ filter (lift > 1.5)  → CSV (strong rules)
#                                              │
#                                              └─→ summarize (top antecedents) → CSV

set -euo pipefail
PROJECT_DIR="${1:-market-basket-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas mlxtend
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Generating synthetic baskets (200 orders × ~4 items each, with bundles)"
uv run python - <<'PY'
import csv, random
random.seed(42)
# Bundles that often co-occur
bundles = [
    ["bread", "butter", "jam"],
    ["chips", "salsa", "guacamole"],
    ["milk", "cereal", "bananas"],
    ["pasta", "tomato_sauce", "parmesan"],
    ["beer", "pretzels", "peanuts"],
    ["coffee", "creamer", "sugar"],
]
extras = ["apples", "oranges", "yogurt", "eggs", "cheese", "lettuce", "tomatoes",
          "cucumber", "carrots", "chicken", "beef", "salmon", "rice", "olive_oil",
          "vinegar", "soap", "shampoo", "toothpaste", "paper_towels"]
rows = []
for order_id in range(1, 201):
    items = []
    # 70% chance of including a bundle
    if random.random() < 0.7:
        items += random.choice(bundles)
    # add 1-3 random extras
    items += random.sample(extras, k=random.randint(1, 3))
    for item in set(items):
        rows.append({"order_id": f"ORD{order_id:04d}", "product_name": item})
with open("/tmp/baskets.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader(); w.writerows(rows)
print(f"wrote /tmp/baskets.csv: {len(rows)} line items across 200 baskets")
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add csv_file_ingestion    --auto-install
$CLI add market_basket_rules   --auto-install
$CLI add filter                --auto-install
$CLI add summarize             --auto-install
$CLI add dataframe_to_csv      --auto-install
mkdir -p "src/$PKG/defs/dataframe_to_csv_top"  # only needs defs.yaml; component code is in components/dataframe_to_csv/
$CLI add cron_schedule         --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: order_line_items
  file_path: /tmp/baskets.csv
  description: 200 synthetic shopping baskets with item bundles
  group_name: ingest
EOF

cat > "src/$PKG/defs/market_basket_rules/defs.yaml" <<EOF
type: $PKG.components.market_basket_rules.component.MarketBasketRulesComponent
attributes:
  asset_name: all_rules
  upstream_asset_key: order_line_items
  basket_column: order_id
  item_column: product_name
  min_support: 0.03
  min_confidence: 0.3
  metric: lift
  group_name: mining
EOF

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: strong_rules
  upstream_asset_key: all_rules
  condition: "lift > 1.5"
  group_name: mining
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: rules_by_antecedent
  upstream_asset_key: all_rules
  group_by:
    - antecedents
  aggregations:
    consequents: count
    lift: max
    confidence: max
    support: max
  group_name: mining
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: strong_rules_report
  upstream_asset_key: strong_rules
  file_path: /tmp/strong_rules.csv
  include_index: false
EOF

cat > "src/$PKG/defs/dataframe_to_csv_top/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: top_antecedents_report
  upstream_asset_key: rules_by_antecedent
  file_path: /tmp/rules_by_antecedent.csv
  include_index: false
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: weekly_basket_mining
  cron_expression: "0 9 * * 1"
  asset_keys:
    - strong_rules_report
    - top_antecedents_report
  default_status: STOPPED
  tags:
    purpose: basket_mining
EOF

cat <<MSG

>>> Setup complete.
Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Outputs:
  /tmp/strong_rules.csv             — rules with lift > 1.5 (the actionable ones)
  /tmp/rules_by_antecedent.csv      — for each antecedent, the count of consequents and best lift

Inspect:
    head /tmp/strong_rules.csv
    head /tmp/rules_by_antecedent.csv
MSG
