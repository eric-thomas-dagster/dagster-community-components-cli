#!/usr/bin/env bash
# context_engineering_mcp -- the same closed context-receipt loop as
# context_engineering.md, but with Dagster doing ONLY context engineering:
# build and maintain the knowledge base and the receipt store. Everything
# downstream of that -- serving queries, the agent that answers them,
# recording real feedback -- runs completely outside Dagster, as two plain
# long-running processes speaking real MCP (stdio, a genuine subprocess, the
# same way Claude Desktop/Claude Code launch an MCP server):
#
#   support_kb_articles -> support_kb_chunks -> support_kb_classified ->
#   support_kb_embedded -> support_kb (ContextEngineeringPipelineComponent,
#                              per_step_ops: true -- one real asset per step)
#                              |
#                     (chromadb index + sqlite receipt store)
#                              |
#   context_gateway.py (FastMCP server: search_context / record_feedback / explain)
#                              ^
#                              | real MCP (stdio subprocess)
#   agent.py (standalone agent -- no Dagster import at all)
#
# Dagster never serves a live request in this demo. It builds the index once,
# the gateway + agent serve queries against it for as long as they're running,
# and Dagster only comes back to REBUILD once real feedback has accumulated.
#
# Free/local throughout (HuggingFace zero-shot + sentence-transformers +
# ChromaDB + local SQLite). Optional real generated answer if OPENAI_API_KEY
# is set (agent.py calls litellm directly -- no Dagster component involved).

set -eo pipefail

PROJECT_DIR="${1:-context-engineering-mcp-demo}"
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
# fastmcp for the gateway + agent; litellm only matters if OPENAI_API_KEY is set.
uv add -q "$DCC_SRC" \
  pandas \
  "chromadb>=0.5.0" \
  "sentence-transformers>=2.7.0" \
  "litellm>=1.30.0" \
  fastmcp

# --- 4. Seed KB articles ----------------------------------------------------
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

# --- 5. Write defs.yaml files -- Dagster's ONLY job: build the KB --------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

mkdir -p "$DEFS/support_kb_articles"
cat > "$DEFS/support_kb_articles/defs.yaml" <<YAML
type: dagster_community_components.DataframeFromCsvComponent
attributes:
  asset_name: support_kb_articles
  file_path: ${PROJECT_ABS}/docs/support_kb_articles.csv
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
  per_step_ops: true   # one real asset node per step -- see it as a 4-node chain in the UI, not one box
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

# --- 6. The serving layer -- plain scripts, NOT Dagster components --------
# Neither of these lives in defs/. Dagster doesn't know they exist; they just
# happen to read the same chromadb index + sqlite receipt store Dagster built.
cat > context_gateway.py <<'PYEOF'
"""FastMCP gateway serving the knowledge base Dagster built.

Dagster's job ends at `support_kb`: build and maintain the ChromaDB index and
the receipt store. Everything downstream -- serving queries, the agent that
calls this gateway, recording real feedback -- runs completely outside
Dagster, as a plain long-running process speaking MCP. Any MCP-compatible
client can talk to this, not just agent.py.

Run standalone:  python context_gateway.py          (stdio)
             or: fastmcp run context_gateway.py
Or let agent.py spawn it for you (the default -- see that file).
"""
import hashlib
import os

import chromadb
from fastmcp import FastMCP
from sentence_transformers import SentenceTransformer

from dagster_community_components.resources.context_receipt_store.component import ContextReceiptStore

VECTOR_INDEX_PATH = os.environ.get("VECTOR_INDEX_PATH", "./vector_index")
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "support_kb")
RECEIPTS_DB = os.environ.get("CONTEXT_RECEIPTS_DB", "./context-receipts.sqlite3")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
CONTEXT_NAME = "support_kb"

mcp = FastMCP("support-context-gateway")
_model = SentenceTransformer(EMBEDDING_MODEL)
_chroma = chromadb.PersistentClient(path=VECTOR_INDEX_PATH)
_store = ContextReceiptStore(RECEIPTS_DB)


