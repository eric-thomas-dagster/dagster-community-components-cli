#!/usr/bin/env bash
# Customer analytics demo — 6 marketing/retention components.
#
# WHAT THIS DEMONSTRATES
#   The customer-analytics family that was not in setup_analytics_demo.sh:
#   journey mapping, RFM segmentation, marketing attribution, ML
#   predictions, and text preprocessing + topic modeling on support
#   tickets. All sklearn/pandas, no SaaS.
#
# Asset graph:
#   customers (synthetic 200 rows)
#   customer_events (synthetic touchpoints — purchases, page views, ads)
#   marketing_touchpoints (synthetic multi-channel attribution data)
#   support_tickets (synthetic 50 rows of support text)
#         │
#         ├── customer_journeys           ← customer_journey_mapping
#         ├── customer_rfm_segments       ← customer_segmentation (RFM)
#         ├── channel_attribution         ← multi_touch_attribution
#         ├── customer_churn_predictions  ← random_forest_model
#         ├── support_clean_text          ← text_preprocessing
#         └── support_topics              ← topic_modeling (LDA)
#
# COST: \$0 — pandas + sklearn, all local.

set -euo pipefail
PROJECT_DIR="${1:-customer-analytics-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy scikit-learn nltk
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 customer-analytics components"
$CLI add customer_journey_mapping --auto-install
$CLI add customer_segmentation    --auto-install
$CLI add multi_touch_attribution  --auto-install
$CLI add random_forest_model      --auto-install
$CLI add text_preprocessing       --auto-install
$CLI add topic_modeling           --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import numpy as np
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest")
def customer_events() -> pd.DataFrame:
    """Synthetic event stream — page views, ad clicks, purchases."""
    rng = np.random.default_rng(42)
    n_users = 100
    rows = []
    for u in range(1, n_users + 1):
        ts = pd.Timestamp("2025-04-01")
        for step in range(rng.integers(3, 12)):
            etype = rng.choice(
                ["page_view", "ad_click", "add_to_cart", "purchase"],
                p=[0.6, 0.15, 0.15, 0.10],
            )
            ts += pd.Timedelta(hours=int(rng.integers(1, 48)))
            rows.append({
                "user_id": u,
                "event_type": etype,
                "timestamp": ts,
                "channel": rng.choice(["organic", "paid_search", "email", "social"]),
            })
    return pd.DataFrame(rows)


@dg.asset(group_name="ingest")
def orders() -> pd.DataFrame:
    """Synthetic orders for RFM segmentation (recency / frequency / monetary).

    Dates are generated relative to today so the customer_segmentation
    component's analysis_period_days (default 365) doesn't filter them all
    out as 'too old'.
    """
    rng = np.random.default_rng(7)
    today = pd.Timestamp.now().normalize()
    rows = []
    for u in range(1, 101):
        n_orders = rng.integers(1, 12)
        for _ in range(n_orders):
            rows.append({
                "customer_id": u,
                "order_date": today - pd.Timedelta(days=int(rng.integers(0, 360))),
                "amount": float(round(float(rng.gamma(2, 50)), 2)),
            })
    return pd.DataFrame(rows)


@dg.asset(group_name="ingest")
def marketing_touchpoints() -> pd.DataFrame:
    """Synthetic per-conversion touchpoint trails for attribution.

    Each conversion is anchored to a customer (customer_id) — required by
    the multi_touch_attribution component. Dates are recent so the
    component's lookback_window_days filter retains them.
    """
    rng = np.random.default_rng(11)
    rows = []
    today = pd.Timestamp.now().normalize()
    for conv_id in range(1, 51):
        customer_id = int(rng.integers(1, 101))   # link conversions to the same customer pool as orders
        n = rng.integers(1, 5)
        ts = today - pd.Timedelta(days=int(rng.integers(1, 25)))
        for i in range(n):
            ts -= pd.Timedelta(days=int(rng.integers(1, 7)))
            rows.append({
                "conversion_id": conv_id,
                "customer_id": customer_id,
                "touchpoint_index": i,
                "timestamp": ts,
                "channel": rng.choice(["organic", "paid_search", "email", "social", "display"]),
                "revenue": 100.0 if i == 0 else 0.0,  # revenue only attributed to last
            })
    return pd.DataFrame(rows)


