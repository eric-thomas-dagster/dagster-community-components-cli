#!/usr/bin/env bash
# BigQuery query asset demo — run SQL against BigQuery's public Shakespeare dataset.
#
# WHAT THIS DEMONSTRATES
#   The new bigquery_query_asset component running a real query against
#   bigquery-public-data.samples.shakespeare. Service-account auth via
#   GOOGLE_APPLICATION_CREDENTIALS. Asset metadata exposes
#   bytes_processed / bytes_billed / cache_hit / slot_millis.
#
# Asset graph:
#   top_shakespeare_words   ← bigquery_query_asset (public BQ dataset)
#         │
#         └── word_summary  ← pandas (occurrences-vs-word_length analysis)
#
# REQUIRED ENV VAR
#   GOOGLE_APPLICATION_CREDENTIALS  Path to service-account JSON.
#                                    SA needs roles/bigquery.jobUser
#                                    (or roles/owner) on the project.
#
# COST while running
#   ~\$0.0001. The query scans ~2.6 MB; on-demand pricing is ~\$5/TB.

set -euo pipefail
PROJECT_DIR="${1:-bigquery-query-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON path."
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-bigquery db-dtypes
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing bigquery_query_asset"
$CLI add bigquery_query_asset --auto-install

# Top Shakespeare words >4 chars, top 20.
mkdir -p "src/$PKG/defs/bigquery_query_asset"
cat > "src/$PKG/defs/bigquery_query_asset/defs.yaml" <<EOF
type: $PKG.components.bigquery_query_asset.component.BigQueryQueryAssetComponent
attributes:
  asset_name: top_shakespeare_words
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  query: |
    SELECT word, SUM(word_count) AS occurrences, LENGTH(word) AS word_length
    FROM \`bigquery-public-data.samples.shakespeare\`
    WHERE LENGTH(word) > {min_word_length}
    GROUP BY word
    ORDER BY occurrences DESC
    LIMIT {top_n}
  query_params:
    top_n: 20
    min_word_length: 4
  description: Most-frequent Shakespeare words from BigQuery's public sample dataset.
  group_name: warehouse
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset:
    top_shakespeare_words   ← bigquery_query_asset (public BQ dataset)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect (asset metadata shows bytes_processed, cache_hit, slot_millis,
the rendered query, and a markdown preview of the result):
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.0001 per run (2.6 MB scanned).
MSG
