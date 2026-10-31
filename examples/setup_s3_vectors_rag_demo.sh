#!/usr/bin/env bash
# setup_s3_vectors_rag_demo.sh
#
# AWS S3 Vectors (GA since 2025-12-02) is native vector storage in S3 -- no
# separate vector database to run or pay for if you're already on AWS. This
# demo is a real embed → write → query RAG round-trip against it, using the
# new `provider: s3vectors` option on the existing VectorStoreWriterComponent
# / VectorStoreQueryComponent (not a dedicated component -- same dispatch
# pattern already used for chromadb/pinecone/qdrant/faiss).
#
#   support_tickets_corpus → ticket_embeddings → s3_vector_index → ticket_search_results
#      (synth)              (local embedder)      (S3 Vectors write)   (S3 Vectors query)
#
# demo_mode on both the writer and query steps means the whole thing runs
# against a local JSON file instead of a real AWS account -- no cloud spend,
# no credentials. Querying asserts the round-trip actually worked (every
# ticket's own embedding comes back as its own closest match).
#
# IMPORTANT: the query step declares `deps: [s3_vector_index]` even though it
# doesn't read that asset's data -- without it, Dagster can run the query
# BEFORE the write finishes in a multi-asset job (they're otherwise
# independent branches off the same embeddings asset). This is a real
# ordering gotcha worth keeping if you adapt this demo.
#
# What it demonstrates
#   • synthetic_data_generator — sample support-ticket corpus
#   • embeddings_generator (sentence_transformers provider — local, no API key)
#   • vector_store_writer (provider: s3vectors, demo_mode)
#   • vector_store_query (provider: s3vectors, demo_mode)
#
# Cost: $0 — demo_mode on both s3vectors steps; sentence-transformers runs
# locally (downloads a small model on first run, no account needed).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_s3_vectors_rag_demo.sh                  # → s3_vectors_demo/
#   ./setup_s3_vectors_rag_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-s3_vectors_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, numpy, boto3, sentence-transformers)…"
uv add -q pandas numpy boto3 sentence-transformers
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add embeddings_generator --auto-install
$CLI add vector_store_writer --auto-install
$CLI add vector_store_query --auto-install

info "Writing defs.yaml (synth → embed → write [s3vectors] → query [s3vectors])"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets_corpus
  schema_type: support_tickets
  row_count: 8
  random_state: 7
  group_name: source
EOF

cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: ticket_embeddings
  upstream_asset_key: support_tickets_corpus
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  input_column: ticket_text
  output_column: embedding
  group_name: embeddings
EOF

cat > "src/$PKG/defs/vector_store_writer/defs.yaml" <<EOF
type: $PKG.components.vector_store_writer.component.VectorStoreWriterComponent
attributes:
  asset_name: s3_vector_index
  upstream_asset_key: ticket_embeddings
  provider: s3vectors
  collection_name: support-tickets
  vector_bucket_name: demo-support-tickets
  aws_region: us-east-2
  embedding_column: embedding
  text_column: ticket_text
  metadata_columns: [priority, channel]
  connection_string: ./s3vectors_demo.json
  group_name: vector_storage
  demo_mode: true
EOF

cat > "src/$PKG/defs/vector_store_query/defs.yaml" <<EOF
type: $PKG.components.vector_store_query.component.VectorStoreQueryComponent
attributes:
  asset_name: ticket_search_results
  upstream_asset_key: ticket_embeddings
  provider: s3vectors
  collection_name: support-tickets
  vector_bucket_name: demo-support-tickets
  aws_region: us-east-2
  connection_string: ./s3vectors_demo.json
  embedding_column: embedding
  query_text_column: ticket_text
  top_k: 3
  group_name: retrieval
  demo_mode: true
  # See the header comment: without this, Dagster can run the query before
  # the write finishes (they're otherwise independent branches).
  deps: [s3_vector_index]
EOF

ok "Wrote 4 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain (downloads the sentence-transformers model on first run)"
uv run dg launch --assets '*' 2>&1 | tail -20

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated 8 synthetic support tickets
  2. Embedded each ticket locally (sentence-transformers, no API key)
  3. Wrote all 8 vectors to a local JSON file standing in for an S3 Vectors
     index (s3_vector_index)
  4. Queried that same index using each ticket's own embedding — top_k=3
     results per query (24 total), ranked by real cosine similarity

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click ticket_search_results → each query's top match should be itself
      (distance ≈ 0), proving the write → query round-trip is correct

Point at a real AWS account:
  Set demo_mode: false on BOTH vector_store_writer and vector_store_query,
  plus a real vector_bucket_name your AWS credentials can write to. The
  bucket + index are created automatically on first write if they don't
  already exist (dataType: float32, distanceMetric: cosine).

Production variants:
  • Swap sentence_transformers for provider: openai (needs OPENAI_API_KEY)
    for higher-quality embeddings
  • Swap synthetic_data_generator for your real document source
  • Add filter_metadata to vector_store_query (e.g. '{"priority": "high"}')
    — it maps directly onto S3 Vectors' own filter parameter
EOF
