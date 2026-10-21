#!/usr/bin/env bash
# context_engineering_partitioned -- the same closed context-receipt loop as
# context_engineering.md (build -> serve -> real feedback -> rebuild with the
# identical recipe -> the chunks that kept failing are gone), but the build side
# is daily-partitioned instead of built all at once: new tickets arrive a day at
# a time, each partition's run reads only its own new row, and a rebuild only
# ever re-processes the ONE partition whose chunk picked up bad feedback.
#
#   support_kb_chunks -> support_kb_classified -> support_kb_embedded -> support_kb
#   (ContextEngineeringPipelineComponent, per_step_ops: true, partition_type: daily,
#    source: {kind: warehouse_query} against a local DuckDB table filtered by
#    {partition_key}, receipts: {quarantine: {mode: exclude}})
#   support_questions -> support_answers (RAGPipelineComponent: retrieve,
#    receipts: {resource_key: context_receipts})
#
# Free/local throughout: HuggingFace zero-shot classification + sentence-transformers
# embeddings + a local ChromaDB index + a local DuckDB source table + a local SQLite
# receipt store. No API key required for the core loop.

set -eo pipefail

PROJECT_DIR="${1:-context-engineering-partitioned-demo}"
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
  duckdb \
  dagster-duckdb \
  "chromadb>=0.5.0" \
  "sentence-transformers>=2.7.0" \
  "litellm>=1.30.0"

# --- 4. Seed a DuckDB table spanning 3 days + a question --------------------
# One new ticket arrives per day. Nothing in this table is special per-partition
# except the `day` column context_engineering_pipeline's {partition_key} filters on.
uv run python - <<'PY'
import duckdb
con = duckdb.connect("support_tickets.duckdb")
con.execute("CREATE TABLE tickets(ticket_id VARCHAR, body VARCHAR, day VARCHAR)")
con.execute("""
    INSERT INTO tickets VALUES
    ('billing_refund', 'I was charged twice for my subscription this month, please refund the duplicate charge.', '2026-01-01'),
    ('shipping_delay', 'My package says delivered but tracking still shows it in transit, when will it actually arrive?', '2026-01-02'),
    ('export_broken', 'The export button does not work when I try to download my usage report as a CSV file.', '2026-01-03')
""")
con.close()
PY

mkdir -p docs
cat > docs/support_questions.csv <<'CSV'
query_id,query
q1,Why was I charged twice for my subscription?
CSV

# --- 5. Write defs.yaml files -----------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

mkdir -p "$DEFS/duckdb"
cat > "$DEFS/duckdb/defs.yaml" <<YAML
type: dagster_community_components.DuckDBResourceComponent
attributes:
  resource_key: duckdb
  database: ${PROJECT_ABS}/support_tickets.duckdb
YAML

mkdir -p "$DEFS/support_questions"
cat > "$DEFS/support_questions/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: support_questions
  file_path: ${PROJECT_ABS}/docs/support_questions.csv
  group_name: source
YAML

mkdir -p "$DEFS/context_receipts"
cat > "$DEFS/context_receipts/defs.yaml" <<YAML
type: dagster_community_components.ContextReceiptStoreComponent
attributes:
  resource_key: context_receipts
  database_path: ${PROJECT_ABS}/context-receipts.sqlite3
  backend: sqlite
YAML

# per_step_ops: true -- same 4-node look as the other two demos. partition_type: daily +
# source.sql's {partition_key} placeholder scope each day's run to its own new ticket only;
# write_vector_store's default sync_mode=replace_sources never touches chunks from source rows
# outside THIS run, so each day's build adds to the index instead of replacing it. code_version
# stays IDENTICAL across every build below -- the point is that nothing about the recipe
# changes, only accumulated real-world feedback (and which single partition gets rebuilt).
mkdir -p "$DEFS/support_kb"
cat > "$DEFS/support_kb/defs.yaml" <<YAML
type: dagster_community_components.ContextEngineeringPipelineComponent
attributes:
  asset_name: support_kb
  source:
    kind: warehouse_query
    resource_key: duckdb
    sql: "SELECT ticket_id, body FROM tickets WHERE day = {partition_key}"
  id_column: ticket_id
  text_column: body
  code_version: "recipe-1"
  per_step_ops: true
  partition_type: daily
  partition_start: "2026-01-01"
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

# ─────────────── Query side: rag_pipeline (same shape as context_engineering.md) ──
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

# --- 7. Build one partition per day, in order -------------------------------
echo ""
echo ">>> Building one partition per day -- each run reads only its own day's new ticket"
for day in 2026-01-01 2026-01-02 2026-01-03; do
  echo "    ─── partition $day ───"
  uv run dg launch --assets group:knowledge_base --partition "$day" 2>&1 | tail -5
  IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
  echo "    indexed chunk ids after $day: $IDS"
done

# --- 8. Serve the question 6 times, recording real negative feedback --------
# The question matches the 2026-01-01 partition's ticket (billing_refund). rag_pipeline's
# receipts: block records what it served, without any code here. Recording OUTCOMES (did the
# answer help?) is deliberately an external-system job per the component's own design -- a
# support UI's thumbs-down button, not a Dagster asset -- so this reads the new receipt id back
# from the store and calls ContextReceiptStore.record_outcome directly, the same way a real
# app would via its own backend.
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

# --- 9. Rebuild ONLY the partition whose chunk got the bad feedback ---------
# SAME code_version: only accumulated real-world feedback changed. Receipt feedback is looked
# up by chunk id against THIS run's own batch, so rebuilding 2026-01-01 alone is enough -- the
# 2026-01-02 / 2026-01-03 partitions were never served, have no feedback, and are left untouched.
echo ""
echo ">>> Rebuilding ONLY partition 2026-01-01 -- SAME code_version, only feedback changed"
uv run dg launch --assets group:knowledge_base --partition 2026-01-01 2>&1 | tail -8

AFTER_IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
echo "    indexed chunk ids: $AFTER_IDS"

# --- 10. Ask the same question again -- served from a different chunk now --
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

✓ context_engineering_partitioned demo done.

After day 3: billing_refund_0, export_broken_0, shipping_delay_0
After rebuilding day 1 only: $AFTER_IDS
(billing_refund_0 -- the chunk real users kept rating thumbs-down -- is gone;
export_broken_0 and shipping_delay_0, from partitions that were never served,
are untouched; same recipe, same code_version, only one day's partition
rebuilt)

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000
  # knowledge_base group: support_kb_chunks -> support_kb_classified ->
  # support_kb_embedded -> support_kb -- one real asset node per step
  # (per_step_ops: true), each partitioned daily starting 2026-01-01. Pick a
  # partition in the UI's partition selector and look at its own
  # materialization history independent of the others.
  # serving group: support_questions -> support_answers

Re-run the loop yourself:
  uv run dg launch --assets support_answers   # ask again
  uv run dg launch --assets group:knowledge_base --partition 2026-01-01   # rebuild just that day

Backfill further days yourself (after seeding more rows in support_tickets.duckdb):
  uv run dg launch --assets group:knowledge_base --partition-range "2026-01-04...2026-01-10"

Cleanup:
  rm -rf $PROJECT_ABS
DONE
