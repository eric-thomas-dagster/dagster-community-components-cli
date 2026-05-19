#!/usr/bin/env bash
# AI pipeline — LLM-powered (OpenAI or Azure OpenAI).
#
# WHAT THIS DEMONSTRATES
#   Synthetic support tickets → 5 LLM-powered components running against
#   OpenAI's API. Each component exercises a different real-world LLM use
#   case: classification, extraction, summarization, sentiment, generic
#   chat completion. All use the same OPENAI_API_KEY env var.
#
# Pipeline:
#   support_tickets (synthetic_data_generator with schema_type=support_tickets)
#         │
#         ├── text_classifier      → priority bucket per ticket
#         ├── entity_extractor      → extracted entities (people, orgs, products)
#         ├── sentiment_analyzer    → sentiment + confidence
#         ├── document_summarizer   → 1-line summary per ticket
#         └── data_enricher         → LLM-derived 'urgent_action_required' yes/no
#
# REQUIRED ENV VARS
#   OPENAI_API_KEY      OpenAI API key (sk-...)
#
#   # OR for Azure OpenAI (uses the same components — they read these env vars):
#   OPENAI_AZURE_ENDPOINT    https://<your-resource>.openai.azure.com
#   OPENAI_AZURE_API_VERSION 2024-10-21
#   OPENAI_AZURE_USE_ENTRA   1            # if using managed identity / SP auth
#
# COST while running
#   ~$0.10–$0.50 against gpt-4o-mini for 30 tickets × 5 components.

set -euo pipefail
PROJECT_DIR="${1:-ai-with-llm-demo}"

if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${OPENAI_AZURE_ENDPOINT:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY (or the OPENAI_AZURE_* env vars for Azure OpenAI)"
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add text_classifier          --auto-install
$CLI add entity_extractor         --auto-install
$CLI add sentiment_analyzer       --auto-install
$CLI add document_summarizer      --auto-install
$CLI add data_enricher            --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 30
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/text_classifier/defs.yaml" <<EOF
type: $PKG.components.text_classifier.component.TextClassifierComponent
attributes:
  asset_name: ticket_priorities
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: priority
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  categories: [low, medium, high, urgent]
  classification_task: "Classify the support ticket priority. urgent = outage/security/financial. high = significant impact. medium = mild issue. low = informational."
  group_name: ai
EOF

cat > "src/$PKG/defs/entity_extractor/defs.yaml" <<EOF
type: $PKG.components.entity_extractor.component.EntityExtractorComponent
attributes:
  asset_name: ticket_entities
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: entities
  llm_provider: openai
  llm_model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  entity_types: "person,organization,product,order_id,email"
  group_name: ai
EOF

cat > "src/$PKG/defs/sentiment_analyzer/defs.yaml" <<EOF
type: $PKG.components.sentiment_analyzer.component.SentimentAnalyzerComponent
attributes:
  asset_name: ticket_sentiment
  upstream_asset_key: support_tickets
  input_column: ticket_text
  method: llm
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  group_name: ai
EOF

cat > "src/$PKG/defs/document_summarizer/defs.yaml" <<EOF
type: $PKG.components.document_summarizer.component.DocumentSummarizerComponent
attributes:
  asset_name: ticket_summaries
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: summary
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  summary_type: one_sentence
  max_length: 50
  group_name: ai
EOF

cat > "src/$PKG/defs/data_enricher/defs.yaml" <<EOF
type: $PKG.components.data_enricher.component.DataEnricherComponent
attributes:
  asset_name: ticket_urgency
  upstream_asset_key: support_tickets
  api_key_env_var: OPENAI_API_KEY
  model: gpt-4o-mini
  context_columns: [ticket_text]
  enrichment_fields:
    urgent_action_required: "Output 'yes' or 'no'. yes = customer needs urgent response within 1 hour (outage, security, financial, safety). no = otherwise. Just 'yes' or 'no', nothing else."
    primary_intent: "What is the customer's primary intent? Use one of: bug_report, feature_request, billing, account_management, support_question, complaint, refund, other."
  group_name: ai
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

The pipeline will fan out 5 LLM calls per ticket × 30 tickets = ~150
API calls against gpt-4o-mini. Should cost < \$0.50 total.

Inspect:
    uv run dg dev   # http://localhost:3000 → Assets graph

To use Azure OpenAI instead, set the OPENAI_AZURE_* env vars before
running — the components will route through Azure (same code path,
because of the unified _make_openai_client helper).
MSG
