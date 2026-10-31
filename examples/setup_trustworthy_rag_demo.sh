#!/usr/bin/env bash
# setup_trustworthy_rag_demo.sh
#
# Classic RAG is retrieve-and-hope: the answer looks plausible, but is it
# actually grounded in what was retrieved, and is it any good? This demo adds
# a real trust layer on top of a minimal retrieve->generate chain:
#
#   knowledge_corpus -> kb_embeddings -> kb_index (chromadb)
#   rag_queries -----------------------------------\
#                                                    -> rag_answers
#                                                         |-> answer_grounding   (rag_grounding_check, mode: overlap -- FREE)
#                                                         |-> answer_quality     (llm_judge -- real LLM call)
#
# rag_grounding_check's `mode: overlap` is n-gram-overlap based -- no LLM, no
# API key, catches fabricated numbers/claims the retrieved docs don't
# support. llm_judge is a second, independent LLM-as-judge pass scoring
# accuracy/clarity/completeness (0-10) -- this one genuinely needs a real
# model call, so it's not free. Both run as real Dagster asset checks /
# assets you'd see in the UI, not printed text.
#
# rag_grounding_check's overlap mode was explicitly designed to consume
# rag_pipeline's native `sources` shape (list of {text: ...} dicts) with zero
# transformation -- confirmed by reading both components' source directly.
#
# Embeddings use sentence-transformers (local, free) for both indexing and
# query-time retrieval -- only the final answer-generation step and the
# judge call need a real OpenAI key, keeping cost to a few short completions.
#
# REQUIRED ENV
#   OPENAI_API_KEY    used for rag_pipeline's generate step + llm_judge
#
# COST  ~$0.01 — 3 short gpt-4o-mini completions (generate) + 3 judge calls.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_trustworthy_rag_demo.sh                  # -> trustworthy_rag_demo/
#   ./setup_trustworthy_rag_demo.sh my_project         # custom name

set -eo pipefail

PROJECT_NAME="${1:-trustworthy_rag_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -z "${OPENAI_API_KEY:-}" ] && fail "set OPENAI_API_KEY (used for rag_pipeline's generate step + llm_judge)."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, openai, chromadb, sentence-transformers)…"
uv add -q pandas openai chromadb sentence-transformers
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add embeddings_generator --auto-install
$CLI add vector_store_writer      --auto-install
$CLI add rag_pipeline             --auto-install
$CLI add rag_grounding_check      --auto-install
$CLI add llm_judge                --auto-install

info "Writing inline source data (a small, deliberately narrow KB)"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg

KB_DOCS = [
    "Dagster assets are software-defined objects — a table, file, or model your pipeline produces.",
    "Dagster components are reusable, YAML-configurable units that generate assets, checks, schedules, and sensors.",
    "Asset checks validate data quality or correctness and run alongside asset materialization.",
    "Partitions let an asset represent many logical slices of data — one per day, per region, etc.",
    "Dagster+ is the commercial, hosted offering with alerts, insights, and managed infrastructure.",
]

QUERIES = [
    "What is a Dagster asset?",
    "What are Dagster components used for?",
    "What does an asset check do?",
]


@dg.asset(group_name="source")
def knowledge_corpus() -> pd.DataFrame:
    return pd.DataFrame({"text": KB_DOCS, "doc_id": range(len(KB_DOCS))})


@dg.asset(group_name="source")
def rag_queries() -> pd.DataFrame:
    return pd.DataFrame({"query": QUERIES})


defs = dg.Definitions(assets=[knowledge_corpus, rag_queries])
PYEOF

info "Writing defs.yaml (embed -> index -> retrieve+generate -> grounding check + judge)"

cat > "src/$PKG/defs/embeddings_generator/defs.yaml" <<EOF
type: $PKG.components.embeddings_generator.component.EmbeddingsGeneratorComponent
attributes:
  asset_name: kb_embeddings
  upstream_asset_key: knowledge_corpus
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  input_column: text
  output_column: embedding
  group_name: source
EOF

cat > "src/$PKG/defs/vector_store_writer/defs.yaml" <<EOF
type: $PKG.components.vector_store_writer.component.VectorStoreWriterComponent
attributes:
  asset_name: kb_index
  upstream_asset_key: kb_embeddings
  provider: chromadb
  collection_name: trustworthy_kb
  connection_string: ./chroma_kb
  embedding_column: embedding
  group_name: source
EOF

cat > "src/$PKG/defs/rag_pipeline/defs.yaml" <<EOF
type: $PKG.components.rag_pipeline.component.RAGPipelineComponent
attributes:
  asset_name: rag_answers
  upstream_asset_key: rag_queries
  deps: [kb_index]

  retrievers:
    docs_vs:
      kind: chromadb
      collection_name: trustworthy_kb
      path: ./chroma_kb
      embedding:
        provider: sentence_transformers
        model: all-MiniLM-L6-v2

  steps:
    - {id: retrieved, op: retrieve, retriever: docs_vs, top_k: 3}
    - id: answered
      op: generate
      source: retrieved
      provider: openai
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      temperature: 0.0

  group_name: rag
EOF

cat > "src/$PKG/defs/rag_grounding_check/defs.yaml" <<EOF
type: $PKG.components.rag_grounding_check.component.RagGroundingCheckComponent
attributes:
  asset_name: answer_grounding
  upstream_asset_key: rag_answers
  query_column: query
  answer_column: answer
  sources_column: sources
  mode: overlap
  min_grounding_score_threshold: 0.15
  group_name: trust
EOF

cat > "src/$PKG/defs/llm_judge/defs.yaml" <<EOF
type: $PKG.components.llm_judge.component.LlmJudgeComponent
attributes:
  asset_name: answer_quality
  upstream_asset_key: rag_answers
  response_column: answer
  criteria: [accuracy, clarity, completeness]
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: trust
EOF

ok "Wrote 5 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain (downloads the sentence-transformers model on first run)"
uv run dg launch --assets '*' 2>&1 | tail -30

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Embedded 5 KB docs locally (sentence-transformers, no API key) and
     indexed them into a local ChromaDB collection
  2. For each of 3 queries: retrieved top-3 docs from that index, then asked
     gpt-4o-mini to answer using only those docs as context
  3. answer_grounding: a FREE, no-LLM overlap check — flags fabricated
     numbers/claims the retrieved docs don't actually support
  4. answer_quality: a real LLM-as-judge pass scoring accuracy/clarity/
     completeness (0-10) for every answer

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click rag_answers → see each query/answer/sources row
    → click answer_grounding → Checks tab for the grounding_quality check
      (passes trivially on a first run with no prior materialization to
      compare against — rerun to see real regression detection)
    → click answer_quality → judge_score / judge_reason per answer

Make it fail on purpose:
  Lower min_grounding_score_threshold's bite by asking a question the KB
  can't answer (e.g. "What is Dagster's revenue?") — gpt-4o-mini will likely
  fabricate a number, and answer_grounding's overlap score will drop.

Production variants:
  • Swap mode: overlap for mode: llm_judge on rag_grounding_check itself for
    a second, LLM-based groundedness opinion (costs more, catches paraphrased
    hallucinations overlap scoring misses)
  • Swap sentence_transformers for provider: openai embeddings for
    higher-quality retrieval
  • Add a rerank step to rag_pipeline's steps: (voyage/cohere) before generate
EOF
