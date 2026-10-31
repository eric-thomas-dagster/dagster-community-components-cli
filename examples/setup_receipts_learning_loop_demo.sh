#!/usr/bin/env bash
# setup_receipts_learning_loop_demo.sh
#
# The "context receipts" closed loop, real end-to-end: a RAG pipeline writes a
# receipt (which chunks were retrieved, for which query, with what scores) on
# every answer, real user feedback (thumbs up/down) is ingested against those
# receipts, the store computes a learned per-chunk "prior" from that feedback,
# and the next retrieval round reads those priors back in via rerank. This is
# the "receipt store as a platform moat" idea -- Dagster owning both the
# audit trail (receipts) and the learning signal (priors) underneath an
# agent/RAG stack, not just running the pipeline.
#
#   support_tickets_corpus (synth)
#          │
#          ├── support_tickets_with_chunk_ids (formula: chunk_id = ticket_id)
#                 │
#                 ├── ticket_embeddings (sentence-transformers, local)
#                 │        │
#                 │        └── support_kb_index (chromadb, local)
#                 │
#                 └── support_answers (rag_pipeline: retrieve + receipts, using
#                       each ticket's own text as its query -- self-retrieval,
#                       same validation trick as the S3 Vectors demo)
#
#   [outside Dagster] query receipts.sqlite3 for this run's receipt_ids,
#   simulate thumbs up/down feedback → feedback_with_receipts.csv
#          │
#          ├── receipt_outcome_ingestion (writes outcome events to the store)
#          │
#   [outside Dagster] store.refresh_item_scores() -- recomputes learned priors
#   from receipts + outcomes (the same call context_engineering_pipeline's own
#   `receipts:` block makes internally; done directly here since this demo
#   doesn't use that heavier chunking/dedup component)
#          │
#          └── support_answers (re-run) -- retrieval now widens its candidate
#                pool (fetch_multiplier) and reorders by the learned priors
#
# IMPORTANT -- read before adapting this demo:
#   • rag_pipeline's receipts.rerank only WORKS if the vector store's document
#     metadata carries a stable `chunk_id` key -- that's the literal field
#     name `_doc_chunk_id()` looks for. vector_store_writer doesn't add one on
#     its own, so this demo inserts a `formula` step first
#     (chunk_id: ticket_id) and passes `metadata_columns: [chunk_id]`. Skip
#     this and every retrieved doc gets a synthetic `__noid_N` id that's
#     silently excluded from receipt items and learning.
#   • `get_item_scores()` only reads the `context_item_scores` table -- it
#     does NOT compute it. Something has to call `store.refresh_item_scores()`
#     (context_engineering_pipeline's own `receipts:` block does this
#     internally during its build step). This demo calls it directly in
#     Python since it isn't using that component.
#   • On this tiny 10-document, self-retrieval corpus, the #1 rank is always
#     the ticket's own exact text (cosine ≈ 1.0) -- no learned prior at a
#     sane prior_weight can ever displace a perfect match. The loop's
#     MECHANISM (write → ingest → refresh → rerank-ready) is fully real and
#     verified below; it is not staged to produce a dramatic ranking flip.
#     Point it at a real corpus with genuinely competing documents to see
#     reranking actually change an answer.
#   • `context_receipt_bigquery_sync` (ships receipts to BigQuery) is NOT
#     used here -- it requires a real `dagster_gcp.BigQueryResource` with no
#     local/demo-mode alternative. The receipt store's native SQLite backing
#     (used throughout this demo) needs no warehouse at all. See "Production
#     variants" below.
#
# What it demonstrates
#   • synthetic_data_generator — sample support-ticket corpus
#   • formula — stamps a stable chunk_id (required for receipt item tracking)
#   • embeddings_generator (sentence_transformers — local, no API key)
#   • vector_store_writer / rag_pipeline (chromadb, local)
#   • context_receipt_store — the SQLite-backed receipt/outcome/prior store
#   • receipt_outcome_ingestion — real feedback, ingested as receipt outcomes
#
# Cost: $0 — everything runs locally (sentence-transformers downloads a small
# model on first run; no account, no cloud resource, no LLM call).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • sqlite3 (preinstalled on macOS/most Linux)
#
# Usage
#   ./setup_receipts_learning_loop_demo.sh                 # → receipts_loop_demo/
#   ./setup_receipts_learning_loop_demo.sh my_project       # custom name

set -eo pipefail

