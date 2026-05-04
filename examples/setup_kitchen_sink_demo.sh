#!/usr/bin/env bash
# Kitchen Sink demo — 21 components in one project.
#
# An e-commerce intelligence pipeline that exercises a long list of
# components across every category (ingest, quality, transform, analytics,
# sink, schedule). This is the showcase demo: pick almost any combination
# of registry components and they should compose like this.
#
# Synthetic data only — no API keys, no external services.
#
# Pipeline (21 components, all autoloaded by `dg`):
#
#   synthetic_data_generator (orders, 2000)    ─┐
#   synthetic_data_generator (customers, 600)   ├─ INGEST
#   synthetic_data_generator (products, 200)   ─┘
#         │
#         ├─→ unique_dedup → type_coercer → outlier_clipper ─┐
#         ├─→ data_cleansing                                 │
#         │                                                   ├─→ dataframe_join
#         │                                                   │   (orders ⋈ customers)
#         │                                                   │        │
#         │                                                   │        ├─→ filter (delivered)  → CSV
#         │                                                   │        ├─→ summarize (category)→ CSV
#         │                                                   │        ├─→ summarize (city)    → CSV
#         │                                                   │        ├─→ rank (top cats)     → CSV
#         │                                                   │        ├─→ select_columns ──┐
#         │                                                   │        ├─→ sort (recent)    │
#         │                                                   │        └─→ rfm_segmentation → CSV
#         │
#         └─→ cron_schedule (daily 7am refresh of all 5 reports)

set -euo pipefail

PROJECT_DIR="${1:-kitchen-sink-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas numpy
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 21 community components"
# Ingest (1 base + 2 extra target dirs for customers, products)
$CLI add synthetic_data_generator --auto-install
$CLI add synthetic_data_generator --auto-install --target-dir "src/$PKG/defs/customers_gen"
$CLI add synthetic_data_generator --auto-install --target-dir "src/$PKG/defs/products_gen"

# Quality
$CLI add unique_dedup     --auto-install
$CLI add type_coercer     --auto-install
$CLI add outlier_clipper  --auto-install
$CLI add data_cleansing   --auto-install

# Join
$CLI add dataframe_join   --auto-install

# Transform branch
$CLI add filter           --auto-install
$CLI add select_columns   --auto-install
$CLI add sort             --auto-install

# Analytics
$CLI add summarize        --auto-install
$CLI add summarize        --auto-install --target-dir "src/$PKG/defs/summarize_by_city"
$CLI add rank             --auto-install
$CLI add rfm_segmentation --auto-install

# Sinks (5 total — base + 4 extras)
$CLI add dataframe_to_csv --auto-install
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/csv_city"
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/csv_top_categories"
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/csv_rfm"
$CLI add dataframe_to_csv --auto-install --target-dir "src/$PKG/defs/csv_orders"

# Orchestration
$CLI add cron_schedule    --auto-install

echo ">>> Writing demo defs.yaml for each component"

# ─── INGEST ─────────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 2000
  random_state: 42
  description: 2000 synthetic e-commerce orders across 7 categories with status flags
  group_name: ingest
EOF

cat > "src/$PKG/defs/customers_gen/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: customers_raw
  schema_type: customers
  row_count: 600
  random_state: 42
  description: 600 synthetic customers (name, email, city, state)
  group_name: ingest
EOF

cat > "src/$PKG/defs/products_gen/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: products_raw
  schema_type: products
  row_count: 200
  random_state: 42
  description: 200 synthetic products with categories + prices
  group_name: ingest
EOF

# ─── QUALITY ────────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/unique_dedup/defs.yaml" <<EOF
type: $PKG.components.unique_dedup.component.UniqueDedupComponent
attributes:
  asset_name: orders_dedup
  upstream_asset_key: orders_raw
  subset:
    - order_id
  keep: first
  output_mode: unique
  group_name: quality
EOF

cat > "src/$PKG/defs/type_coercer/defs.yaml" <<EOF
type: $PKG.components.type_coercer.component.TypeCoercerComponent
attributes:
  asset_name: orders_typed
  upstream_asset_key: orders_dedup
  type_map:
    order_date: datetime
    num_items: int
    subtotal: float
    tax: float
    shipping: float
    total: float
  errors: coerce
  datetime_format: "%Y-%m-%d %H:%M:%S"
  group_name: quality
EOF

cat > "src/$PKG/defs/outlier_clipper/defs.yaml" <<EOF
type: $PKG.components.outlier_clipper.component.OutlierClipperComponent
attributes:
  asset_name: orders_clipped
  upstream_asset_key: orders_typed
  strategy: iqr
  iqr_multiplier: 3.0
  action: clip
  columns:
    - total
    - subtotal
  group_name: quality
