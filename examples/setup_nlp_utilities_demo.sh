#!/usr/bin/env bash
# NLP utilities demo — 6 NLP utility components.
#
# WHAT THIS DEMONSTRATES
#   The standalone NLP utility family. Mostly local (NLTK + sklearn +
#   spaCy + pandas), with one LLM-backed source (synthetic_data uses
#   gpt-4o-mini via the OPENAI_API_KEY env var to generate article rows).
#
# Asset graph:
#   articles (synthetic_data — 50 rows of synthetic content)
#   document_pairs (manually crafted Q&A pairs)
#         │
#         ├── chunked_articles            ← document_chunker (recursive split)
#         ├── article_word_frequencies    ← word_cloud (top-N word frequencies)
#         ├── pos_tagged_articles         ← part_of_speech_tagger (NLTK POS tags)
#         ├── topic_modeled_articles      ← topic_modeler (sklearn LDA)
#         └── qa_similarity_scores        ← text_similarity (cosine TF-IDF)
#
# COST: \$0 — local NLTK + sklearn + faker.

set -euo pipefail
PROJECT_DIR="${1:-nlp-utilities-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas nltk scikit-learn faker numpy spacy
uv add --dev -q dagster-dg-cli dagster-webserver

# Download the spaCy model that part_of_speech_tagger needs.
echo ">>> Downloading spaCy en_core_web_sm model"
uv run python -m spacy download en_core_web_sm 2>&1 | tail -3 || {
  echo "WARN: spaCy model download failed — pos_tagged_articles will fail."
  echo "      Run manually: uv run python -m spacy download en_core_web_sm"
}

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 6 NLP utility components"
$CLI add synthetic_data            --auto-install
$CLI add document_chunker          --auto-install
$CLI add word_cloud                --auto-install
$CLI add part_of_speech_tagger     --auto-install
$CLI add topic_modeler             --auto-install
$CLI add text_similarity           --auto-install

echo ">>> Writing inline source assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


# Document pairs for text_similarity. Use a few hand-crafted Q&A pairs
# so the similarity scores are interpretable.
@dg.asset(group_name="ingest")
def document_pairs() -> pd.DataFrame:
    return pd.DataFrame([
        {"pair_id": 1, "question": "How do I reset my password?",
         "answer": "Click the Forgot Password link on the login page."},
        {"pair_id": 2, "question": "What is your refund policy?",
         "answer": "We offer 30-day money-back guarantee on all plans."},
        {"pair_id": 3, "question": "Where can I find API documentation?",
         "answer": "API docs are available at docs.example.com/api."},
        {"pair_id": 4, "question": "How do I export my data?",
         "answer": "Go to Settings → Export Data → choose CSV or JSON."},
        {"pair_id": 5, "question": "Is there a free trial?",
         "answer": "Yes, all paid plans come with a 14-day free trial."},
        {"pair_id": 6, "question": "How do I cancel my subscription?",
         "answer": "You can cancel anytime from Settings → Billing."},
        # Mismatched pair (low similarity expected)
        {"pair_id": 7, "question": "How do I reset my password?",
         "answer": "Our office is in San Francisco, California."},
    ])


defs = dg.Definitions(assets=[document_pairs])
PYEOF

echo ">>> Writing 6 NLP defs.yaml"

# synthetic_data is the source for the other 4 NLP components
cat > "src/$PKG/defs/synthetic_data/defs.yaml" <<EOF
type: $PKG.components.synthetic_data.component.SyntheticDataComponent
attributes:
  asset_name: articles
  schema:
    article_id: "UUID string"
    title: "short marketing-style title (5-8 words)"
    body: "two-paragraph article on a software topic — APIs, databases, security, machine learning, devops"
    category: "one of: api, database, security, ml, devops"
    word_count: "integer between 100 and 400"
  n_rows: 30
  api_key_env_var: OPENAI_API_KEY
  context: "software-engineering articles"
  group_name: ingest
EOF

cat > "src/$PKG/defs/document_chunker/defs.yaml" <<EOF
type: $PKG.components.document_chunker.component.DocumentChunkerComponent
attributes:
  asset_name: chunked_articles
  upstream_asset_key: articles
  source_column: body
  output_column: chunk
  strategy: recursive
  chunk_size: 200
  chunk_overlap: 30
  group_name: nlp
EOF

cat > "src/$PKG/defs/word_cloud/defs.yaml" <<EOF
type: $PKG.components.word_cloud.component.WordCloudComponent
attributes:
  asset_name: article_word_frequencies
  upstream_asset_key: articles
  text_column: body
  output_mode: top_n
  language: english
  top_n: 50
  min_word_length: 3
  group_name: nlp
EOF

cat > "src/$PKG/defs/part_of_speech_tagger/defs.yaml" <<EOF
type: $PKG.components.part_of_speech_tagger.component.PartOfSpeechTaggerComponent
attributes:
  asset_name: pos_tagged_articles
  upstream_asset_key: articles
  text_column: body
  output_mode: counts
  group_name: nlp
EOF

cat > "src/$PKG/defs/topic_modeler/defs.yaml" <<EOF
type: $PKG.components.topic_modeler.component.TopicModelerComponent
attributes:
  asset_name: topic_modeled_articles
  upstream_asset_key: articles
  text_column: body
  output_column: topic_id
  topic_label_column: topic_keywords
  n_topics: 5
  method: lda
  group_name: nlp
EOF

cat > "src/$PKG/defs/text_similarity/defs.yaml" <<EOF
type: $PKG.components.text_similarity.component.TextSimilarityComponent
attributes:
  asset_name: qa_similarity_scores
  upstream_asset_key: document_pairs
  text_column_a: question
  text_column_b: answer
  output_column: similarity_score
  method: cosine_tfidf
  group_name: nlp
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Browse:
    uv run dg dev   # http://localhost:3000

Note: synthetic_data calls OpenAI (gpt-4o-mini) to generate article
rows matching the YAML-declared schema. The other 5 components are
fully local (NLTK + sklearn + spaCy + pandas).
MSG
