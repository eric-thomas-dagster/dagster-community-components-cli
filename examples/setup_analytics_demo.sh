#!/usr/bin/env bash
# Analytics mega-demo — 42 local sklearn/pandas analytics components.
# No API keys, no external services. Synthetic source data fans out to
# regressions, classifiers, scoring, geo, standardizers, customer/marketing.
#
# COST: $0 — entirely local

set -euo pipefail
PROJECT_DIR="${1:-analytics-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas numpy scikit-learn scipy statsmodels imbalanced-learn shapely geopy joblib
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 42 analytics components"
for c in linear_regression_model gradient_boosting_model naive_bayes_model \
         neural_network_model gamma_regression count_regression spline_model svm \
         k_centroids_diagnostics append_cluster model_coefficients vif \
         model_comparison model_score lift_chart \
         stepwise multidimensional_scaling oversample_field simulation_sampling \
         test_of_means optimization \
         geocoder reverse_geocoder coordinate_transformer point_in_polygon \
         customer_360 customer_health_score lead_scoring priority_scorer \
         product_recommendations product_usage_analytics propensity_scoring \
         subscription_metrics funnel_analysis campaign_performance \
         ad_spend_standardizer crm_data_standardizer ecommerce_standardizer \
         event_data_standardizer marketing_data_standardizer \
         product_analytics_standardizer support_ticket_standardizer; do
  $CLI add $c --auto-install || echo "FAILED: $c"
done

echo ">>> Writing inline source data"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import numpy as np
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest")
def ml_dataset() -> pd.DataFrame:
    """Synthetic dataset for ML regressions/classifiers."""
    rng = np.random.default_rng(42)
    n = 200
    x1 = rng.normal(0, 1, n)
    x2 = rng.normal(5, 2, n)
    x3 = rng.uniform(0, 10, n)
    # regression target (continuous, positive — works for gamma + linear)
    y_reg = np.exp(0.3 * x1 + 0.2 * x2 + 0.1 * x3 + rng.normal(0, 0.3, n))
    # count target (Poisson-like)
    y_count = rng.poisson(np.exp(0.1 * x1 + 0.05 * x3), n)
    # binary classification target
    p = 1 / (1 + np.exp(-(0.5 * x1 - 0.2 * x2)))
    y_class = (rng.uniform(0, 1, n) < p).astype(int)
    return pd.DataFrame({
        "x1": x1, "x2": x2, "x3": x3,
        "y_reg": y_reg.round(3),
        "y_count": y_count,
        "y_class": y_class,
    })


@dg.asset(group_name="ingest")
def geo_dataset() -> pd.DataFrame:
    """Lat/lon points + addresses for geo components."""
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["NYC", "SF", "Chicago", "Seattle", "Austin"],
        "address": ["350 5th Ave, New York, NY",
                    "1 Market St, San Francisco, CA",
                    "233 S Wacker Dr, Chicago, IL",
                    "400 Broad St, Seattle, WA",
                    "100 Congress Ave, Austin, TX"],
        "latitude": [40.7484, 37.7749, 41.8781, 47.6062, 30.2672],
        "longitude": [-73.9857, -122.4194, -87.6298, -122.3321, -97.7431],
        "x_coord": [40.7484, 37.7749, 41.8781, 47.6062, 30.2672],
        "y_coord": [-73.9857, -122.4194, -87.6298, -122.3321, -97.7431],
    })


@dg.asset(group_name="ingest")
def customer_dataset() -> pd.DataFrame:
    """Customer-360 / health-score / churn-style data."""
    rng = np.random.default_rng(7)
    n = 100
    return pd.DataFrame({
        "customer_id": range(1, n + 1),
        "email": [f"user{i}@example.com" for i in range(1, n + 1)],
        "signup_date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "last_active_date": pd.date_range("2025-04-01", periods=n, freq="D")[:n],
        "mrr": (rng.gamma(2, 50, n)).round(2),
        "ltv": (rng.gamma(2, 500, n)).round(2),
        "feature_usage_count": rng.integers(0, 100, n),
        "support_tickets": rng.integers(0, 10, n),
        "nps_score": rng.integers(0, 11, n),
        "churned": rng.integers(0, 2, n),
        "plan": rng.choice(["free", "pro", "enterprise"], n),
    })