@dg.asset(group_name="ingest")
def customer_features() -> pd.DataFrame:
    """Customer features + churn label for random_forest_model."""
    rng = np.random.default_rng(13)
    n = 200
    return pd.DataFrame({
        "customer_id": range(1, n + 1),
        "total_orders": rng.integers(1, 30, n),
        "avg_order_value": rng.gamma(2, 50, n).round(2),
        "days_since_first_order": rng.integers(30, 1000, n),
        "support_tickets": rng.poisson(3, n),
        "churn_label": rng.choice([0, 1], n, p=[0.7, 0.3]).astype(int),
    })


@dg.asset(group_name="ingest")
def support_tickets() -> pd.DataFrame:
    """Synthetic support ticket text for NLP preprocessing + topic modeling."""
    templates = [
        "Cannot log into my account, password reset link not working",
        "Subscription was charged twice this month, need a refund",
        "How do I export data to CSV format?",
        "App crashes on iOS when I try to upload images",
        "Feature request: please add dark mode to the dashboard",
        "Order #12345 was never delivered, where is my package?",
        "Billing statement shows wrong amount for last month",
        "Integration with Salesforce is not syncing properly",
        "Forgot my password and the reset email never arrives",
        "Tutorial videos are not loading on Safari browser",
    ]
    rng = np.random.default_rng(17)
    return pd.DataFrame([
        {
            "ticket_id": i,
            "ticket_body": templates[rng.integers(0, len(templates))]
                + (" " + templates[rng.integers(0, len(templates))] if i % 3 == 0 else ""),
        }
        for i in range(50)
    ])


defs = dg.Definitions(assets=[customer_events, orders, marketing_touchpoints, customer_features, support_tickets])
PYEOF

echo ">>> Writing 6 component defs.yaml"

cat > "src/$PKG/defs/customer_journey_mapping/defs.yaml" <<EOF
type: $PKG.components.customer_journey_mapping.component.CustomerJourneyMappingComponent
attributes:
  asset_name: customer_journeys
  upstream_asset_key: customer_events
  conversion_event: purchase
  max_journey_length: 20
  time_window_hours: 720
  group_name: customer_analytics
EOF

cat > "src/$PKG/defs/customer_segmentation/defs.yaml" <<EOF
type: $PKG.components.customer_segmentation.component.CustomerSegmentationComponent
attributes:
  asset_name: customer_rfm_segments
  transaction_data_asset_key: orders
  analysis_period_days: 365
  scoring_method: quintiles
  use_predefined_segments: true
  group_name: customer_analytics
EOF

cat > "src/$PKG/defs/multi_touch_attribution/defs.yaml" <<EOF
type: $PKG.components.multi_touch_attribution.component.MultiTouchAttributionComponent
attributes:
  asset_name: channel_attribution
  upstream_asset_key: marketing_touchpoints
  attribution_model: time_decay
  lookback_window_days: 30
  time_decay_half_life_days: 7.0
  include_channel_performance: true
  group_name: marketing_analytics
EOF

cat > "src/$PKG/defs/random_forest_model/defs.yaml" <<EOF
type: $PKG.components.random_forest_model.component.RandomForestModelComponent
attributes:
  asset_name: customer_churn_predictions
  upstream_asset_key: customer_features
  target_column: churn_label
  feature_columns: [total_orders, avg_order_value, days_since_first_order, support_tickets]
  task_type: classification
  n_estimators: 100
  group_name: ml
EOF

cat > "src/$PKG/defs/text_preprocessing/defs.yaml" <<EOF
type: $PKG.components.text_preprocessing.component.TextPreprocessingComponent
attributes:
  asset_name: support_clean_text
  upstream_asset_key: support_tickets
  text_column: ticket_body
  output_column: cleaned_text
  normalize_case: true
  remove_punctuation: true
  remove_numbers: false
  group_name: nlp
EOF

cat > "src/$PKG/defs/topic_modeling/defs.yaml" <<EOF
type: $PKG.components.topic_modeling.component.TopicModelingComponent
attributes:
  asset_name: support_topics
  upstream_asset_key: support_clean_text
  text_column: cleaned_text
  n_topics: 4
  n_top_words: 8
  max_features: 500
  max_iter: 20
  group_name: nlp
EOF

cat <<MSG

>>> Setup complete.

Materialize all 6 components + their sources:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000
MSG