@mcp.tool
def search_context(query: str, caller_id: str, purpose: str, top_k: int = 2) -> dict:
    """Retrieve chunks for a query against the Dagster-built support_kb index.

    Every call WRITES a receipt (ContextReceiptStore.record_retrieval) -- same
    provenance context_engineering_pipeline stamped onto each chunk
    (source_asset_key, source_version, source_code_version), same mechanism
    rag_pipeline's `receipts:` block uses, just invoked directly here instead
    of from inside a Dagster asset.
    """
    collection = _chroma.get_collection(name=COLLECTION_NAME)
    embedding = _model.encode([query])[0].tolist()
    results = collection.query(query_embeddings=[embedding], n_results=top_k)

    items, chunks = [], []
    for rank in range(len(results["ids"][0])):
        meta = results["metadatas"][0][rank]
        score = 1.0 - float(results["distances"][0][rank])
        items.append({
            "item_id": meta["chunk_id"],
            "source_asset_key": meta.get("source_asset_key"),
            "source_version": meta.get("source_version"),
            "source_code_version": meta.get("source_code_version"),
            "rank": rank,
            "score": score,
        })
        chunks.append({"chunk_id": meta["chunk_id"], "text": results["documents"][0][rank], "score": score})

    receipt_id = _store.record_retrieval(
        context_name=CONTEXT_NAME,
        items=items,
        caller_id=caller_id,
        purpose=purpose,
        query_hash=hashlib.sha256(query.encode()).hexdigest(),
        eligible_count=collection.count(),
    )
    return {"receipt_id": receipt_id, "chunks": chunks}


@mcp.tool
def record_feedback(receipt_id: str, signal: str, user_id: str) -> str:
    """WRITES a real outcome (thumbs_up, thumbs_down, escalated, ...) onto a receipt.

    This is deliberately not a Dagster asset: an outcome is a fact about how
    the agent's answer landed with a real user, reported by whatever served
    the answer -- this gateway, a support UI, anything. Dagster reads it back
    on the next `support_kb` build via that component's `receipts:` block.
    """
    _store.record_outcome(receipt_id, signal=signal, source="agent", outcome_ref=user_id)
    return "ok"


@mcp.tool
def explain(receipt_id: str) -> dict:
    """Read-only: answer 'why did the agent say this?' for a receipt id."""
    return _store.get_receipt(receipt_id) or {"error": "unknown receipt"}


if __name__ == "__main__":
    mcp.run()
PYEOF

cat > agent.py <<'PYEOF'
"""A standalone agent -- not a Dagster asset, not even in the Dagster project's
defs/ -- that answers questions by calling context_gateway.py over real MCP.

Spawns the gateway as its own subprocess over stdio, the same way Claude
Desktop or Claude Code launches an MCP server. Any MCP-compatible agent
could stand in for this script; this one is deliberately minimal so the
protocol boundary is obvious: everything below the `Client(...)` call is
just an MCP tool call, no Dagster import in sight.

Usage:
  python agent.py "Why was I charged twice for my subscription?"
  python agent.py "..." --rate thumbs_down   # simulate a real bad-answer report
"""
import argparse
import asyncio
import os
from pathlib import Path

from fastmcp import Client

GATEWAY = Path(__file__).parent / "context_gateway.py"


async def ask(query: str, rate: str | None) -> None:
    async with Client(GATEWAY) as client:
        result = (await client.call_tool("search_context", {
            "query": query,
            "caller_id": "demo-agent",
            "purpose": "answer_support_question",
            "top_k": 2,
        })).data

        print(f"receipt_id: {result['receipt_id']}")
        for chunk in result["chunks"]:
            print(f"  [{chunk['chunk_id']}] {chunk['text']}")

        if os.environ.get("OPENAI_API_KEY"):
            import litellm

            context = "\n\n".join(chunk["text"] for chunk in result["chunks"])
            completion = litellm.completion(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Answer using only the provided context. If the context "
                            "does not answer the question, say so explicitly."
                        ),
                    },
                    {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
                ],
            )
            print(f"answer: {completion.choices[0].message.content}")

        if rate:
            await client.call_tool("record_feedback", {
                "receipt_id": result["receipt_id"],
                "signal": rate,
                "user_id": "demo-user",
            })
            print(f"recorded: {rate}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--rate", default=None, help="thumbs_up | thumbs_down | ...")
    args = parser.parse_args()
    asyncio.run(ask(args.query, args.rate))
