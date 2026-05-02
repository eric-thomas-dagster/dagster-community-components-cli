#!/usr/bin/env bash
# PDF text extraction demo — canonical create-dagster + dg.
#
# Downloads two famous ML papers from arXiv (Attention Is All You Need
# + Pre-train, Prompt, Predict survey), extracts their text via pdfplumber,
# computes word/page counts, writes a per-paper summary CSV. Demonstrates
# the document → text-extract → summarize pattern.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     csv_file_ingestion → pdf_text_extractor → formula → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-arxiv-pdf-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests pdfplumber
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Pre-downloading two arXiv PDFs (so the asset reads file paths, not URLs)"
mkdir -p /tmp/arxiv_papers
curl -sf -o /tmp/arxiv_papers/attention_is_all_you_need.pdf https://arxiv.org/pdf/1706.03762
curl -sf -o /tmp/arxiv_papers/pre_train_prompt_predict.pdf https://arxiv.org/pdf/2107.13586

echo ">>> Writing input manifest CSV"
cat > /tmp/arxiv_papers/manifest.csv <<EOF
paper,path
Attention Is All You Need,/tmp/arxiv_papers/attention_is_all_you_need.pdf
Pre-train Prompt and Predict,/tmp/arxiv_papers/pre_train_prompt_predict.pdf
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/defs/"
$CLI add csv_file_ingestion    --auto-install
$CLI add pdf_text_extractor    --auto-install
$CLI add formula               --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: papers_manifest
  file_path: /tmp/arxiv_papers/manifest.csv
  description: Two arXiv papers (manifest of paper title + local path)
  group_name: ingest
EOF

cat > "src/$PKG/defs/pdf_text_extractor/defs.yaml" <<EOF
type: $PKG.components.pdf_text_extractor.component.PdfTextExtractorComponent
attributes:
  asset_name: papers_text
  upstream_asset_key: papers_manifest
  column: path
  output_column: text
  group_name: parse
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: papers_summary
  upstream_asset_key: papers_text
  expressions:
    char_count: "text.str.len()"
    word_count: "text.str.split().str.len()"
  drop_source_columns: [text]
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: papers_report
  upstream_asset_key: papers_summary
  file_path: /tmp/arxiv_summary.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: /tmp/arxiv_summary.csv — each paper's title, path, char count,
and word count (text dropped after counting to keep the report small).

Inspect:
    cat /tmp/arxiv_summary.csv
MSG
