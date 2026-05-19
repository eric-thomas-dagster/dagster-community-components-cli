#!/usr/bin/env bash
# AI pipeline — no LLM API key required.
#
# WHAT THIS DEMONSTRATES
#   Five AI components running locally on synthetic support tickets — no
#   OpenAI/Anthropic key needed. Showcases the registry's "lightweight AI"
#   layer: NLP feature extraction, language detection, PII detection,
#   PII redaction, embeddings — all using local models.
#
# Pipeline:
#   support_tickets (custom inline asset, 30 synthetic tickets with text + emails)
#         │
#         ├── keyword_extractor   → TF-IDF top keywords per ticket
#         ├── language_detector   → ISO 639-1 codes (en/es/fr/de/...)
#         ├── pii_detector         → counts of emails / phones / names
#         ├── pii_redactor         → ticket_text with PII masked
#         └── embeddings_generator → 384-dim sentence embeddings (local sentence-transformers)
#
# COST: $0 — all local

set -euo pipefail
PROJECT_DIR="${1:-ai-no-llm-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas scikit-learn langdetect presidio-analyzer presidio-anonymizer sentence-transformers
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add keyword_extractor       --auto-install
$CLI add language_detector       --auto-install
$CLI add pii_detector            --auto-install
$CLI add pii_redactor            --auto-install
$CLI add embeddings_generator    --auto-install

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets       # built-in support_tickets schema (multilingual + PII)
  row_count: 30
  random_state: 42
  description: 30 synthetic multilingual support tickets with embedded PII
  group_name: ingest
EOF

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/keyword_extractor/defs.yaml" <<EOF
type: $PKG.components.keyword_extractor.component.KeywordExtractorComponent
attributes:
  asset_name: ticket_keywords
  upstream_asset_key: support_tickets
  input_column: ticket_text
  group_name: ai
EOF

cat > "src/$PKG/defs/language_detector/defs.yaml" <<EOF
type: $PKG.components.language_detector.component.LanguageDetectorComponent
attributes:
  asset_name: ticket_languages
  upstream_asset_key: support_tickets
  input_column: ticket_text
  group_name: ai
EOF

cat > "src/$PKG/defs/pii_detector/defs.yaml" <<EOF
type: $PKG.components.pii_detector.component.PiiDetectorComponent
attributes:
  asset_name: ticket_pii_counts
  upstream_asset_key: support_tickets
  input_column: ticket_text
  group_name: ai
EOF

cat > "src/$PKG/defs/pii_redactor/defs.yaml" <<EOF
type: $PKG.components.pii_redactor.component.PiiRedactorComponent
attributes:
  asset_name: ticket_redacted
  upstream_asset_key: support_tickets
  input_column: ticket_text
  group_name: ai
EOF

cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: ticket_embeddings
  upstream_asset_key: support_tickets
  provider: sentence_transformers     # local — no API key
  model: all-MiniLM-L6-v2
  input_column: ticket_text
  output_column: embedding
  group_name: ai
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

The pipeline runs 5 AI components in parallel against the synthetic
support tickets. None require an API key.

Verify:
    uv run dg dev   # http://localhost:3000 → Assets graph

Next: see ai_with_llm.md for the LLM-powered pipeline that uses the
same upstream asset + opensumns/Claude key.
MSG
