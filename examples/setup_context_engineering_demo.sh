#!/usr/bin/env bash
# context_engineering — the closed context-receipt loop, build side + query side,
# with real components (no mocks): ContextEngineeringPipelineComponent builds a
# support knowledge base; RAGPipelineComponent serves queries against it and
# records a receipt per query; real negative feedback is recorded against those
# receipts; a rebuild of the SAME recipe reads that feedback and excludes the
# chunks people kept rating badly — the next query is served from different chunks.
#
#   support_kb_articles → support_kb (ContextEngineeringPipelineComponent:
#                          chunk → classify → embed → write_vector_store,
#                          receipts: {quarantine: {mode: exclude}})
#   support_questions → support_answers (RAGPipelineComponent: retrieve,
#                          receipts: {resource_key: context_receipts})
#
# Both free/local: HuggingFace zero-shot classification + sentence-transformers
# embeddings + a local ChromaDB index + a local SQLite receipt store. No API key
# required for the core loop. Add OPENAI_API_KEY to also materialize a real
# generated answer per query (optional `generate` step, gated below).

set -eo pipefail

PROJECT_DIR="${1:-context-engineering-demo}"
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
# sentence-transformers pulls in torch (heavy but one-time, cached after).
uv add -q "$DCC_SRC" \
  pandas \
  "chromadb>=0.5.0" \
  "sentence-transformers>=2.7.0" \
  "litellm>=1.30.0"

# --- 4. Seed KB articles + a question --------------------------------------
mkdir -p docs
cat > docs/support_kb_articles.csv <<'CSV'
article_id,body
billing_duplicate,"Duplicate charges on your subscription are refunded automatically within five business days. If you do not see the refund after that window, contact billing support with your invoice number."
billing_invoice,"Invoices are emailed on the first day of each month and can be downloaded any time from the billing page under Account Settings."
billing_workspace,"Seeing two charges for the same plan usually means a second workspace was created by accident during signup. Close the duplicate workspace from the workspace switcher and request a refund for the extra charge."
export_report,"To export a report, open it, choose Export, then pick CSV or PDF. You will receive an email with a download link once the export finishes."
shipping_standard,"Standard shipping takes five to seven business days. Express shipping takes two business days and is available at checkout for an additional fee."
shipping_tracking,"Tracking numbers are emailed automatically as soon as the parcel leaves the warehouse. You can also find the tracking number on the order details page."
CSV

cat > docs/support_questions.csv <<'CSV'
query_id,query
q1,Why was I charged twice for my subscription?
CSV

# --- 5. Write defs.yaml files ---------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ─────────────── Sources ────────────────
mkdir -p "$DEFS/support_kb_articles"
cat > "$DEFS/support_kb_articles/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: support_kb_articles
  file_path: ${PROJECT_ABS}/docs/support_kb_articles.csv
  group_name: source
YAML

mkdir -p "$DEFS/support_questions"
cat > "$DEFS/support_questions/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: support_questions
  file_path: ${PROJECT_ABS}/docs/support_questions.csv
  group_name: source
YAML

# ─────────────── Shared resource: the receipt store (local sqlite) ──────
mkdir -p "$DEFS/context_receipts"
cat > "$DEFS/context_receipts/defs.yaml" <<YAML
type: dagster_community_components.ContextReceiptStoreComponent
attributes:
  resource_key: context_receipts
  database_path: ${PROJECT_ABS}/context-receipts.sqlite3
  backend: sqlite
YAML

# ─────────────── Build side: context_engineering_pipeline ──────────────
# code_version stays IDENTICAL across both builds below -- the point is that
# nothing about the recipe changes, only accumulated real-world feedback.
mkdir -p "$DEFS/support_kb"
cat > "$DEFS/support_kb/defs.yaml" <<YAML
type: dagster_community_components.ContextEngineeringPipelineComponent
attributes:
  asset_name: support_kb
  upstream_asset_key: support_kb_articles
  id_column: article_id
  text_column: body
  code_version: "recipe-1"
  receipts:
    resource_key: context_receipts
    quarantine: {mode: exclude}
  steps:
    - id: chunks
      op: chunk
      strategy: fixed
      chunk_size: 500
      chunk_overlap: 0
      min_chunk_chars: 10
    - id: classified
      op: classify
      candidate_labels: [billing, shipping, other]
    - id: embedded
      op: embed
      provider: sentence_transformers
      model: all-MiniLM-L6-v2
    - id: indexed
      op: write_vector_store
      provider: chromadb
      connection_string: ${PROJECT_ABS}/vector_index
      collection_name: support_kb
  group_name: knowledge_base
YAML