@dg.asset(group_name="ingest")
def event_dataset() -> pd.DataFrame:
    """Event-stream data for funnel + product-analytics standardizer."""
    rng = np.random.default_rng(11)
    n = 500
    events = ["page_view", "signup", "trial_start", "purchase", "logout"]
    return pd.DataFrame({
        "user_id": rng.integers(1, 100, n),
        "event_name": rng.choice(events, n, p=[0.4, 0.15, 0.15, 0.1, 0.2]),
        "timestamp": pd.date_range("2025-04-01", periods=n, freq="h"),
        "session_id": rng.integers(1, 200, n),
        "properties": ["{}"] * n,
    })


@dg.asset(group_name="ingest")
def campaign_dataset() -> pd.DataFrame:
    """Marketing campaign metrics."""
    return pd.DataFrame({
        "campaign_id": [1, 2, 3, 4, 5],
        "campaign_name": ["Spring Sale", "Brand Awareness", "Retargeting",
                          "Holiday Promo", "Lookalike"],
        "platform": ["facebook_ads", "google_ads", "linkedin_ads",
                     "facebook_ads", "tiktok_ads"],
        "spend": [5000.0, 12000.0, 3500.0, 8000.0, 2500.0],
        "impressions": [200000, 500000, 80000, 350000, 60000],
        "clicks": [4000, 8500, 1500, 5000, 900],
        "conversions": [150, 280, 75, 200, 30],
    })


@dg.asset(group_name="ingest")
def crm_dataset() -> pd.DataFrame:
    """HubSpot-shaped CRM data for standardizer."""
    return pd.DataFrame({
        "id": ["c1", "c2", "c3"],
        "properties_email": ["alice@acme.com", "bob@globex.io", "carol@initech.org"],
        "properties_firstname": ["Alice", "Bob", "Carol"],
        "properties_lastname": ["Smith", "Jones", "Davis"],
        "properties_company": ["Acme", "Globex", "Initech"],
        "properties_createdate": ["2024-01-15", "2024-03-10", "2024-06-20"],
    })


@dg.asset(group_name="ingest")
def ecommerce_dataset() -> pd.DataFrame:
    """Shopify-shaped order data for ecommerce standardizer."""
    return pd.DataFrame({
        "id": [101, 102, 103],
        "name": ["#1001", "#1002", "#1003"],
        "total_price": ["100.00", "200.00", "300.00"],
        "currency": ["USD", "USD", "USD"],
        "customer": [{"email": "alice@acme.com"}, {"email": "bob@globex.io"}, {"email": "carol@initech.org"}],
        "created_at": ["2025-04-01T10:00:00Z", "2025-04-05T14:30:00Z", "2025-04-10T09:15:00Z"],
    })


@dg.asset(group_name="ingest")
def support_dataset() -> pd.DataFrame:
    """Zendesk-shaped ticket data for support_ticket_standardizer."""
    return pd.DataFrame({
        "id": [501, 502, 503],
        "subject": ["Cannot login", "Billing question", "Feature request"],
        "description": ["I locked out", "Charged twice", "Want dark mode"],
        "status": ["open", "pending", "open"],
        "priority": ["high", "low", "normal"],
        "created_at": ["2025-04-01T10:00:00Z", "2025-04-02T11:00:00Z", "2025-04-03T12:00:00Z"],
        "requester_id": [101, 102, 103],
    })


