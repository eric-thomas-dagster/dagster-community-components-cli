#!/usr/bin/env bash
# context_engineering_partitioned -- the closed context-receipt loop demos (context_engineering.md,
# context_engineering_mcp.md) both build the whole corpus in one run. Real corpora instead arrive a
# slice at a time (today's new tickets), and context_engineering_pipeline supports that: daily
# partitions, one real asset per step (per_step_ops: true), each day's build reading only its own
# slice of the source table -- never reprocessing, never wiping out, a prior day's chunks.
#
#   support_kb_chunks -> support_kb_classified -> support_kb_embedded -> support_kb
#   (ContextEngineeringPipelineComponent, per_step_ops: true, partition_type: daily,
#    source: {kind: warehouse_query} against a local DuckDB table filtered by {partition_key})
#
# Free/local throughout: HuggingFace zero-shot classification + sentence-transformers embeddings +
# a local ChromaDB index + a local DuckDB source table. No API key required.

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
  "sentence-transformers>=2.7.0"

# --- 4. Seed a DuckDB table spanning 3 days --------------------------------
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

# per_step_ops: true -- same 4-node look as the other two demos. partition_type: daily +
# source.sql's {partition_key} placeholder scope each day's run to its own new ticket only;
# write_vector_store's default sync_mode=replace_sources never touches chunks from source rows
# outside THIS run, so each day's build adds to the index instead of replacing it.
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

# --- 8. A semantic query against the day-3 ticket, right after its own build
echo ""
echo ">>> Searching for the day-3 ticket's topic -- indexed the moment its OWN partition built"
uv run python - <<'PY'
import chromadb
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")
qv = model.encode(["how do I download my report"]).tolist()
collection = chromadb.PersistentClient(path="vector_index").get_collection("support_kb")
hits = collection.query(query_embeddings=qv, n_results=1)
print(f"    top hit: {hits['ids'][0][0]} -- {hits['metadatas'][0][0].get('category')}")
PY

# --- 9. Done -----------------------------------------------------------------
cat <<DONE

✓ context_engineering_partitioned demo done.

Three independent builds, one per day, each reading only that day's new ticket --
the index grew from 1 chunk to 3 across three runs, and no run touched a
previous day's chunks.

Browse the graph:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000 -- support_kb_chunks -> support_kb_classified ->
  # support_kb_embedded -> support_kb: one real asset node per step
  # (per_step_ops: true), each partitioned daily starting 2026-01-01. Pick a
  # partition in the UI's partition selector and look at its own
  # materialization history independent of the others.

Backfill the rest of a date range yourself:
  uv run dg launch --assets group:knowledge_base --partition-range "2026-01-04...2026-01-10"
  # (after seeding more rows in support_tickets.duckdb for those days)

Cleanup:
  rm -rf $PROJECT_ABS
DONE
