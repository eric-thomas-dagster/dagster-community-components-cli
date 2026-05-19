#!/usr/bin/env bash
# Vector / RAG mega-demo — 5 vector-store + RAG components against
# a local ChromaDB using OpenAI embeddings.
#
# Pipeline:
#   knowledge_corpus (10 synthetic doc chunks)
#         │
#         └→ kb_embeddings (embeddings_generator: OpenAI text-embedding-3-small)
#                 │
#                 └→ kb_index (vector_store_writer: chromadb /tmp/chroma_db)
#                                 │
#   queries (5 synthetic questions)
#         │
#         └→ query_embeddings (embeddings_generator)
#                 │
#                 └→ search_results (vector_store_query: top_k=5 against kb_index)
#                         │
#                         └→ reranked (reranker: cross_encoder local — no Cohere key)
#
#   rag_corpus (10 docs + 3 questions in same DF)
#         │
#         └→ rag_response (rag_pipeline: end-to-end RAG — writes its own chroma DB)
#
#   chat_log (3 user/assistant turns)
#         │
#         └→ chat_history (conversation_memory: writes /tmp/chat_memory.json)
#
# REQUIRED ENV
#   OPENAI_API_KEY    OpenAI key (sk-...)
#
# COST  ~$0.05 — embeddings are ~$0.02/1M tokens; few queries.

set -euo pipefail
PROJECT_DIR="${1:-vector-rag-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai chromadb sentence-transformers
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 vector/RAG components"
$CLI add embeddings_generator --auto-install
$CLI add vector_store_writer  --auto-install
$CLI add vector_store_query   --auto-install
$CLI add reranker             --auto-install
$CLI add rag_pipeline         --auto-install
$CLI add conversation_memory  --auto-install

echo ">>> Writing inline source data assets"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


KB_DOCS = [
    "Dagster is a data orchestrator for the entire development lifecycle.",
    "Dagster components let users author reusable assets via YAML configuration.",
    "An asset in Dagster is a software-defined object — a file, table, or model.",
    "Dagster supports partitioned assets for time-windowed and segmented data.",
    "I/O managers in Dagster handle reading and writing asset data automatically.",
    "Dagster sensors trigger jobs based on external events — a new file in S3, a webhook, etc.",
    "Asset checks in Dagster validate data quality and schema integrity inline with execution.",
    "Dagster+ is the commercial offering with hosted infrastructure and observability.",
    "Resources in Dagster represent connections to external systems like databases.",
    "Schedules in Dagster fire jobs on cron expressions — daily, hourly, etc.",
]

QUERIES = [
    "What is Dagster?",
    "How do components work?",
    "What is an asset check?",
    "How does Dagster handle partitioned data?",
    "What is the difference between a sensor and a schedule?",
]

CHAT_LOG = [
    ("How do I get started with Dagster?", "Run `uvx create-dagster@latest project my-project` to scaffold a new project."),
    ("What's a component?", "A component is a reusable, YAML-configurable unit that produces Dagster definitions."),
    ("Can I use components for ingestion?", "Yes — there are dozens of community components for ingestion (S3, Kafka, REST APIs, databases)."),
]


@dg.asset(group_name="ingest")
def knowledge_corpus() -> pd.DataFrame:
    return pd.DataFrame({"text": KB_DOCS, "doc_id": range(len(KB_DOCS))})


@dg.asset(group_name="ingest")
def queries() -> pd.DataFrame:
    return pd.DataFrame({"text": QUERIES, "query_id": range(len(QUERIES))})


@dg.asset(group_name="ingest")
def chat_log() -> pd.DataFrame:
    return pd.DataFrame(CHAT_LOG, columns=["user_message", "assistant_message"])


@dg.asset(group_name="ingest")
def rag_corpus() -> pd.DataFrame:
    """RAG pipeline expects a 'query' column. Ingest 3 questions for end-to-end RAG."""
    return pd.DataFrame({
        "query": [
            "What is the relationship between Dagster components and assets?",
            "How can I add data quality checks to my Dagster assets?",
            "What is Dagster+?",
        ]
    })