PROJECT_NAME="${1:-receipts_loop_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, numpy, sentence-transformers, chromadb, litellm)…"
uv add -q pandas numpy sentence-transformers chromadb litellm
uv add --dev -q dagster-dg-cli dagster-webserver

info "Installing components (--refresh on first call busts the registry-manifest cache)"
uvx --from dagster-community-components-cli dagster-component --refresh add synthetic_data_generator --auto-install
uvx --from dagster-community-components-cli dagster-component add formula --auto-install
uvx --from dagster-community-components-cli dagster-component add embeddings_generator --auto-install
uvx --from dagster-community-components-cli dagster-component add vector_store_writer --auto-install
uvx --from dagster-community-components-cli dagster-component add context_receipt_store --auto-install
uvx --from dagster-community-components-cli dagster-component add rag_pipeline --auto-install
uvx --from dagster-community-components-cli dagster-component add receipt_outcome_ingestion --auto-install

info "Writing defs.yaml (synth → chunk_id → embed → write → [receipts] retrieve)"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets_corpus
  schema_type: support_tickets
  row_count: 10
  random_state: 7
  group_name: source
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: support_tickets_with_chunk_ids
  upstream_asset_key: support_tickets_corpus
  expressions:
    chunk_id: ticket_id
  group_name: source
EOF

cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: ticket_embeddings
  upstream_asset_key: support_tickets_with_chunk_ids
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  input_column: ticket_text
  output_column: embedding
  group_name: embeddings
EOF

cat > "src/$PKG/defs/vector_store_writer/defs.yaml" <<EOF
type: $PKG.components.vector_store_writer.component.VectorStoreWriterComponent
attributes:
  asset_name: support_kb_index
  upstream_asset_key: ticket_embeddings
  provider: chromadb
  collection_name: support-tickets
  connection_string: ./chroma_db
  embedding_column: embedding
  text_column: ticket_text
  metadata_columns: [chunk_id]
  group_name: vector_storage
EOF

cat > "src/$PKG/defs/context_receipt_store/defs.yaml" <<EOF
type: $PKG.components.context_receipt_store.component.ContextReceiptStoreComponent
attributes:
  resource_key: context_receipts
  database_path: ./receipts.sqlite3
EOF

cat > "src/$PKG/defs/rag_pipeline/defs.yaml" <<EOF
type: $PKG.components.rag_pipeline.component.RAGPipelineComponent
attributes:
  asset_name: support_answers
  upstream_asset_key: support_tickets_with_chunk_ids
  query_column: ticket_text
  answer_column: answer
  sources_column: sources
  deps: [support_kb_index]
  retrievers:
    kb:
      kind: chromadb
      path: ./chroma_db
      collection_name: support-tickets
      embedding:
        provider: sentence_transformers
        model: all-MiniLM-L6-v2
  steps:
    - {id: found, op: retrieve, retriever: kb, top_k: 3}
  receipts:
    resource_key: context_receipts
    context_name: support_kb
    rerank: {prior_weight: 0.3, explore_fraction: 0.0}
  group_name: retrieval
EOF

cat > "src/$PKG/defs/receipt_outcome_ingestion/defs.yaml" <<EOF
type: $PKG.components.receipt_outcome_ingestion.component.ReceiptOutcomeIngestionComponent
attributes:
  asset_name: receipt_outcome_ingestion
  store_resource_key: context_receipts
  source:
    kind: file
    path: ./feedback_with_receipts.csv
  mapping:
    receipt_id: receipt_id
    signal: rating
    value_map: {up: thumbs_up, down: thumbs_down}
    outcome_ref: user
    source: feedback_csv
  deps: [support_answers]
  group_name: feedback
EOF

ok "Wrote 7 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Round 1: building the index and answering every ticket (self-retrieval), writing a receipt per answer"
uv run dg launch --assets 'support_tickets_corpus,support_tickets_with_chunk_ids,ticket_embeddings,support_kb_index,support_answers' 2>&1 | tail -10

RECEIPT_COUNT=$(sqlite3 receipts.sqlite3 "SELECT COUNT(*) FROM context_receipts;")
ok "Wrote $RECEIPT_COUNT receipts to receipts.sqlite3"

info "Simulating real user feedback (thumbs up/down) on this round's receipts"
echo "receipt_id,rating" > feedback_with_receipts.csv
sqlite3 -csv receipts.sqlite3 "
SELECT receipt_id,
       CASE WHEN (ROW_NUMBER() OVER (ORDER BY created_at)) % 3 = 0 THEN 'down' ELSE 'up' END AS rating
