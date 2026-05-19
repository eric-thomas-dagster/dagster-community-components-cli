#!/usr/bin/env bash
# GCS round-trip — DataFrame → GCS parquet → BQ → BQ EXPORT → GCS csv.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   Three GCS/BQ ingest+sink components chained:
#     dataframe_to_gcs              writes parquet
#     bigquery_load_from_gcs_asset  loads parquet → BQ table
#     bigquery_export_to_gcs_asset  exports aggregate query → CSV
#
# Asset graph:
#   sales                  (10 synthetic sales rows)
#         │
#         └── sales_in_gcs     ← dataframe_to_gcs (parquet)
#                  │
#                  └── sales_in_bq      ← bigquery_load_from_gcs_asset
#                           │
#                           └── sales_exported  ← bigquery_export_to_gcs_asset
#                                                  (aggregate query → CSV)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#   GCS_BUCKET                      bucket name (no gs:// prefix)
#   BQ_DATASET                      destination BQ dataset
#
# REQUIRED APIS
#   Cloud Storage    https://console.cloud.google.com/apis/library/storage.googleapis.com
#   BigQuery         https://console.cloud.google.com/apis/library/bigquery.googleapis.com
#
# REQUIRED IAM
#   roles/storage.objectAdmin     on the bucket (write + read)
#   roles/bigquery.dataEditor     on the dataset
#   roles/bigquery.jobUser        project-level
#
# PRE-PROVISIONING (one-time)
#   gcloud storage buckets create gs://$GCS_BUCKET --project=$GCP_PROJECT_ID --location=us-central1
#   bq --location=US mk --dataset $GCP_PROJECT_ID:$BQ_DATASET
#
# COST while running
#   < $0.01. Storage at-rest pennies; BQ load + export are free; query is < 1MB.

set -euo pipefail
PROJECT_DIR="${1:-gcs-roundtrip-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
if [ -z "${GCS_BUCKET:-}" ]; then
  echo "ERROR: set GCS_BUCKET (no gs:// prefix)"; exit 1
fi
BQ_DATASET="${BQ_DATASET:-dagster_demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas pyarrow gcsfs google-auth google-cloud-storage google-cloud-bigquery
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add synthetic_data_generator        --auto-install 2>&1 | tail -2
$CLI add dataframe_to_gcs                --auto-install 2>&1 | tail -2
$CLI add bigquery_load_from_gcs_asset    --auto-install 2>&1 | tail -2
$CLI add bigquery_export_to_gcs_asset    --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticDataGeneratorComponent
__all__ = ["SyntheticDataGeneratorComponent"]' > "src/$PKG/components/synthetic_data_generator/__init__.py"
echo 'from .component import DataframeToGcsComponent
__all__ = ["DataframeToGcsComponent"]' > "src/$PKG/components/dataframe_to_gcs/__init__.py"
echo 'from .component import BigQueryLoadFromGcsAssetComponent
__all__ = ["BigQueryLoadFromGcsAssetComponent"]' > "src/$PKG/components/bigquery_load_from_gcs_asset/__init__.py"
echo 'from .component import BigQueryExportToGcsAssetComponent
__all__ = ["BigQueryExportToGcsAssetComponent"]' > "src/$PKG/components/bigquery_export_to_gcs_asset/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_data_generator" "src/$PKG/defs/dataframe_to_gcs" "src/$PKG/defs/bigquery_load_from_gcs_asset" "src/$PKG/defs/bigquery_export_to_gcs_asset"

# 1) Upstream: 20 synthetic transactions
mkdir -p "src/$PKG/defs/transactions"
cat > "src/$PKG/defs/transactions/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: transactions
  schema_type: transactions
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

# 2) Write to GCS as parquet
mkdir -p "src/$PKG/defs/transactions_in_gcs"
cat > "src/$PKG/defs/transactions_in_gcs/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_gcs.component.DataframeToGcsComponent
attributes:
  asset_name: transactions_in_gcs
  upstream_asset_key: transactions
  bucket_env_var: GCS_BUCKET
  blob_path: transactions/transactions.parquet
  format: parquet
  credentials_env_var: GOOGLE_APPLICATION_CREDENTIALS
  project_env_var: GCP_PROJECT_ID
  group_name: warehouse
EOF

# 3) Load from GCS into BQ
mkdir -p "src/$PKG/defs/transactions_in_bq"
cat > "src/$PKG/defs/transactions_in_bq/defs.yaml" <<EOF
type: $PKG.components.bigquery_load_from_gcs_asset.component.BigQueryLoadFromGcsAssetComponent
attributes:
  asset_name: transactions_in_bq
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  source_uris:
    - gs://$GCS_BUCKET/transactions/transactions.parquet
  destination_table_id: $GCP_PROJECT_ID.$BQ_DATASET.transactions_from_gcs
  format: parquet
  write_disposition: WRITE_TRUNCATE
  create_disposition: CREATE_IF_NEEDED
  autodetect: true
  deps: [transactions_in_gcs]
  group_name: warehouse
EOF

# 4) Export aggregate back to GCS as CSV
mkdir -p "src/$PKG/defs/transactions_exported"
cat > "src/$PKG/defs/transactions_exported/defs.yaml" <<EOF
type: $PKG.components.bigquery_export_to_gcs_asset.component.BigQueryExportToGcsAssetComponent
attributes:
  asset_name: transactions_exported
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  source_query: |
    SELECT category, SUM(amount) AS total_amount, COUNT(*) AS n
    FROM \`$GCP_PROJECT_ID.$BQ_DATASET.transactions_from_gcs\`
    GROUP BY category
    ORDER BY n DESC
  destination_uri: gs://$GCS_BUCKET/transactions/summary_*.csv
  format: csv
  csv_print_header: true
  deps: [transactions_in_bq]
  group_name: warehouse
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    transactions       ← synthetic_data_generator (transactions, 20 rows)
          │
          └── sales_in_gcs     ← dataframe_to_gcs (parquet)
                  │
                  └── sales_in_bq      ← bigquery_load_from_gcs_asset
                           │
                           └── sales_exported  ← bigquery_export_to_gcs_asset
                                                  (aggregate → CSV)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify:
    gcloud storage cat gs://$GCS_BUCKET/sales/sales_summary_000000000000.csv
    bq query --use_legacy_sql=false \\
      "SELECT * FROM \\\`$GCP_PROJECT_ID.$BQ_DATASET.sales_from_gcs\\\` LIMIT 10"
MSG