@dg.asset(group_name="ingest")
def ad_spend_dataset() -> pd.DataFrame:
    """Generic ad-spend data."""
    return pd.DataFrame({
        "date": pd.date_range("2025-04-01", periods=30, freq="D").astype(str),
        "platform": ["facebook"] * 10 + ["google"] * 10 + ["linkedin"] * 10,
        "spend_usd": [100 + i for i in range(30)],
        "impressions": [10000 + i * 100 for i in range(30)],
    })


@dg.asset(group_name="ingest")
def trained_model() -> str:
    """Train a tiny model and write to /tmp so model_score can load it."""
    import joblib
    from sklearn.linear_model import LogisticRegression
    import numpy as np
    X = np.random.RandomState(42).normal(0, 1, (50, 3))
    y = (X[:, 0] + X[:, 1] > 0).astype(int)
    m = LogisticRegression().fit(X, y)
    path = "/tmp/analytics_demo_model.pkl"
    joblib.dump(m, path)
    return path


defs = dg.Definitions(assets=[ml_dataset, geo_dataset, customer_dataset,
                              event_dataset, campaign_dataset, crm_dataset,
                              ecommerce_dataset, support_dataset, ad_spend_dataset,
                              trained_model])
PYEOF

echo ">>> Writing 42 analytics defs.yaml"
write_yaml() {
  local d="$1"; local body="$2"
  mkdir -p "src/$PKG/defs/$d"
  echo -e "$body" > "src/$PKG/defs/$d/defs.yaml"
}

# === ML models (8) ===
for c in linear_regression_model gradient_boosting_model naive_bayes_model \
         spline_model svm count_regression gamma_regression; do
  CLASS=$(python3 -c "n='$c'; print(''.join(p.capitalize() for p in n.split('_')) + 'Component')" | sed 's/Svm/SVM/')
  TARGET="y_reg"; if [ "$c" = "naive_bayes_model" ] || [ "$c" = "svm" ]; then TARGET="y_class"; fi
  if [ "$c" = "count_regression" ]; then TARGET="y_count"; fi
  write_yaml "$c" "type: $PKG.components.$c.component.$CLASS
attributes:
  asset_name: ${c}_output
  upstream_asset_key: ml_dataset
  target_column: $TARGET
  feature_columns: [x1, x2, x3]
  group_name: ml"
done

# Neural network needs hidden_layer_sizes
write_yaml "neural_network_model" "type: $PKG.components.neural_network_model.component.NeuralNetworkModelComponent
attributes:
  asset_name: nn_output
  upstream_asset_key: ml_dataset
  target_column: y_reg
  feature_columns: [x1, x2, x3]
  hidden_layer_sizes: [16, 8]
  group_name: ml"

# === Diagnostics (4) ===
write_yaml "k_centroids_diagnostics" "type: $PKG.components.k_centroids_diagnostics.component.KCentroidsDiagnosticsComponent
attributes:
  asset_name: k_centroids_diag
  upstream_asset_key: ml_dataset
  feature_columns: [x1, x2, x3]
  group_name: ml"

write_yaml "append_cluster" "type: $PKG.components.append_cluster.component.AppendClusterComponent
attributes:
  asset_name: ml_with_cluster
  upstream_asset_key: ml_dataset
  feature_columns: [x1, x2, x3]
  group_name: ml"

write_yaml "model_coefficients" "type: $PKG.components.model_coefficients.component.ModelCoefficientsComponent
attributes:
  asset_name: model_coef
  upstream_asset_key: ml_dataset
  target_column: y_reg
  feature_columns: [x1, x2, x3]
  group_name: ml"

write_yaml "vif" "type: $PKG.components.vif.component.VifComponent
attributes:
  asset_name: vif_scores
  upstream_asset_key: ml_dataset
  feature_columns: [x1, x2, x3]
  group_name: ml"

# === Model utility (3) ===
write_yaml "model_comparison" "type: $PKG.components.model_comparison.component.ModelComparisonComponent
attributes:
  asset_name: model_compare
  upstream_asset_key: ml_dataset
  target_column: y_reg
  feature_columns: [x1, x2, x3]
  models: [linear, gradient_boosting]
  group_name: ml"

