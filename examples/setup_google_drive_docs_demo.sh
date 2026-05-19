#!/usr/bin/env bash
# Google Drive + Docs demo — list Drive files, extract Doc text, summarize with Gemini.
#
# WHAT THIS DEMONSTRATES
#   Three new components chained end-to-end:
#     google_drive_ingestion → google_docs_extractor → gemini_llm
#
#   The SA lists every Doc/Sheet/file it has been shared on; the Docs
#   extractor pulls plain text from any Google Doc in that listing;
#   then gemini_llm produces a one-sentence summary per doc.
#
# Asset graph:
#   drive_listing  ← google_drive_ingestion
#         │  (filter: mimeType='application/vnd.google-apps.document')
#         ▼
#   doc_texts      ← google_docs_extractor (Docs API → plain text)
#         │
#         ▼
#   doc_summaries  ← gemini_llm (gemini-2.5-flash one-sentence summary)
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  — path to service-account JSON
#   GEMINI_API_KEY                  — Gemini API key (only for the
#                                     summarizer; first 2 assets work
#                                     without it)
#
# COST while running
#   \$0. Drive + Docs APIs are free at this volume; Gemini 2.5 Flash
#   has free-tier quota.

set -euo pipefail
PROJECT_DIR="${1:-google-drive-docs-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS to a service-account JSON path."
  exit 1
fi
if [ ! -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  echo "ERROR: \$GOOGLE_APPLICATION_CREDENTIALS=$GOOGLE_APPLICATION_CREDENTIALS does not exist."
  exit 1
fi
if [ -z "${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}" ]; then
  echo "WARNING: GEMINI_API_KEY not set — doc_summaries asset will fail."
  echo "         Get a key at https://aistudio.google.com/app/apikey"
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-api-python-client google-genai
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing google_drive_ingestion + google_docs_extractor + gemini_llm"
$CLI add google_drive_ingestion --auto-install
$CLI add google_docs_extractor  --auto-install
$CLI add gemini_llm             --auto-install

# ─── Drive listing (filtered to Google Docs only) ────────────────────────
mkdir -p "src/$PKG/defs/google_drive_ingestion"
cat > "src/$PKG/defs/google_drive_ingestion/defs.yaml" <<EOF
type: $PKG.components.google_drive_ingestion.component.GoogleDriveIngestionComponent
attributes:
  asset_name: drive_listing
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  query: "mimeType='application/vnd.google-apps.document'"
  max_files: 50
  download: false
  description: "Listing of every Google Doc the SA has been shared on."
  group_name: drive
EOF

# ─── Docs extractor (consumes drive_listing) ─────────────────────────────
mkdir -p "src/$PKG/defs/google_docs_extractor"
cat > "src/$PKG/defs/google_docs_extractor/defs.yaml" <<EOF
type: $PKG.components.google_docs_extractor.component.GoogleDocsExtractorComponent
attributes:
  asset_name: doc_texts
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  upstream_asset_key: drive_listing
  id_column: id
  include_text: true
  include_headings: true
  description: "Plain text of each Google Doc in drive_listing."
  group_name: docs
EOF

# ─── Gemini one-sentence summary per doc ─────────────────────────────────
mkdir -p "src/$PKG/defs/gemini_llm"
cat > "src/$PKG/defs/gemini_llm/defs.yaml" <<EOF
type: $PKG.components.gemini_llm.component.GeminiLLMComponent
attributes:
  asset_name: doc_summaries
  upstream_asset_key: doc_texts
  api_key_env_var: GEMINI_API_KEY
  text_model: gemini-2.5-flash
  system_prompt: "You are a doc-summary assistant. Output a single sentence under 25 words. No preamble, no headings."
  input_column: text
  output_column: summary
  max_output_tokens: 80
  temperature: 0.0
  thinking_budget: 0       # disable internal thinking — short summary only
  group_name: ai
EOF

# ─── CSV sink — writes the summarized rows to /tmp ──────────────────────
$CLI add dataframe_to_csv --auto-install
mkdir -p "src/$PKG/defs/dataframe_to_csv"
cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: doc_summaries_csv
  upstream_asset_key: doc_summaries
  file_path: /tmp/doc_summaries.csv
  include_index: false
  description: CSV export of Doc id + title + summary.
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Asset graph:
    drive_listing    ← google_drive_ingestion (filtered to Google Docs)
          │
          └── doc_texts      ← google_docs_extractor (Docs API)
                  │
                  └── doc_summaries  ← gemini_llm (one-sentence summary)
                            │
                            └── doc_summaries_csv  ← dataframe_to_csv (/tmp/doc_summaries.csv)

Materialize all four:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

After running, inspect the CSV:
    cat /tmp/doc_summaries.csv

Inspect:
    uv run dg dev   # http://localhost:3000

If a Doc returns 403 PERMISSION_DENIED, share it with the service-account
email (in your JSON's client_email field) — the component logs the exact
share URL.

If you only want Drive + Docs without the LLM step:
    uv run dg launch --assets drive_listing,doc_texts
MSG
