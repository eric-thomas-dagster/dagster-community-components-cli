#!/usr/bin/env bash
# Local NLP mega-demo — 13 NLP / lightweight-AI components running on
# synthetic support tickets. ~10 fully local (no API key), ~3 use OpenAI.
#
# Pipeline:
#   support_tickets (synthetic_data_generator: 30 rows, support_tickets schema)
#         │
#         ├── document_chunks         (document_chunker: char chunks 600/100)
#         ├── text_chunks             (text_chunker: token chunks 256/32)
#         ├── ticket_pos_tags         (part_of_speech_tagger: spaCy tagger)
#         ├── ticket_topics           (topic_modeler: LDA, 5 topics)
#         ├── ticket_word_cloud       (word_cloud: PNG output)
#         ├── ticket_similarity       (text_similarity: cosine_tfidf vs reference)
#         ├── ticket_zero_shot        (zero_shot_classifier: HF facebook/bart-large-mnli)
#         ├── parsed_tickets          (llm_output_parser: parse json from text)
#         │
#         ├── ticket_schema_fit       (schema_fit: LLM-validated schema)         [needs key]
#         ├── ticket_match_classified (precision_match: LLM standardization)     [needs key]
#         ├── ticket_classified       (ticket_classifier: LLM-mode classification) [needs key]
#         └── tickets_sql_query       (sql_generator: LLM-generated SQL)         [needs key]
#
# REQUIRED ENV
#   OPENAI_API_KEY    OpenAI key (for the 4 LLM-touching components only)
#
# COST  ~$0.05 — local NLP is free; the 4 LLM components share gpt-4o-mini calls.

set -euo pipefail
PROJECT_DIR="${1:-local-nlp-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY (needed for 4 of 13 components)"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas spacy scikit-learn wordcloud transformers torch litellm matplotlib
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
# Download spacy English model (~12MB)
uv run python -m spacy download en_core_web_sm 2>/dev/null || true

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 13 NLP / AI components"
$CLI add synthetic_data_generator --auto-install
$CLI add document_chunker         --auto-install
$CLI add text_chunker             --auto-install
$CLI add part_of_speech_tagger    --auto-install
$CLI add topic_modeler            --auto-install
$CLI add word_cloud               --auto-install
$CLI add text_similarity          --auto-install
$CLI add zero_shot_classifier     --auto-install
$CLI add llm_output_parser        --auto-install
$CLI add schema_fit               --auto-install
$CLI add precision_match          --auto-install
$CLI add ticket_classifier        --auto-install
$CLI add sql_generator            --auto-install

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

cat > "src/$PKG/defs/document_chunker/defs.yaml" <<EOF
type: $PKG.components.document_chunker.component.DocumentChunkerComponent
attributes:
  asset_name: document_chunks
  upstream_asset_key: support_tickets
  source_column: ticket_text
  strategy: recursive
  chunk_size: 200
  chunk_overlap: 50
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/text_chunker/defs.yaml" <<EOF
type: $PKG.components.text_chunker.component.TextChunkerComponent
attributes:
  asset_name: text_chunks
  upstream_asset_key: support_tickets
  source_column: ticket_text
  chunking_strategy: fixed_tokens
  chunk_size: 64
  chunk_overlap: 8
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/part_of_speech_tagger/defs.yaml" <<EOF
type: $PKG.components.part_of_speech_tagger.component.PartOfSpeechTaggerComponent
attributes:
  asset_name: ticket_pos_tags
  upstream_asset_key: support_tickets
  text_column: ticket_text
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/topic_modeler/defs.yaml" <<EOF
type: $PKG.components.topic_modeler.component.TopicModelerComponent
attributes:
  asset_name: ticket_topics
  upstream_asset_key: support_tickets
  text_column: ticket_text
  method: lda
  n_topics: 5
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/word_cloud/defs.yaml" <<EOF
type: $PKG.components.word_cloud.component.WordCloudComponent
attributes:
  asset_name: ticket_word_cloud
  upstream_asset_key: support_tickets
  text_column: ticket_text
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/text_similarity/defs.yaml" <<EOF
type: $PKG.components.text_similarity.component.TextSimilarityComponent
attributes:
  asset_name: ticket_similarity
  upstream_asset_key: support_tickets
  text_column_a: ticket_text
  query: "I cannot access my account and need urgent help"
  method: cosine_tfidf
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/zero_shot_classifier/defs.yaml" <<EOF
type: $PKG.components.zero_shot_classifier.component.ZeroShotClassifierComponent
attributes:
  asset_name: ticket_zero_shot
  upstream_asset_key: support_tickets
  text_column: ticket_text
  candidate_labels: [billing, technical, account, complaint, feature_request]
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/llm_output_parser/defs.yaml" <<EOF
type: $PKG.components.llm_output_parser.component.LLMOutputParserComponent
attributes:
  asset_name: parsed_tickets
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: parsed_text
  parser_type: list
  strip_markdown: true
  strict_validation: false
  group_name: nlp_local
EOF

cat > "src/$PKG/defs/schema_fit/defs.yaml" <<EOF
type: $PKG.components.schema_fit.component.SchemaFitComponent
attributes:
  asset_name: ticket_schema_fit
  upstream_asset_key: support_tickets
  target_schema:
    customer_intent: "primary intent of the customer (one phrase)"
    contains_pii: "true/false — does the ticket contain personal information"
    severity: "severity rating: low, medium, high, urgent"
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: nlp_llm
EOF

cat > "src/$PKG/defs/precision_match/defs.yaml" <<EOF
type: $PKG.components.precision_match.component.PrecisionMatchComponent
attributes:
  asset_name: ticket_match_classified
  upstream_asset_key: support_tickets
  column: ticket_text
  reference_values: [billing_question, technical_issue, account_access, complaint, feature_request]
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: nlp_llm
EOF

cat > "src/$PKG/defs/ticket_classifier/defs.yaml" <<EOF
type: $PKG.components.ticket_classifier.component.TicketClassifierComponent
attributes:
  asset_name: ticket_classified
  upstream_asset_key: support_tickets
  input_column: ticket_text
  method: llm
  llm_provider: openai
  llm_model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  group_name: nlp_llm
EOF

cat > "src/$PKG/defs/sql_generator/defs.yaml" <<EOF
type: $PKG.components.sql_generator.component.SqlGeneratorComponent
attributes:
  asset_name: tickets_sql_query
  upstream_asset_key: support_tickets
  question_column: ticket_text
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: nlp_llm
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

13 NLP components on 30 synthetic tickets. ~9 local + ~4 LLM (gpt-4o-mini, <\$0.05).

Inspect:
    uv run dg dev
MSG
