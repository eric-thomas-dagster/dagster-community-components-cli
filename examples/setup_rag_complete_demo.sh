#!/usr/bin/env bash
# rag_complete — end-to-end RAG showing the full stack of RAG components.
#
# Two parallel paths over the same 5 seed docs:
#
#   Path A — state-tracking (the "we take RAG to production" shape):
#     docs_corpus  →  docs_index_snapshot  →  docs_eval[snapshot_id]
#                     (VectorIndexSnapshot)   (RagEval + regression gate)
#
#   Path B — decomposed (the "each step in the RAG pipeline is its own asset"
#     shape — chunkers, embedders, vector stores, retrieval, rerank, LLM):
#     docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index
#                                (EmbeddingsGenerator)  (VectorStoreWriter)
#                                                             │
#     queries.csv → queries → query_embeddings → retrieved → reranked → rag_answer
#                (DataFrameFromCsv) (EmbeddingsGenerator) (VectorStoreQuery) (Reranker) (LLMPromptExecutor)
#
# Path A works no-key (Chroma's bundled ONNX embedder + no LLM step by default).
# Path B works no-key up through `reranked`; the final `rag_answer` step needs
# an LLM API key (OPENAI_API_KEY by default; swap providers in llm_answer.yaml).

set -eo pipefail

PROJECT_DIR="${1:-rag-complete-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Env -----------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Install deps -------------------------------------------------------
# sentence-transformers pulls in torch (heavy but one-time); rerankers use it too.
uv add -q "$DCC_SRC" \
  pandas \
  "chromadb>=0.5.0" \
  "sentence-transformers>=2.7.0" \
  openai

# --- 4. Seed docs (five markdown docs about Dagster concepts) --------------
mkdir -p docs snapshots vector_index

cat > docs/retry_policy.md <<'MD'
# Retry Policy
Configure a retry_policy on any asset by passing a RetryPolicy(max_retries=3,
delay=30, backoff=Backoff.EXPONENTIAL). The op will be re-executed up to
max_retries times with the given delay between attempts. The backoff strategy
controls whether the delay is linear or exponential.
MD

cat > docs/dynamic_partitions.md <<'MD'
# Dynamic Partitions
DynamicPartitionsDefinition lets you add partition keys at runtime. Register
new keys via context.instance.add_dynamic_partitions(partition_key=...). The
UI shows a partition_key selector so you can materialize a specific partition
on demand — great for per-tenant, per-file, or per-event work.
MD

cat > docs/asset_checks.md <<'MD'
# Asset Checks
Attach an @asset_check to any asset. Return AssetCheckResult(passed=True/False,
severity=AssetCheckSeverity.ERROR, metadata={...}). Failing checks with
severity ERROR block downstream materializations. Use asset checks for schema
drift, row-count sanity, cross-domain freshness gates, and quality regressions.
MD

cat > docs/freshness.md <<'MD'
# Freshness Policies
FreshnessPolicy(maximum_lag_minutes=60, cron_schedule=...) alerts you when an
asset gets stale. Combine with sensors so a downstream re-materialization
kicks off before the freshness deadline. The Dagster UI plots freshness over
time; on-call sees "asset X is 30m late" instead of "the pipeline broke."
MD

cat > docs/automation.md <<'MD'
# Automation Conditions
AutomationCondition.eager() materializes an asset as soon as any upstream
completes. AutomationCondition.on_cron("0 9 * * 1-5") ties materialization to
a schedule. Combine conditions with & (AND) and | (OR) to express: "materialize
when upstream is fresh AND it's a weekday morning."
MD

# --- 5. Seed queries CSV (Path B input) -----------------------------------
mkdir -p data
cat > data/queries.csv <<'CSV'
query_id,question
q1,How do I configure retry policy?
q2,What is a dynamic partition?
q3,How do I write an asset check?
CSV

# --- 6. Write defs.yaml files ---------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ─────────────── Shared source: docs_corpus ────────────────
mkdir -p "$DEFS/docs_corpus"
cat > "$DEFS/docs_corpus/defs.yaml" <<YAML
type: dagster_community_components.DocumentCorpusComponent
attributes:
  asset_name: docs_corpus
  source_dir: ${PROJECT_ABS}/docs
  file_glob: "**/*.md"
  min_doc_count: 3
  group_name: source
