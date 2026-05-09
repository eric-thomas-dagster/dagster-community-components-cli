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

# Downstream pandas analysis
mkdir -p "src/$PKG/defs/word_summary"
cat > "src/$PKG/defs/word_summary/definitions.py" <<'PYEOF'
"""Bucket the top Shakespeare words by length and report counts."""
import pandas as pd
import dagster as dg
from dagster import AssetExecutionContext, AssetIn


@dg.asset(
    key=dg.AssetKey(["word_summary"]),
    description="Word-length-bucket counts and total occurrences for the top Shakespeare words.",
    group_name="downstream",
    kinds={"pandas"},
    ins={"top_shakespeare_words": AssetIn(key=dg.AssetKey(["top_shakespeare_words"]))},
)
def word_summary(top_shakespeare_words: pd.DataFrame) -> pd.DataFrame:
    df = top_shakespeare_words
    if df.empty:
        return pd.DataFrame()
    summary = df.groupby("word_length").agg(
        word_count=("word", "count"),
        total_occurrences=("occurrences", "sum"),
        sample_words=("word", lambda s: list(s.head(3))),
    ).reset_index().sort_values("word_length")
    return summary


defs = dg.Definitions(assets=[word_summary])
PYEOF

cat <<MSG

>>> Setup complete.

Asset graph:
    top_shakespeare_words   ← bigquery_query_asset (public BQ dataset)
          │
          └── word_summary  ← pandas (bucket by word length)

Materialize all:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Inspect (asset metadata shows bytes_processed, cache_hit, slot_millis,
the rendered query, and a markdown preview of the result):
    uv run dg dev   # http://localhost:3000

Cost: ~\$0.0001 per run (2.6 MB scanned).
MSG