FROM context_receipts
ORDER BY created_at DESC LIMIT $RECEIPT_COUNT;
" >> feedback_with_receipts.csv
ok "Wrote $(($(wc -l < feedback_with_receipts.csv) - 1)) simulated feedback rows"

info "Ingesting feedback as real receipt outcomes"
uv run dg launch --assets 'receipt_outcome_ingestion' 2>&1 | tail -10

info "Recomputing learned per-chunk priors from receipts + outcomes"
uv run python3 -c "
import sys; sys.path.insert(0, 'src')
from ${PKG}.components.context_receipt_store.component import ContextReceiptStore
store = ContextReceiptStore(database_path='receipts.sqlite3')
n = store.refresh_item_scores(prior_strength=5.0)
print(f'Scored {n} items from real receipts + outcomes:')
import sqlite3 as sq
conn = sq.connect('receipts.sqlite3')
conn.row_factory = sq.Row
for row in conn.execute('SELECT item_id, retrievals, round(prior, 3) as prior FROM context_item_scores ORDER BY item_id'):
    print(f\"  {row['item_id']}: retrievals={row['retrievals']}, learned prior={row['prior']}\")
"

info "Round 2: re-answering with the learned priors now in play (rerank reads context_item_scores)"
uv run dg launch --assets 'support_tickets_corpus,support_tickets_with_chunk_ids,ticket_embeddings,support_kb_index,support_answers' 2>&1 | tail -10

echo
ok "Demo complete."
echo
cat <<EOF
The closed loop just ran, twice, for real:
  Round 1: 10 synthetic support tickets embedded + indexed (ChromaDB, local).
           rag_pipeline answered each ticket (using its own text as the
           query — the same self-retrieval validation trick as the S3
           Vectors demo) and wrote a real receipt per answer: which chunks
           were retrieved, at what rank, with what score.
  Feedback: simulated thumbs up/down against this round's real receipt_ids,
           ingested via receipt_outcome_ingestion as real outcome events.
  Learning: store.refresh_item_scores() recomputed a learned "prior" per
           chunk from the receipts + outcomes — printed above, read
           directly from context_item_scores.
  Round 2: rag_pipeline re-ran with receipts.rerank active, widening its
           candidate pool and reordering by score + prior_weight * prior —
           using the exact priors computed above.

Inspect it yourself:
  cd $PROJECT_NAME
  sqlite3 receipts.sqlite3 "SELECT * FROM context_receipts LIMIT 5;"
  sqlite3 receipts.sqlite3 "SELECT * FROM context_receipt_events WHERE event_type='outcome' LIMIT 5;"
  sqlite3 receipts.sqlite3 "SELECT * FROM context_item_scores;"

Be honest about what this does and doesn't show:
  On this tiny 10-document, self-retrieval corpus, each query's #1 result is
  always its own exact text (cosine ≈ 1.0) — no learned prior at a sane
  prior_weight can displace a perfect match. This demo proves the LOOP
  MECHANISM is real end-to-end (a receipt gets written, real feedback gets
  ingested against it, a real per-chunk prior gets computed from that
  feedback, and retrieval genuinely reads it back on the next round) — it
  is not staged to produce a dramatic ranking flip. Point this at a real
  corpus with genuinely competing documents (ambiguous queries where
  several chunks are plausible answers) to see reranking actually change
  what gets served.

Production variants:
  • Use a real embedding model (provider: openai, needs OPENAI_API_KEY) on
    both embeddings_generator and rag_pipeline's retriever.embedding.
  • Ship receipts to a warehouse with context_receipt_bigquery_sync (needs
    a real dagster_gcp.BigQueryResource — not exercised in this zero-cost
    demo, which uses the store's native SQLite backing throughout).
  • Schedule store.refresh_item_scores() on a cron (e.g. nightly) rather
    than calling it ad hoc, so priors stay current as outcomes arrive.
  • Swap vector_store_writer/rag_pipeline's chromadb for context_engineering_pipeline,
    which runs this same refresh internally as part of its own \`receipts:\`
    block (with chunking, dedup, and quarantine/rewrite-candidate reporting
    on top) — see quickstarts/receipts_learning_loop in dagster-component-templates
    for that fuller version.
EOF
