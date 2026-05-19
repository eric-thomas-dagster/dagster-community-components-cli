#!/usr/bin/env bash
# ML feature engineering demo — 6 feature-prep transforms.
#
# WHAT THIS DEMONSTRATES
#   The standard pre-modeling pipeline as Dagster components: missing-value
#   imputation, outlier clipping, label/one-hot encoding, scaling, binning.
#   Each is a typed component you wire into your asset graph instead of
#   ad-hoc sklearn code.
#
# Asset graph:
#   raw_customers (synthetic source, 100 rows, mixed numeric + categorical)
#         │
#         ├── customers_imputed       ← imputation (fill NaNs)
#         ├── customers_no_outliers   ← outlier_clipper (IQR-based)
#         ├── customers_label_encoded ← label_encoder (categorical → ints)
#         ├── customers_one_hot       ← one_hot_encoding (categorical → boolean cols)
#         ├── customers_scaled        ← feature_scaler (standard z-score)
#         └── customers_binned        ← tile_binning (annual_spend → 5 tiers)
#
# COST: \$0 — pandas + scikit-learn, all local.

set -euo pipefail
PROJECT_DIR="${1:-ml-features-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas scikit-learn numpy
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 feature-engineering transforms"
$CLI add imputation       --auto-install
$CLI add outlier_clipper  --auto-install
$CLI add label_encoder    --auto-install
$CLI add one_hot_encoding --auto-install
$CLI add feature_scaler   --auto-install
$CLI add tile_binning     --auto-install

echo ">>> Writing inline source asset"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import numpy as np
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest", description="100 synthetic customers with missing values + outliers + categoricals.")
def raw_customers() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 100
    annual_spend = rng.gamma(2, 1000, n)
    # Inject some outliers
    annual_spend[5] = 100000
    annual_spend[42] = 250000
    df = pd.DataFrame({
        "customer_id": range(1, n + 1),
        "country": rng.choice(["US", "CA", "UK", "FR", "DE"], n),
        "subscription_tier": rng.choice(["free", "pro", "enterprise"], n, p=[0.6, 0.3, 0.1]),
        "acquisition_channel": rng.choice(["organic", "paid", "referral"], n),
        "annual_spend": annual_spend.round(2),
        "support_tickets_30d": rng.integers(0, 15, n),
        "lifetime_value": rng.gamma(3, 2000, n).round(2),
        "days_since_signup": rng.integers(1, 1000, n),
    })
    # Inject some missing values
    df.loc[rng.choice(n, 10, replace=False), "support_tickets_30d"] = np.nan
    df.loc[rng.choice(n, 8, replace=False), "lifetime_value"] = np.nan
    return df


defs = dg.Definitions(assets=[raw_customers])
PYEOF

echo ">>> Writing 6 transform defs.yaml"

cat > "src/$PKG/defs/imputation/defs.yaml" <<EOF
type: $PKG.components.imputation.component.ImputationComponent
attributes:
  asset_name: customers_imputed
  upstream_asset_key: raw_customers
  strategy: median
  columns: [support_tickets_30d, lifetime_value]
  group_name: features
EOF

cat > "src/$PKG/defs/outlier_clipper/defs.yaml" <<EOF
type: $PKG.components.outlier_clipper.component.OutlierClipperComponent
attributes:
  asset_name: customers_no_outliers
  upstream_asset_key: raw_customers
  strategy: iqr
  iqr_multiplier: 1.5
  action: clip
  columns: [annual_spend, lifetime_value]
  group_name: features
EOF

cat > "src/$PKG/defs/label_encoder/defs.yaml" <<EOF
type: $PKG.components.label_encoder.component.LabelEncoderComponent
attributes:
  asset_name: customers_label_encoded
  upstream_asset_key: raw_customers
  columns: [country, subscription_tier, acquisition_channel]
  ordering: frequency
  suffix: _code
  keep_original: true
  group_name: features
EOF

cat > "src/$PKG/defs/one_hot_encoding/defs.yaml" <<EOF
type: $PKG.components.one_hot_encoding.component.OneHotEncodingComponent
attributes:
  asset_name: customers_one_hot
  upstream_asset_key: raw_customers
  columns: [country, subscription_tier]
  drop_first: true
  dummy_na: false
  prefix_sep: _
  dtype: int
  group_name: features
EOF

cat > "src/$PKG/defs/feature_scaler/defs.yaml" <<EOF
type: $PKG.components.feature_scaler.component.FeatureScalerComponent
attributes:
  asset_name: customers_scaled
  upstream_asset_key: raw_customers
  strategy: standard
  columns: [annual_spend, lifetime_value, days_since_signup]
  suffix: _scaled
  group_name: features
EOF

cat > "src/$PKG/defs/tile_binning/defs.yaml" <<EOF
type: $PKG.components.tile_binning.component.TileBinningComponent
attributes:
  asset_name: customers_binned
  upstream_asset_key: raw_customers
  column: annual_spend
  n_bins: 5
  method: equal_freq
  labels: [bronze, silver, gold, platinum, diamond]
  output_column: spend_tier
  group_name: features
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000

Each output asset shows the transformed DataFrame's preview metadata —
useful to visually confirm imputation filled the NaNs, outliers got
clipped, encoding produced the expected columns, etc.
MSG
