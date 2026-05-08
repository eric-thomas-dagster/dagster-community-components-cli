#!/usr/bin/env bash
# DataFrame basics demo — 9 fundamental shape-preserving transforms.
#
# WHAT THIS DEMONSTRATES
#   The bread-and-butter pandas operations exposed as Dagster components.
#   No new dependencies, simple synthetic source.
#
# Asset graph:
#   sales (synthetic source: 60 rows, multi-region multi-category sales)
#         │
#         ├── sales_filtered      ← filter (active rows only)
#         ├── sales_sorted        ← sort (by date desc)
#         ├── sales_unique        ← unique_dedup (drop dup transaction_ids)
#         ├── sales_slim          ← select_columns (subset columns)
#         ├── sales_cleansed      ← data_cleansing (trim/normalize/fillna)
#         ├── revenue_by_region   ← summarize (group by + aggregate)
#         ├── ranked_categories   ← rank (per-region rank by revenue)
#         ├── running_revenue     ← running_total (cumulative per region)
#         └── metrics_transposed  ← transpose (long → wide)
#
# COST: \$0 — pandas only.

set -euo pipefail
PROJECT_DIR="${1:-dataframe-basics-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 9 basic-transform components"
$CLI add filter          --auto-install
$CLI add sort            --auto-install
$CLI add unique_dedup    --auto-install
$CLI add select_columns  --auto-install
$CLI add data_cleansing  --auto-install
$CLI add summarize       --auto-install
$CLI add rank            --auto-install
$CLI add running_total   --auto-install
$CLI add transpose       --auto-install

echo ">>> Writing inline source asset"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest", description="60 synthetic sales rows: 3 regions × 4 categories × 5 days.")
def sales() -> pd.DataFrame:
    rows = []
    txn = 1000
    for day_offset in range(5):
        for region in ("us", "eu", "apac"):
            for category in ("widgets", "gadgets", "sprockets", "misc"):
                for _ in range(1):
                    rows.append({
                        "transaction_id": txn,
                        "sale_date": f"2025-04-{day_offset + 1:02d}",
                        "region": region,
                        "category": category,
                        "product": f"{category}-A",
                        "quantity": (txn % 5) + 1,
                        "revenue": round((txn % 100) * 1.5, 2),
                        "status": "active" if txn % 4 != 0 else "cancelled",
                        "notes": "  Some leading/trailing whitespace  " if txn % 3 == 0 else None,
                    })
                    txn += 1
    return pd.DataFrame(rows)


# Long-format metric data for the transpose demo.
@dg.asset(group_name="ingest", description="Long-format monthly metrics for transpose.")
def monthly_metrics() -> pd.DataFrame:
    return pd.DataFrame([
        {"metric_name": "revenue",     "jan": 1000, "feb": 1100, "mar": 1250},
        {"metric_name": "active_users", "jan": 50,  "feb": 55,   "mar": 62},
        {"metric_name": "churn_rate",   "jan": 0.05, "feb": 0.04, "mar": 0.03},
    ])


defs = dg.Definitions(assets=[sales, monthly_metrics])
PYEOF

echo ">>> Writing 9 transform defs.yaml"

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: sales_filtered
  upstream_asset_key: sales
  condition: "status == 'active' and revenue > 50"
  group_name: transforms
EOF

cat > "src/$PKG/defs/sort/defs.yaml" <<EOF
type: $PKG.components.sort.component.SortComponent
attributes:
  asset_name: sales_sorted
  upstream_asset_key: sales
  by: [sale_date, revenue]
  ascending: false
  reset_index: true
  group_name: transforms
EOF

cat > "src/$PKG/defs/unique_dedup/defs.yaml" <<EOF
type: $PKG.components.unique_dedup.component.UniqueDedupComponent
attributes:
  asset_name: sales_unique
  upstream_asset_key: sales
  subset: [transaction_id]
  keep: first
  output_mode: unique
  group_name: transforms
EOF

cat > "src/$PKG/defs/select_columns/defs.yaml" <<EOF
type: $PKG.components.select_columns.component.SelectColumnsComponent
attributes:
  asset_name: sales_slim
  upstream_asset_key: sales
  columns: [transaction_id, sale_date, region, category, revenue]
  group_name: transforms
EOF

cat > "src/$PKG/defs/data_cleansing/defs.yaml" <<EOF
type: $PKG.components.data_cleansing.component.DataCleansingComponent
attributes:
  asset_name: sales_cleansed
  upstream_asset_key: sales
  null_handling: fill
  null_fill_value: "n/a"
  trim_whitespace: true
  normalize_case: lower
  columns: [notes, region, category]
  group_name: transforms
EOF

cat > "src/$PKG/defs/summarize/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: revenue_by_region
  upstream_asset_key: sales
  group_by: [region, category]
  aggregations:
    revenue: sum
    transaction_id: count
  group_name: transforms
EOF

cat > "src/$PKG/defs/rank/defs.yaml" <<EOF
type: $PKG.components.rank.component.RankComponent
attributes:
  asset_name: ranked_categories
  upstream_asset_key: sales
  column: revenue
  method: dense
  ascending: false
  group_by: [region]
  output_column: revenue_rank
  group_name: transforms
EOF

cat > "src/$PKG/defs/running_total/defs.yaml" <<EOF
type: $PKG.components.running_total.component.RunningTotalComponent
attributes:
  asset_name: running_revenue
  upstream_asset_key: sales
  value_column: revenue
  output_column: cumulative_revenue
  group_by: [region]
  sort_by: sale_date
  sort_ascending: true
  group_name: transforms
EOF

cat > "src/$PKG/defs/transpose/defs.yaml" <<EOF
type: $PKG.components.transpose.component.TransposeComponent
attributes:
  asset_name: metrics_transposed
  upstream_asset_key: monthly_metrics
  index_column: metric_name
  reset_column_name: field
  group_name: transforms
EOF

cat <<MSG

>>> Setup complete.

Materialize all 9 transforms:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000
MSG
