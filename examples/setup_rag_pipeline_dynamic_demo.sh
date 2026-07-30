#!/usr/bin/env bash
# rag_pipeline_dynamic — the "one-component RAG" shape, driven by dynamic
# per-partition queries (not a hard-coded string or env var).
#
# What's built:
#   docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index
#                                                       │
#   queries.csv → queries (DataframeFromCsv)            │
#                    │                                  │
#                    ▼                                  │
#                 rag_answer[query_id]  ◀───────────────┘  (deps: docs_vector_index)
#                 RAGPipelineComponent
#                 partition_type: static, partition_values: q1,q2,q3
#                 partition_static_column: query_id
#
# Materialize rag_answer per query:
#   dg launch --assets rag_answer --partition q2
# Each partition runs embed → retrieve → generate on ONE query, tracked in
# Dagster's asset history with its own run id, its own inputs, its own
# retry semantics. Add more queries (append to queries.csv, extend the
# partition set) without changing the component.
#
# Everything except the LLM answer runs no-key: sentence_transformers for
# embeddings + ChromaDB local. The LLM step needs OPENAI_API_KEY (or swap
# provider in the yaml — anthropic/gemini/openai-compatible).

set -eo pipefail

PROJECT_DIR="${1:-rag-pipeline-dynamic-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Env ----------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# --- 3. Install deps -------------------------------------------------------
uv add -q "$DCC_SRC" \
  pandas \
  "chromadb>=0.5.0" \
  "sentence-transformers>=2.7.0" \
  openai

# --- 4. Seed docs (5 markdown docs about Dagster concepts) ----------------
mkdir -p docs data vector_index

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

# --- 5. Seed queries — one row per query_id (the dynamic partition axis) --
cat > data/queries.csv <<'CSV'
query_id,question
q1,How do I configure retry policy?
q2,What is a dynamic partition?
q3,How do I write an asset check?
CSV

# --- 6. defs.yaml files ----------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# Doc corpus
mkdir -p "$DEFS/docs_corpus"
cat > "$DEFS/docs_corpus/defs.yaml" <<YAML
type: dagster_community_components.DocumentCorpusComponent
attributes:
  asset_name: docs_corpus
  source_dir: ${PROJECT_ABS}/docs
  file_glob: "**/*.md"
  min_doc_count: 3
  group_name: index
YAML

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
  group_name: index
YAML

# Embeddings — sentence_transformers/all-MiniLM-L6-v2 (must match rag_answer)
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
  group_name: index
YAML

# Vector store — Chroma persistent, path we control, collection name we reuse.
mkdir -p "$DEFS/docs_vector_index"
cat > "$DEFS/docs_vector_index/defs.yaml" <<YAML
type: dagster_community_components.VectorStoreWriterComponent
attributes:
  asset_name: docs_vector_index
  upstream_asset_key: chunk_embeddings
  provider: chromadb
  collection_name: docs_kb
  connection_string: ${PROJECT_ABS}/vector_index
  embedding_column: embedding
  text_column: chunk
  batch_size: 128
  upsert: true
  group_name: index
YAML

# Queries source — one row per query_id
mkdir -p "$DEFS/queries"
cat > "$DEFS/queries/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: queries
  file_path: ${PROJECT_ABS}/data/queries.csv
  group_name: query
YAML

