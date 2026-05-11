#!/usr/bin/env bash
# BigQuery Vector Search — k-NN similarity over a BQ embedding column.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   bigquery_vector_search_asset against a 5-row demo table with 4-dim
#   embeddings. Two query vectors (billing + account intent) return
#   semantically-correct top-2 matches each.
#
# Asset graph:
#   doc_search    ← bigquery_vector_search_asset
#                   (queries servicepulse-490502.dagster_demo.demo_docs_embedded)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#   BQ_DATASET                      destination dataset (e.g. dagster_demo)
#
# REQUIRED APIS
#   BigQuery  https://console.cloud.google.com/apis/library/bigquery.googleapis.com
#
# REQUIRED IAM
#   roles/bigquery.dataEditor (on $BQ_DATASET)
#   roles/bigquery.jobUser    (project)
#
# COST while running
#   < $0.01. 5 rows × few KB scanned per query.

set -euo pipefail
PROJECT_DIR="${1:-bq-vector-search-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
BQ_DATASET="${BQ_DATASET:-dagster_demo}"

echo ">>> Creating demo embedding table ${GCP_PROJECT_ID}.${BQ_DATASET}.demo_docs_embedded"
python3 <<PY
from google.cloud import bigquery
from google.oauth2 import service_account
creds = service_account.Credentials.from_service_account_file('$GOOGLE_APPLICATION_CREDENTIALS')
c = bigquery.Client(credentials=creds, project='$GCP_PROJECT_ID')
sql = '''
CREATE OR REPLACE TABLE \`$GCP_PROJECT_ID.$BQ_DATASET.demo_docs_embedded\` AS
SELECT * FROM UNNEST([
  STRUCT('d1' AS doc_id, 'Refund my credit card' AS content, [1.0, 0.0, 0.0, 0.0] AS embedding),
  STRUCT('d2' AS doc_id, 'Cancel my subscription' AS content, [0.9, 0.1, 0.0, 0.0] AS embedding),
  STRUCT('d3' AS doc_id, 'Reset my password' AS content,    [0.0, 0.0, 1.0, 0.0] AS embedding),
  STRUCT('d4' AS doc_id, 'Update my email address' AS content, [0.0, 0.1, 0.9, 0.0] AS embedding),
  STRUCT('d5' AS doc_id, 'Server is down 502 errors' AS content, [0.0, 0.0, 0.0, 1.0] AS embedding)
])
'''
c.query(sql).result()
print('Created with 5 rows')
PY

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigquery
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing bigquery_vector_search_asset"
$CLI add bigquery_vector_search_asset --auto-install 2>&1 | tail -2

echo 'from .component import BigqueryVectorSearchAssetComponent
__all__ = ["BigqueryVectorSearchAssetComponent"]' > "src/$PKG/components/bigquery_vector_search_asset/__init__.py"

mkdir -p "src/$PKG/defs/doc_search"
cat > "src/$PKG/defs/doc_search/defs.yaml" <<EOF
type: $PKG.components.bigquery_vector_search_asset.component.BigqueryVectorSearchAssetComponent
attributes:
  asset_name: doc_search
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  base_table: $GCP_PROJECT_ID.$BQ_DATASET.demo_docs_embedded
  base_column: embedding
  select_columns: [doc_id, content]
  top_k: 3
  distance_type: COSINE
  use_brute_force: true   # small table; force exhaustive scan
  query_vectors:
    - [0.95, 0.05, 0.0, 0.0]   # billing intent — should match d1, d2
    - [0.0, 0.05, 0.95, 0.0]   # account intent — should match d3, d4
  group_name: retrieval
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Expected output (one row per query × match):
    q0 → d1 'Refund my credit card'   (closest)
    q0 → d2 'Cancel my subscription'  (next)
    q1 → d3 'Reset my password'       (closest)
    q1 → d4 'Update my email address' (next)
MSG