EOF

cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: customers_cleansed
  upstream_asset_key: customers_raw
  trim_whitespace: true
  normalize_case: lower
  columns:
    - first_name
    - last_name
    - email
    - city
  group_name: quality
EOF

# ─── JOIN ───────────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/dataframe_join/defs.yaml" <<EOF
type: $PKG.components.dataframe_join.component.DataframeJoin
attributes:
  asset_name: orders_with_customers
  left_asset_key: orders_clipped
  right_asset_key: customers_cleansed
  how: left
  "on":
    - customer_id
  group_name: enriched
EOF

# ─── TRANSFORMS ─────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: orders_completed
  upstream_asset_key: orders_with_customers
  condition: "status == 'delivered'"
  group_name: transforms
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.components.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: orders_slim
  upstream_asset_key: orders_completed
  columns:
    - order_id
    - customer_id
    - order_date
    - category
    - total
    - city
    - state
  reorder: true
  group_name: transforms
EOF

cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.components.sort.component.SortComponent
attributes:
  asset_name: orders_recent
  upstream_asset_key: orders_slim
  by:
    - order_date
    - total
  ascending: false
  reset_index: true
  group_name: transforms
EOF

# ─── ANALYTICS ──────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: revenue_by_category
  upstream_asset_key: orders_with_customers
  group_by:
    - category
  aggregations:
    total: sum
    order_id: count
    num_items: sum
  group_name: analytics
EOF

cat > "src/$PKG/defs/summarize_by_city/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: revenue_by_city
  upstream_asset_key: orders_with_customers
  group_by:
    - city
    - state
  aggregations:
    total: sum
    order_id: count
  group_name: analytics
EOF

cat > "src/$PKG/defs/rank/defs.yaml" <<EOF
type: $PKG.components.rank.component.RankComponent
attributes:
  asset_name: top_categories
  upstream_asset_key: revenue_by_category
  column: total
  method: dense
  ascending: false
  output_column: revenue_rank
  group_name: analytics
EOF

cat > "src/$PKG/defs/rfm_segmentation/defs.yaml" <<EOF
type: $PKG.components.rfm_segmentation.component.RFMSegmentationComponent
attributes:
  asset_name: customer_rfm
  upstream_asset_key: orders_with_customers
  scoring_method: quintile
  lookback_days: 365
  customer_id_field: customer_id
  order_date_field: order_date
  order_id_field: order_id
  revenue_field: total
  description: RFM segmentation — Champions, Loyal, At Risk, Lost
  group_name: analytics
EOF

# ─── SINKS ──────────────────────────────────────────────────────────────────
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: revenue_by_category_report
  upstream_asset_key: revenue_by_category
  file_path: /tmp/kitchen_sink_revenue_by_category.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/csv_city/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: revenue_by_city_report
  upstream_asset_key: revenue_by_city
  file_path: /tmp/kitchen_sink_revenue_by_city.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/csv_top_categories/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: top_categories_report
  upstream_asset_key: top_categories
  file_path: /tmp/kitchen_sink_top_categories.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/csv_rfm/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: rfm_report
  upstream_asset_key: customer_rfm
  file_path: /tmp/kitchen_sink_rfm.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/csv_orders/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: orders_recent_report
  upstream_asset_key: orders_recent
  file_path: /tmp/kitchen_sink_orders_recent.csv
  include_index: false
  group_name: sink
EOF

# ─── ORCHESTRATION ──────────────────────────────────────────────────────────
cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: daily_kitchen_sink_refresh
  cron_expression: "0 7 * * *"
  asset_keys:
    - revenue_by_category_report
    - revenue_by_city_report
    - top_categories_report
    - rfm_report
    - orders_recent_report
  default_status: STOPPED
  tags:
    purpose: ecommerce_intelligence
EOF

cat <<MSG

>>> Setup complete (21 components in one project).

Materialize:
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Or open the UI to browse the lineage graph:
    cd $PROJECT_DIR && uv run dg dev

Outputs (5 reports in /tmp):
  /tmp/kitchen_sink_revenue_by_category.csv
  /tmp/kitchen_sink_revenue_by_city.csv
  /tmp/kitchen_sink_top_categories.csv
  /tmp/kitchen_sink_rfm.csv
  /tmp/kitchen_sink_orders_recent.csv

Inspect:
    head /tmp/kitchen_sink_revenue_by_category.csv
    head /tmp/kitchen_sink_rfm.csv
MSG