write_yaml "model_score" "type: $PKG.components.model_score.component.ModelScoreComponent
attributes:
  asset_name: model_predictions
  upstream_asset_key: ml_dataset
  model_path: /tmp/analytics_demo_model.pkl
  feature_columns: [x1, x2, x3]
  deps:
    - trained_model
  group_name: ml"

write_yaml "lift_chart" "type: $PKG.components.lift_chart.component.LiftChartComponent
attributes:
  asset_name: lift_chart_output
  upstream_asset_key: ml_dataset
  actual_column: y_class
  predicted_proba_column: x1
  group_name: ml"

# === Stats (5) ===
write_yaml "stepwise" "type: $PKG.components.stepwise.component.StepwiseComponent
attributes:
  asset_name: stepwise_output
  upstream_asset_key: ml_dataset
  target_column: y_reg
  feature_columns: [x1, x2, x3]
  group_name: stats"

write_yaml "multidimensional_scaling" "type: $PKG.components.multidimensional_scaling.component.MultidimensionalScalingComponent
attributes:
  asset_name: mds_output
  upstream_asset_key: ml_dataset
  feature_columns: [x1, x2, x3]
  group_name: stats"

write_yaml "oversample_field" "type: $PKG.components.oversample_field.component.OversampleFieldComponent
attributes:
  asset_name: oversampled
  upstream_asset_key: ml_dataset
  target_column: y_class
  group_name: stats"

write_yaml "simulation_sampling" "type: $PKG.components.simulation_sampling.component.SimulationSamplingComponent
attributes:
  asset_name: simulated
  upstream_asset_key: ml_dataset
  variable_column: x1
  distribution_column: x2
  param1_column: x3
  param2_column: y_reg
  group_name: stats"

write_yaml "test_of_means" "type: $PKG.components.test_of_means.component.TestOfMeansComponent
attributes:
  asset_name: t_test_output
  upstream_asset_key: ml_dataset
  value_column: y_reg
  group_column: y_class
  group_name: stats"

# === Optimization (1) ===
write_yaml "optimization" "type: $PKG.components.optimization.component.OptimizationComponent
attributes:
  asset_name: optimization_output
  upstream_asset_key: ml_dataset
  objective_column: y_reg
  group_name: stats"

# === Geo (4) ===
write_yaml "coordinate_transformer" "type: $PKG.components.coordinate_transformer.component.CoordinateTransformerComponent
attributes:
  asset_name: coord_transformed
  upstream_asset_key: geo_dataset
  x_column: x_coord
  y_column: y_coord
  group_name: geo"

write_yaml "point_in_polygon" "type: $PKG.components.point_in_polygon.component.PointInPolygonComponent
attributes:
  asset_name: pip_output
  upstream_asset_key: geo_dataset
  group_name: geo"

write_yaml "geocoder" "type: $PKG.components.geocoder.component.GeocoderComponent
attributes:
  asset_name: geocoded
  upstream_asset_key: geo_dataset
  address_column: address
  group_name: geo"

write_yaml "reverse_geocoder" "type: $PKG.components.reverse_geocoder.component.ReverseGeocoderComponent
attributes:
  asset_name: reverse_geocoded
  upstream_asset_key: geo_dataset
  group_name: geo"

# === Business analytics (8) ===
write_yaml "customer_360" "type: $PKG.components.customer_360.component.Customer360Component
attributes:
  asset_name: customer_360_output
  crm_data_asset_key: crm_dataset
  group_name: business"

write_yaml "customer_health_score" "type: $PKG.components.customer_health_score.component.CustomerHealthScoreComponent
attributes:
  asset_name: health_score_output
  customer_data_asset_key: customer_dataset
  support_ticket_asset_key: support_dataset
  group_name: business"