YAML

# ─────────────── Path A: state-tracking (no key) ───────────
mkdir -p "$DEFS/docs_index_snapshot"
cat > "$DEFS/docs_index_snapshot/defs.yaml" <<YAML
type: dagster_community_components.VectorIndexSnapshotComponent
attributes:
  asset_name: docs_index_snapshot
  upstream_asset_key: docs_corpus
  snapshot_root_dir: ${PROJECT_ABS}/snapshots
  chunk_size: 500
  chunk_overlap: 50
  embedder_provider: chromadb_default
  collection_name: docs
  # Partition this asset by the same dynamic-partitions def it registers keys
  # on for downstream. Each partition_key = one snapshot_id = one immutable
  # ChromaDB dir. `dg launch --assets docs_index_snapshot --partition snap_v3`
  # materializes exactly that snapshot; downstream docs_eval[snap_v3] finds it.
  dynamic_partition_name: rag_snapshot
  partition_this_asset: true
  group_name: path_a_state_tracking
YAML

mkdir -p "$DEFS/docs_eval"
cat > "$DEFS/docs_eval/defs.yaml" <<YAML
type: dagster_community_components.RagEvalComponent
attributes:
  asset_name: docs_eval
  upstream_snapshot_asset_key: docs_index_snapshot
  snapshot_root_dir: ${PROJECT_ABS}/snapshots
  collection_name: docs
  k: 3
  min_score_threshold: 0.5
  regression_pct_threshold: 10.0
  dynamic_partition_name: rag_snapshot
  golden_set:
    - query: "How do I configure retry policy?"
      expected_terms: ["retry_policy", "max_retries", "backoff"]
    - query: "What is a dynamic partition?"
      expected_terms: ["DynamicPartitionsDefinition", "partition_key"]
    - query: "How do I write an asset check?"
      expected_terms: ["AssetCheckResult", "passed", "severity"]
  group_name: path_a_state_tracking
YAML

# ─────────────── Path B: decomposed pipeline ────────────────
# Chunker
mkdir -p "$DEFS/docs_chunks"
cat > "$DEFS/docs_chunks/defs.yaml" <<YAML
type: dagster_community_components.DocumentChunkerComponent
attributes:
  asset_name: docs_chunks
  upstream_asset_key: docs_corpus
  source_column: content
  strategy: fixed
  chunk_size: 500
  chunk_overlap: 50
  group_name: path_b_decomposed
YAML

# Chunk embeddings
mkdir -p "$DEFS/chunk_embeddings"
cat > "$DEFS/chunk_embeddings/defs.yaml" <<YAML
type: dagster_community_components.EmbeddingsGeneratorComponent
attributes:
  asset_name: chunk_embeddings
  upstream_asset_key: docs_chunks
  input_column: chunk
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  batch_size: 32
  group_name: path_b_decomposed
YAML

# Vector store writer — persists to a Chroma path we control
mkdir -p "$DEFS/docs_vector_index"
cat > "$DEFS/docs_vector_index/defs.yaml" <<YAML
type: dagster_community_components.VectorStoreWriterComponent
attributes:
  asset_name: docs_vector_index
  upstream_asset_key: chunk_embeddings
  provider: chromadb
  collection_name: docs_decomposed
  connection_string: ${PROJECT_ABS}/vector_index
  embedding_column: embedding
  text_column: chunk
  batch_size: 128
  upsert: true
  group_name: path_b_decomposed
YAML

# Queries — load from CSV
mkdir -p "$DEFS/queries"
cat > "$DEFS/queries/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: queries
  file_path: ${PROJECT_ABS}/data/queries.csv
  group_name: path_b_decomposed
YAML

# Query chain is partitioned by query_id (static: q1, q2, q3).
# Each downstream asset filters upstream to its partition's row via
# partition_static_column, so materializing --partition q2 processes only
# the row where query_id=q2. Backfill across all 3: dagster asset backfill.
QUERY_PARTITIONS="q1,q2,q3"