PYEOF

# --- 7. dg check defs -------------------------------------------------------
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

# --- 8. Build 1: before any receipts exist ----------------------------------
echo ""
echo ">>> Build 1 (Dagster): support_kb_articles → support_kb_chunks → support_kb_classified → support_kb_embedded → support_kb (recipe-1, nothing learned yet)"
uv run dg launch --assets support_kb_articles 2>&1 | tail -3
uv run dg launch --assets group:knowledge_base 2>&1 | tail -5   # per_step_ops=true -> one non-subsettable multi-asset, select by group

BEFORE_IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
echo "    indexed chunk ids: $BEFORE_IDS"

# --- 9. Serve the question 6 times through the real MCP agent --------------
# No Dagster involved from here until step 11 -- agent.py spawns
# context_gateway.py as a real subprocess over stdio for every call.
echo ""
echo ">>> Serving the question 6 times via agent.py -> context_gateway.py (real MCP), recording real thumbs-down each time"
QUESTION="Why was I charged twice for my subscription?"
for i in 1 2 3 4 5 6; do
  echo "    ─── round $i ───"
  uv run python agent.py "$QUESTION" --rate thumbs_down 2>&1 | grep -E "receipt_id|^  \[|answer:|recorded:"
done

# --- 10. Build 2 (Dagster): SAME recipe, reads the receipts the gateway wrote
echo ""
echo ">>> Build 2 (Dagster): SAME code_version (recipe-1) -- only real-world feedback changed"
uv run dg launch --assets group:knowledge_base 2>&1 | tail -8

AFTER_IDS="$(uv run python - <<'PY'
import chromadb
print(sorted(chromadb.PersistentClient(path="vector_index").get_collection("support_kb").get()["ids"]))
PY
)"
echo "    indexed chunk ids: $AFTER_IDS"

# --- 11. Ask the same question again via the agent -- different chunks now -
echo ""
echo ">>> Asking the SAME question again via agent.py"
uv run python agent.py "$QUESTION" 2>&1 | grep -E "receipt_id|^  \[|answer:"

# --- 12. Done ---------------------------------------------------------------
cat <<DONE

✓ context_engineering_mcp demo done.

Before: $BEFORE_IDS
After:  $AFTER_IDS
(the chunks real users kept rating thumbs-down are gone from the index --
same recipe, same code_version, nothing re-tuned by hand -- and Dagster never
served a single query to get there)

Browse the Dagster side:
  cd $PROJECT_ABS
  uv run dg dev
  # http://localhost:3000 -- support_kb_articles → support_kb_chunks →
  # support_kb_classified → support_kb_embedded → support_kb: one real asset
  # node per step (per_step_ops: true), each with its own materialization
  # history and metadata. No rag_pipeline, no questions/answers assets:
  # Dagster's whole job here is build + rebuild. Click support_kb's (the
  # final node) Build 2 materialization to see receipts_feedback metadata
  # (quarantined_ids, scoped_to_recipe, ...).

Talk to the gateway yourself:
  cd $PROJECT_ABS
  python agent.py "How do I track my shipment?"
  python agent.py "$QUESTION" --rate thumbs_up   # or escalated, etc.

Run the gateway standalone (any MCP-compatible client can connect):
  python context_gateway.py
  # or: fastmcp run context_gateway.py

Add a real generated answer:
  export OPENAI_API_KEY=sk-...
  python agent.py "$QUESTION"   # agent.py calls litellm directly, no Dagster involved

Cleanup:
  rm -rf $PROJECT_ABS
DONE
