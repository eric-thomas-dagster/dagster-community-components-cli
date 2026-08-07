#!/usr/bin/env bash
# PDF text extraction demo — canonical create-dagster + dg.
#
# Downloads two famous ML papers from arXiv (Attention Is All You Need
# + Pre-train, Prompt, Predict survey), extracts their text via pdfplumber,
# computes word/page counts, writes a per-paper summary CSV. Demonstrates
# the document → text-extract → summarize pattern.
#
# Pipeline (4 components, all autoloaded by `dg`):
#     file_ingestion → pdf_text_extractor → formula → dataframe_to_csv

set -euo pipefail

PROJECT_DIR="${1:-arxiv-pdf-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

echo ">>> Adding runtime + dev deps"
uv add -q pandas requests pdfplumber
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Pre-downloading two arXiv PDFs (so the asset reads file paths, not URLs)"
mkdir -p $PROJECT_ABS/out/arxiv_papers
curl -sf -o $PROJECT_ABS/out/arxiv_papers/attention_is_all_you_need.pdf https://arxiv.org/pdf/1706.03762
curl -sf -o $PROJECT_ABS/out/arxiv_papers/pre_train_prompt_predict.pdf https://arxiv.org/pdf/2107.13586

echo ">>> Writing input manifest CSV"
cat > $PROJECT_ABS/out/arxiv_papers/manifest.csv <<EOF
paper,path
Attention Is All You Need,$PROJECT_ABS/out/arxiv_papers/attention_is_all_you_need.pdf
Pre-train Prompt and Predict,$PROJECT_ABS/out/arxiv_papers/pre_train_prompt_predict.pdf
EOF

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components into src/$PKG/components/ + defs/"
$CLI add file_ingestion    --auto-install
$CLI add pdf_text_extractor    --auto-install
$CLI add formula               --auto-install
$CLI add dataframe_to_csv      --auto-install

echo ">>> Writing demo defs.yaml for each component"

cat > "src/$PKG/defs/file_ingestion/defs.yaml" <<EOF
type: $PKG.components.file_ingestion.component.FileIngestionComponent
attributes:
  asset_name: papers_manifest
  file_path: out/arxiv_papers/manifest.csv
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
  file_path: out/arxiv_summary.csv
  include_index: false
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Output: $PROJECT_ABS/out/arxiv_summary.csv — each paper's title, path, char count,
and word count (text dropped after counting to keep the report small).

Inspect:
    cat $PROJECT_ABS/out/arxiv_summary.csv
MSG