# Query embeddings — partitioned per query
mkdir -p "$DEFS/query_embeddings"
cat > "$DEFS/query_embeddings/defs.yaml" <<YAML
type: dagster_community_components.EmbeddingsGeneratorComponent
attributes:
  asset_name: query_embeddings
  upstream_asset_key: queries
  input_column: question
  provider: sentence_transformers
  model: all-MiniLM-L6-v2
  batch_size: 32
  partition_type: static
  partition_values: "${QUERY_PARTITIONS}"
  partition_static_column: query_id
  group_name: path_b_decomposed
YAML

# Retrieval — top-k against docs_vector_index, per query partition
mkdir -p "$DEFS/retrieved"
cat > "$DEFS/retrieved/defs.yaml" <<YAML
type: dagster_community_components.VectorStoreQueryComponent
attributes:
  asset_name: retrieved
  upstream_asset_key: query_embeddings
  provider: chromadb
  collection_name: docs_decomposed
  connection_string: ${PROJECT_ABS}/vector_index
  embedding_column: embedding
  query_text_column: question
  top_k: 3
  include_distances: true
  # Ordering-only dep so the asset graph shows retrieval reading from the index.
  # Chroma path is loaded via connection_string, not via context.load_asset_value.
  deps:
    - docs_vector_index
  partition_type: static
  partition_values: "${QUERY_PARTITIONS}"
  partition_static_column: query_id
  group_name: path_b_decomposed
YAML

# Reranker — cross-encoder local (no key), per query partition
mkdir -p "$DEFS/reranked"
cat > "$DEFS/reranked/defs.yaml" <<YAML
type: dagster_community_components.RerankerComponent
attributes:
  asset_name: reranked
  upstream_asset_key: retrieved
  method: cross_encoder
  model: cross-encoder/ms-marco-MiniLM-L-6-v2
  # vector_store_query emits the query text as 'query' and the retrieved doc as 'document'.
  query_column: query
  text_column: document
  top_n: 3
  partition_type: static
  partition_values: "${QUERY_PARTITIONS}"
  partition_static_column: query_id
  group_name: path_b_decomposed
YAML

# LLM answer — needs OPENAI_API_KEY. Per query partition.
mkdir -p "$DEFS/rag_answer"
cat > "$DEFS/rag_answer/defs.yaml" <<YAML
type: dagster_community_components.LLMPromptExecutorComponent
attributes:
  asset_name: rag_answer
  upstream_asset_key: reranked
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  input_column: query
  output_column: answer
  system_prompt: "Answer using only the retrieved context. If the context does not answer the question, say so explicitly."
  user_prompt_template: |
    Question: {query}
    Context: {document}

    Answer:
  temperature: 0.2
  max_tokens: 300
  partition_type: static
  partition_values: "${QUERY_PARTITIONS}"
  partition_static_column: query_id
  group_name: path_b_decomposed
YAML

# --- 7. dg check defs -----------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 8. Path A — corpus + snapshot v1 + eval v1 ---------------------------
# Both docs_index_snapshot AND docs_eval are partitioned by rag_snapshot.
# We pre-register the partition keys, then materialize each per-partition.
# Snapshot v1 is the baseline; later we inject a regression and materialize v2.
SNAP1="snap_v1"
SNAP2="snap_v2"

echo ">>> Registering rag_snapshot partitions: $SNAP1, $SNAP2"
uv run python - <<PY
import os
os.environ["DAGSTER_HOME"] = "$DAGSTER_HOME"
from dagster import DagsterInstance
inst = DagsterInstance.get()
inst.add_dynamic_partitions("rag_snapshot", ["$SNAP1", "$SNAP2"])
inst.dispose()
print("registered:", ["$SNAP1", "$SNAP2"])
PY

echo ""
echo ">>> Path A round 1: docs_corpus → docs_index_snapshot[$SNAP1] → docs_eval[$SNAP1]"
uv run dg launch --assets docs_corpus 2>&1 | tail -3
uv run dg launch --assets docs_index_snapshot --partition "$SNAP1" 2>&1 | tail -3
uv run dg launch --assets docs_eval --partition "$SNAP1" 2>&1 | tail -5