# RAG answer — the "one-component RAG" shape. Partitioned by query_id so
# each partition materializes a single row's embed → retrieve → generate.
QUERY_PARTITIONS="q1,q2,q3"
mkdir -p "$DEFS/rag_answer"
cat > "$DEFS/rag_answer/defs.yaml" <<YAML
type: dagster_community_components.RAGPipelineComponent
attributes:
  asset_name: rag_answer
  upstream_asset_key: queries
  # Vector store — SAME collection + path as docs_vector_index above.
  vector_store_provider: chromadb
  collection_name: docs_kb
  vector_store_connection: ${PROJECT_ABS}/vector_index
  # Query-side embedder MUST match the index-side embedder (both MiniLM).
  embedding_provider: sentence_transformers
  embedding_model: all-MiniLM-L6-v2
  # LLM — swap provider/model in this file; api_key follows env-var syntax.
  llm_provider: openai
  llm_model: gpt-4o-mini
  llm_api_key: \${OPENAI_API_KEY}
  # DataFrame conventions — pull query text from 'question', write to 'answer'.
  query_column: question
  answer_column: answer
  sources_column: sources
  top_k: 3
  temperature: 0.2
  # Per-partition partitioning: static values, filter upstream on query_id.
  partition_type: static
  partition_values: "${QUERY_PARTITIONS}"
  partition_static_column: query_id
  # Lineage-only edge — index is loaded via connection_string at runtime,
  # not via context.load_asset_value, so this is a deps-style dep.
  deps:
    - docs_vector_index
  group_name: query
YAML

# --- 7. dg check defs ------------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 8. Build the index (unpartitioned; runs once) ------------------------
echo ""
echo ">>> Building vector index: docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index"
uv run dg launch --assets docs_corpus         2>&1 | tail -3
uv run dg launch --assets docs_chunks         2>&1 | tail -3
uv run dg launch --assets chunk_embeddings    2>&1 | tail -3
uv run dg launch --assets docs_vector_index   2>&1 | tail -3
uv run dg launch --assets queries             2>&1 | tail -3

# --- 9. Run per-query RAG (needs OPENAI_API_KEY) --------------------------
if [ -n "$OPENAI_API_KEY" ]; then
  echo ""
  echo ">>> rag_answer per query_id partition — one materialization per query"
  for QID in q1 q2 q3; do
    echo "    ─── rag_answer partition: $QID ───"
    uv run dg launch --assets rag_answer --partition "$QID" 2>&1 | tail -3
  done
else
  echo ""
  echo "    (skipping rag_answer — OPENAI_API_KEY not set. Index is built;"
  echo "     set OPENAI_API_KEY and re-run:"
  echo "        cd $PROJECT_ABS"
  echo "        uv run dg launch --assets rag_answer --partition q1"
  echo "     — or open the UI: uv run dg dev)"
fi

# --- 10. Done --------------------------------------------------------------
cat <<DONE

✓ rag_pipeline_dynamic demo done.

The "one-component RAG" shape driven by dynamic per-partition queries:

  docs_corpus → docs_chunks → chunk_embeddings → docs_vector_index
                                                         │
  queries (from CSV) ──────────────────────────────      │
                                                  │      │
                                                  ▼      │
                                          rag_answer[query_id]  ◀── deps: docs_vector_index
                                          (RAGPipelineComponent, partitioned)

Per-partition semantics:
  - rag_answer partition_type=static, partition_values=q1,q2,q3
  - partition_static_column=query_id filters upstream 'queries' to one row
  - each partition = one embed → retrieve → generate call, one Dagster run
  - re-run a single question: dg launch --assets rag_answer --partition q2
  - add a new question: append to data/queries.csv + add key to partition_values

Add or remove queries without rewriting the pipeline:
  echo "q4,How do freshness policies work?" >> data/queries.csv
  # In src/$PKG/defs/rag_answer/defs.yaml, set:
  #   partition_values: "q1,q2,q3,q4"
  uv run dg launch --assets queries
  uv run dg launch --assets rag_answer --partition q4

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # asset groups: 'index' (unpartitioned index build) + 'query' (partitioned RAG)
  # rag_answer → Partitions tab: q1 / q2 / q3 each with its own materialization

Swap the LLM (edit ${PROJECT_ABS}/src/${PKG}/defs/rag_answer/defs.yaml):
  # Anthropic
  llm_provider: anthropic
  llm_model: claude-3-5-sonnet-latest
  llm_api_key: \${ANTHROPIC_API_KEY}

  # OpenAI-compatible local (Ollama / llama.cpp with /v1/): set OPENAI_BASE_URL
  # to the local endpoint before dg dev, keep llm_provider: openai.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