defs = dg.Definitions(assets=[knowledge_corpus, queries, chat_log, rag_corpus])
PYEOF

echo ">>> Writing demo defs.yaml"

# 1. KB embeddings (OpenAI text-embedding-3-small)
cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: kb_embeddings
  upstream_asset_key: knowledge_corpus
  provider: openai
  model: text-embedding-3-small
  api_key: \${OPENAI_API_KEY}
  input_column: text
  output_column: embedding
  group_name: vector
EOF

# 2. KB index (chromadb local)
cat > "src/$PKG/defs/vector_store_writer/defs.yaml" <<EOF
type: $PKG.components.vector_store_writer.component.VectorStoreWriterComponent
attributes:
  asset_name: kb_index
  upstream_asset_key: kb_embeddings
  provider: chromadb
  collection_name: knowledge_base
  connection_string: /tmp/chroma_kb
  embedding_column: embedding
  upsert: true
  batch_size: 50
  group_name: vector
EOF

# 3. Query embeddings — make a 2nd embeddings_generator instance via different path
mkdir -p "src/$PKG/defs/query_embeddings_generator"
cat > "src/$PKG/defs/query_embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: query_embeddings
  upstream_asset_key: queries
  provider: openai
  model: text-embedding-3-small
  api_key: \${OPENAI_API_KEY}
  input_column: text
  output_column: embedding
  group_name: vector
EOF

# 4. Vector search
cat > "src/$PKG/defs/vector_store_query/defs.yaml" <<EOF
type: $PKG.components.vector_store_query.component.VectorStoreQueryComponent
attributes:
  asset_name: search_results
  upstream_asset_key: query_embeddings
  provider: chromadb
  collection_name: knowledge_base
  connection_string: /tmp/chroma_kb
  embedding_column: embedding
  query_text_column: text
  top_k: 3
  include_distances: true
  deps:
    - kb_index
  group_name: vector
EOF

# 5. Reranker (cross_encoder local — no Cohere key)
cat > "src/$PKG/defs/reranker/defs.yaml" <<EOF
type: $PKG.components.reranker.component.RerankerComponent
attributes:
  asset_name: reranked_results
  upstream_asset_key: search_results
  method: cross_encoder
  model: cross-encoder/ms-marco-MiniLM-L-6-v2
  query_column: query
  text_column: document
  top_n: 3
  group_name: vector
EOF

# 6. RAG pipeline (end-to-end — has its own collection + chroma path)
cat > "src/$PKG/defs/rag_pipeline/defs.yaml" <<EOF
type: $PKG.components.rag_pipeline.component.RAGPipelineComponent
attributes:
  asset_name: rag_response
  upstream_asset_key: rag_corpus
  vector_store_provider: chromadb
  collection_name: knowledge_base
  vector_store_connection: /tmp/chroma_kb
  llm_provider: openai
  llm_model: gpt-4o-mini
  llm_api_key: \${OPENAI_API_KEY}
  embedding_provider: openai
  embedding_model: text-embedding-3-small
  embedding_api_key: \${OPENAI_API_KEY}
  query_column: query
  answer_column: answer
  sources_column: sources
  top_k: 3
  temperature: 0.0
  deps:
    - kb_index
  group_name: rag
EOF

# 7. Conversation memory
cat > "src/$PKG/defs/conversation_memory/defs.yaml" <<EOF
type: $PKG.components.conversation_memory.component.ConversationMemoryComponent
attributes:
  asset_name: chat_history
  upstream_asset_key: chat_log
  memory_file: /tmp/chat_memory.json
  user_message_column: user_message
  assistant_message_column: assistant_message
  max_messages: 20
  include_system_prompt: true
  system_prompt: "You are a helpful Dagster assistant."
  group_name: chat
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Pipeline: 10 KB docs + 5 queries + 3 RAG questions + 3 chat turns,
all routed through ChromaDB (local) and OpenAI embeddings + chat.
Cost <\$0.05.

Inspect:
    uv run dg dev   # http://localhost:3000
MSG