echo ""
echo ">>> Injecting corpus regression (strips 'max_retries' + 'backoff' from retry_policy.md)"
sed -i.bak \
  -e 's/max_retries=[0-9]*, //g' \
  -e 's/, backoff=Backoff\.EXPONENTIAL//g' \
  -e 's/backoff strategy/policy shape/g' \
  -e 's/exponential/one-shot/g' \
  -e 's/max_retries/attempts/g' \
  -e 's/backoff/timing/g' \
  docs/retry_policy.md
rm -f docs/retry_policy.md.bak

echo ""
echo ">>> Path A round 2: docs_corpus (regressed) → docs_index_snapshot[$SNAP2] → docs_eval[$SNAP2]"
echo "    (asset check on docs_eval[$SNAP2] should FAIL — retrieval regression caught)"
uv run dg launch --assets docs_corpus 2>&1 | tail -3
uv run dg launch --assets docs_index_snapshot --partition "$SNAP2" 2>&1 | tail -3
uv run dg launch --assets docs_eval --partition "$SNAP2" 2>&1 | tail -8 || true

# --- 9. Path B — chunker → embed → index (unpartitioned; one-time index build)
echo ""
echo ">>> Path B unpartitioned prefix: chunker → embeddings → vector index"
uv run dg launch --assets docs_chunks 2>&1 | tail -3
uv run dg launch --assets chunk_embeddings 2>&1 | tail -3
uv run dg launch --assets docs_vector_index 2>&1 | tail -3
uv run dg launch --assets queries 2>&1 | tail -3

# Path B query chain — partitioned by query_id [q1, q2, q3]. Materialize each.
echo ""
echo ">>> Path B partitioned query chain: [q1, q2, q3] per query_embeddings/retrieved/reranked"
for QID in q1 q2 q3; do
  echo "    ─── query partition: $QID ───"
  uv run dg launch --assets query_embeddings --partition "$QID" 2>&1 | tail -2
  uv run dg launch --assets retrieved       --partition "$QID" 2>&1 | tail -2
  uv run dg launch --assets reranked        --partition "$QID" 2>&1 | tail -2
done

# --- 10. Optional: LLM answer step (needs OPENAI_API_KEY) ----------------
if [ -n "$OPENAI_API_KEY" ]; then
  echo ""
  echo ">>> LLM answer step per query partition (OPENAI_API_KEY detected)"
  for QID in q1 q2 q3; do
    echo "    ─── rag_answer partition: $QID ───"
    uv run dg launch --assets rag_answer --partition "$QID" 2>&1 | tail -3
  done
else
  echo ""
  echo "    (skipping rag_answer — OPENAI_API_KEY not set. Everything through 'reranked' materialized;"
  echo "     set OPENAI_API_KEY and re-run 'dg launch --assets rag_answer --partition <q1|q2|q3>')"
fi

# --- 11. Done -------------------------------------------------------------
cat <<DONE

✓ rag_complete demo done.

Two parallel RAG paths built over the same 5-doc corpus:

  Path A — state-tracking (no key required):
    docs_corpus → docs_index_snapshot[$SNAP1,$SNAP2] → docs_eval[$SNAP1,$SNAP2]
    (both partitioned by rag_snapshot — v1 clean, v2 regression-injected)
                                        ↑ asset check gates regressions

  Path B — decomposed pipeline:
    docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index
    queries     → query_embeddings → retrieved → reranked → rag_answer
                                                              ↑
                                                    $( [ -n "$OPENAI_API_KEY" ] && echo "materialized ✓" || echo "gated on OPENAI_API_KEY" )

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # Two asset groups side by side: path_a_state_tracking / path_b_decomposed
  # Both consume the shared 'source' group's docs_corpus.

Swap the LLM:
  Edit ${PROJECT_ABS}/src/${PKG}/defs/rag_answer/defs.yaml
  - Anthropic: provider: anthropic, model: claude-3-5-sonnet, api_key_env_var: ANTHROPIC_API_KEY
  - Gemini:    provider: gemini,    model: gemini-1.5-flash,   api_key_env_var: GOOGLE_API_KEY
  - Local:     any OpenAI-compatible endpoint (llama.cpp server, Ollama with /v1/) via api_base_env_var.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