# ─────────────── Query side: rag_pipeline ───────────────────────────────
# Same embedding model as the build side (required -- query and chunk vectors
# must live in the same space). The 'generate' step is gated on OPENAI_API_KEY
# below; receipts are recorded by 'retrieve' regardless of whether it runs.
mkdir -p "$DEFS/support_answers"
cat > "$DEFS/support_answers/defs.yaml" <<YAML
type: dagster_community_components.RAGPipelineComponent
attributes:
  asset_name: support_answers
  upstream_asset_key: support_questions
  query_column: query
  receipts:
    resource_key: context_receipts
    context_name: support_kb
  retrievers:
    kb:
      kind: chromadb
      path: ${PROJECT_ABS}/vector_index
      collection_name: support_kb
      embedding:
        provider: sentence_transformers
        model: all-MiniLM-L6-v2
  steps:
    - id: found
      op: retrieve
      retriever: kb
      top_k: 2
YAML
if [ -n "$OPENAI_API_KEY" ]; then
cat >> "$DEFS/support_answers/defs.yaml" <<YAML
    - id: answered
      op: generate
      source: found
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "Answer using only the provided context. If the context does not answer the question, say so explicitly."
YAML
fi
cat >> "$DEFS/support_answers/defs.yaml" <<YAML
  group_name: serving
YAML

# --- 6. dg check defs -------------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 7. Build 1: before any receipts exist ----------------------------------
echo ""
echo ">>> Build 1: support_kb_articles → support_kb (code_version=recipe-1, nothing learned yet)"
uv run dg launch --assets support_kb_articles 2>&1 | tail -3
uv run dg launch --assets support_kb 2>&1 | tail -5

BEFORE_IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
echo "    indexed chunk ids: $BEFORE_IDS"

# --- 8. Serve the question 6 times, recording real negative feedback --------
# rag_pipeline's receipts: block records what it served, without any code here.
# Recording OUTCOMES (did the answer help?) is deliberately an external-system
# job per the component's own design -- a support UI's thumbs-down button, not
# a Dagster asset -- so this reads the new receipt id back from the store and
# calls ContextReceiptStore.record_outcome directly, the same way a real app
# would via its own backend.
echo ""
echo ">>> Serving the question 6 times, recording real thumbs-down each time"
uv run dg launch --assets support_questions 2>&1 | tail -3
: > rated_receipts.txt
for i in 1 2 3 4 5 6; do
  echo "    ─── round $i ───"
  uv run dg launch --assets support_answers 2>&1 | tail -3
  uv run python - <<'PY'
from dagster_community_components.resources.context_receipt_store.component import ContextReceiptStore

store = ContextReceiptStore("context-receipts.sqlite3")
seen = set(open("rated_receipts.txt").read().split())
new = set(store.find_by_source_asset("support_kb")) - seen
assert len(new) == 1, f"expected exactly 1 new receipt this round, got {new}"
rid = new.pop()
served = [i["item_id"] for i in store.get_receipt(rid)["items"]]
store.record_outcome(rid, signal="thumbs_down")
with open("rated_receipts.txt", "a") as f:
    f.write(rid + "\n")
print(f"    receipt {rid} served {served} -> rated thumbs_down")
PY
done

# --- 9. Build 2: SAME recipe, but now the build reads those receipts --------
echo ""
echo ">>> Build 2: SAME code_version (recipe-1) -- only real-world feedback changed"
uv run dg launch --assets support_kb 2>&1 | tail -8

AFTER_IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
echo "    indexed chunk ids: $AFTER_IDS"

# --- 10. Ask the same question again -- served from different chunks -------
echo ""
echo ">>> Asking the SAME question again"
uv run dg launch --assets support_answers 2>&1 | tail -3
uv run python - <<'PY'
import pandas as pd
df = pd.read_pickle(".dagster_home/storage/support_answers")
served_now = [d["metadata"]["chunk_id"] for d in df["sources"].iloc[0]]
print("    now served:", served_now)
PY

# --- 11. Done ---------------------------------------------------------------
cat <<DONE

✓ context_engineering demo done.

Before: $BEFORE_IDS
After:  $AFTER_IDS
(the chunks real users kept rating thumbs-down are gone from the index --
same recipe, same code_version, nothing re-tuned by hand)

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # knowledge_base group: support_kb_articles → support_kb
  # serving group: support_questions → support_answers
  # Click support_kb's materializations to see receipts_feedback metadata
  # (quarantined_ids, scoped_to_recipe, ...) on the Build 2 run.

Re-run the loop yourself:
  uv run dg launch --assets support_answers   # ask again
  uv run dg launch --assets support_kb        # rebuild, reading any new feedback

Add a real generated answer:
  export OPENAI_API_KEY=sk-...
  # re-run this script -- the 'generate' step is gated on the key being set

Cleanup:
  rm -rf $PROJECT_ABS
DONE