write_yaml "lead_scoring" "type: $PKG.components.lead_scoring.component.LeadScoringComponent
attributes:
  asset_name: lead_score_output
  lead_data_asset_key: customer_dataset
  group_name: business"

write_yaml "priority_scorer" "type: $PKG.components.priority_scorer.component.PriorityScorerComponent
attributes:
  asset_name: priority_score_output
  upstream_asset_key: support_dataset
  group_name: business"

write_yaml "product_recommendations" "type: $PKG.components.product_recommendations.component.ProductRecommendationsComponent
attributes:
  asset_name: product_recs
  upstream_asset_key: customer_dataset
  group_name: business"

write_yaml "product_usage_analytics" "type: $PKG.components.product_usage_analytics.component.ProductUsageAnalyticsComponent
attributes:
  asset_name: product_usage_output
  event_data_asset_key: event_dataset
  user_data_asset_key: customer_dataset
  group_name: business"

write_yaml "propensity_scoring" "type: $PKG.components.propensity_scoring.component.PropensityScoringComponent
attributes:
  asset_name: propensity_output
  upstream_asset_key: customer_dataset
  group_name: business"

write_yaml "subscription_metrics" "type: $PKG.components.subscription_metrics.component.SubscriptionMetricsComponent
attributes:
  asset_name: subscription_metrics_output
  stripe_data_asset_key: ecommerce_dataset
  group_name: business"

# === Marketing/event (2) ===
write_yaml "funnel_analysis" "type: $PKG.components.funnel_analysis.component.FunnelAnalysisComponent
attributes:
  asset_name: funnel_output
  event_data_asset_key: event_dataset
  user_data_asset_key: customer_dataset
  group_name: marketing"

write_yaml "campaign_performance" "type: $PKG.components.campaign_performance.component.CampaignPerformanceComponent
attributes:
  asset_name: campaign_perf
  upstream_asset_key: campaign_dataset
  group_name: marketing"

# === Standardizers (7) ===
write_yaml "ad_spend_standardizer" "type: $PKG.components.ad_spend_standardizer.component.AdSpendStandardizerComponent
attributes:
  asset_name: ad_spend_std
  google_ads_asset_key: ad_spend_dataset
  group_name: standardizers"

write_yaml "crm_data_standardizer" "type: $PKG.components.crm_data_standardizer.component.CRMDataStandardizerComponent
attributes:
  asset_name: crm_std
  upstream_asset_key: crm_dataset
  platform: hubspot
  resource_type: contacts
  group_name: standardizers"

write_yaml "ecommerce_standardizer" "type: $PKG.components.ecommerce_standardizer.component.EcommerceStandardizerComponent
attributes:
  asset_name: ecommerce_std
  upstream_asset_key: ecommerce_dataset
  platform: shopify
  resource_type: orders
  group_name: standardizers"

write_yaml "event_data_standardizer" "type: $PKG.components.event_data_standardizer.component.EventDataStandardizerComponent
attributes:
  asset_name: event_std
  upstream_asset_key: event_dataset
  platform: segment
  group_name: standardizers"

write_yaml "marketing_data_standardizer" "type: $PKG.components.marketing_data_standardizer.component.MarketingDataStandardizerComponent
attributes:
  asset_name: marketing_std
  upstream_asset_key: campaign_dataset
  platform: facebook_ads
  group_name: standardizers"

write_yaml "product_analytics_standardizer" "type: $PKG.components.product_analytics_standardizer.component.ProductAnalyticsStandardizerComponent
attributes:
  asset_name: product_analytics_std
  upstream_asset_key: event_dataset
  platform: amplitude
  group_name: standardizers"

write_yaml "support_ticket_standardizer" "type: $PKG.components.support_ticket_standardizer.component.SupportTicketStandardizerComponent
attributes:
  asset_name: support_std
  upstream_asset_key: support_dataset
  platform: zendesk
  group_name: standardizers"

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

42 analytics components on synthetic data. \$0 cost — all local sklearn/scipy/statsmodels.
MSG
