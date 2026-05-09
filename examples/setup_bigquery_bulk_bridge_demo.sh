#!/usr/bin/env bash
# BQ ↔ GCS bulk-bridge demo — vendor-native EXTRACT + LOAD round-trip.
#
# WHAT THIS DEMONSTRATES
#   The two new vendor-native bridge components running a full round
#   trip: BQ table → GCS parquet → BQ table. Data never goes through
#   the Dagster executor — BQ pushes/pulls directly to GCS via the
#   EXTRACT job and LOAD job APIs (fast at any scale).
#
# Asset graph:
#   iris_export_to_gcs   ← bigquery_export_to_gcs_asset
#                          (BQ EXTRACT iris_clean → gs://.../bridge/iris_*.parquet)
#         │
#         └── iris_round_tripped  ← bigquery_load_from_gcs_asset
#                                   (LOAD parquet → BQ iris_round_tripped)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS   service-account JSON
#
# REQUIRED RESOURCES (created on first run if missing)
#   - BQ dataset: ${BQ_DATASET:-servicepulse-490502.dagster_demo}
#   - BQ table:   ${BQ_DATASET}.iris_clean (run setup_bigquery_ml_pipeline_demo.sh first if you haven't)
#   - GCS bucket: ${GCS_BUCKET:-servicepulse-490502-dagster-demo}
#
# COST while running
#   ~\$0. Iris is 150 rows; EXTRACT is free; LOAD is free; storage is <3 KB.

set -euo pipefail
PROJECT_DIR="${1:-bigquery-bulk-bridge-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi

DATASET="${BQ_DATASET:-servicepulse-490502.dagster_demo}"
PROJECT_ID="${DATASET%%.*}"
SOURCE_TABLE="${BQ_SOURCE_TABLE:-${DATASET}.iris_clean}"
GCS_BUCKET="${GCS_BUCKET:-${PROJECT_ID}-dagster-demo}"
GCS_PREFIX="${GCS_PREFIX:-bridge/iris}"
TARGET_TABLE="${BQ_TARGET_TABLE:-${DATASET}.iris_round_tripped}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigquery google-cloud-storage db-dtypes
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Installing BQ↔GCS bulk-bridge components"
uvx --from dagster-community-components-cli dagster-component add bigquery_export_to_gcs_asset --auto-install
uvx --from dagster-community-components-cli dagster-component add bigquery_load_from_gcs_asset  --auto-install

# Fix __init__ files (CLI doesn't always write them with the import)
echo 'from .component import BigQueryExportToGcsAssetComponent
__all__ = ["BigQueryExportToGcsAssetComponent"]' > "src/$PKG/components/bigquery_export_to_gcs_asset/__init__.py"
echo 'from .component import BigQueryLoadFromGcsAssetComponent
__all__ = ["BigQueryLoadFromGcsAssetComponent"]' > "src/$PKG/components/bigquery_load_from_gcs_asset/__init__.py"

# Asset 1: BQ EXTRACT → GCS parquet
mkdir -p "src/$PKG/defs/bigquery_export_to_gcs_asset"
cat > "src/$PKG/defs/bigquery_export_to_gcs_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_export_to_gcs_asset.component.BigQueryExportToGcsAssetComponent
attributes:
  asset_name: iris_export_to_gcs
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  source_table_id: $SOURCE_TABLE
  destination_uri: "gs://${GCS_BUCKET}/${GCS_PREFIX}_*.parquet"
  format: parquet
  compression: snappy
  overwrite: true
  description: BQ EXTRACT $SOURCE_TABLE to GCS as parquet.
  group_name: bridge
EOF

# Asset 2: GCS parquet → BQ table (the round trip)
mkdir -p "src/$PKG/defs/bigquery_load_from_gcs_asset"
cat > "src/$PKG/defs/bigquery_load_from_gcs_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_load_from_gcs_asset.component.BigQueryLoadFromGcsAssetComponent
attributes:
  asset_name: iris_round_tripped
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  source_uris:
    - "gs://${GCS_BUCKET}/${GCS_PREFIX}_*.parquet"
  destination_table_id: $TARGET_TABLE
  format: parquet
  write_disposition: WRITE_TRUNCATE
  autodetect: true
  deps: [iris_export_to_gcs]
  description: BQ LOAD parquet → $TARGET_TABLE.
  group_name: bridge
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    iris_export_to_gcs       ← bigquery_export_to_gcs_asset
                               (BQ EXTRACT $SOURCE_TABLE
                                → gs://${GCS_BUCKET}/${GCS_PREFIX}_*.parquet)
          │
          └── iris_round_tripped   ← bigquery_load_from_gcs_asset
                                     (LOAD parquet → $TARGET_TABLE)

Materialize all:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect:
    bq query --nouse_legacy_sql 'SELECT COUNT(*) FROM ${DATASET#*.}.iris_round_tripped'
    gsutil ls gs://${GCS_BUCKET}/${GCS_PREFIX}_*.parquet

Both components use BigQuery's NATIVE EXTRACT and LOAD JOB APIs —
data never round-trips through the Dagster executor (BQ pushes
directly to GCS). Same pattern works at any scale, from 150-row
iris to multi-TB tables.

Cost: ~\$0. BQ EXTRACT is free; LOAD is free; iris is <10 KB.
MSG
