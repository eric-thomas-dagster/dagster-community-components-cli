#!/usr/bin/env bash
# setup_supabase_rag_demo.sh
#
# **The RAG demo everyone is building right now**, done properly with Dagster:
# an ingestion asset embeds documents via OpenAI + writes them into
# Supabase pgvector, then a query asset does a similarity search + LLM answer.
# Runs entirely on LOCAL Supabase (via the official supabase CLI Docker stack) —
# no cloud account needed.
#
# Shape:
#   docs.csv  ──▶  docs_embedded  ──(write to pgvector)──▶  Supabase (pgvector RPC)
#                                                                      │
#                                                                      ▼
#   user_question ─▶ question_embedded ─▶ retrieved_context ─▶ rag_answer (LLM)
#
# What it demonstrates
#   • Live Supabase via `supabase start` (7 containers, official CLI stack)
#   • pgvector with the recommended RPC pattern for cosine similarity
#   • SupabaseResourceComponent + SupabaseVectorSearchAssetComponent (both
#     live-validated against real pgvector)
#   • End-to-end RAG: embed → store → retrieve → generate
#
# Cost: ~$0.01 per run (embedding + one LLM call on gpt-4o-mini).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • docker (Docker Desktop or engine)
#   • supabase CLI: `brew install supabase/tap/supabase`
#   • OPENAI_API_KEY
#
# Usage
#   export OPENAI_API_KEY=sk-...
#   ./setup_supabase_rag_demo.sh                     # → supabase_rag_demo/
#   ./setup_supabase_rag_demo.sh my_rag              # custom name

set -eo pipefail

PROJECT_NAME="${1:-supabase_rag_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
SB_DIR="${BASE_DIR}/${PROJECT_NAME}_supabase"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup; exit 1; }

cleanup() {
  if [ -d "$SB_DIR" ]; then
    (cd "$SB_DIR" && supabase stop >/dev/null 2>&1) || true
  fi
}
trap cleanup INT TERM

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
command -v docker >/dev/null 2>&1 || fail "docker not found."
command -v supabase >/dev/null 2>&1 || fail "supabase CLI not found. Install: brew install supabase/tap/supabase"
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"
[ -d "$SB_DIR" ] && fail "Directory exists: $SB_DIR"

info "Target Dagster project: $PROJECT_DIR"
info "Supabase local dir:     $SB_DIR"

# ── Start local Supabase stack via the supabase CLI ─────────────────────────
info "Starting local Supabase (this pulls ~9 images the first time)…"
mkdir -p "$SB_DIR"
(cd "$SB_DIR" && supabase init 2>&1 | tail -3 && supabase start 2>&1 | tail -3) || fail "supabase start failed"
export SUPABASE_URL="http://127.0.0.1:54321"
export SUPABASE_KEY="$(cd "$SB_DIR" && supabase status --output json | jq -r .SERVICE_ROLE_KEY)"
[ -z "$SUPABASE_KEY" ] || [ "$SUPABASE_KEY" = "null" ] && fail "Couldn't fetch service_role key from supabase status"
ok "Supabase up: Studio at http://127.0.0.1:54323"

# ── Create the schema (docs table + RPC for cosine similarity) ─────────────
info "Creating docs table + match_docs RPC function via psycopg2…"
/opt/homebrew/bin/python3.11 -m pip install --quiet psycopg2-binary 2>&1 | tail -1
/opt/homebrew/bin/python3.11 << 'PY'
import psycopg2
conn = psycopg2.connect(host='127.0.0.1', port=54322, user='postgres', password='postgres', dbname='postgres')
conn.autocommit = True
cur = conn.cursor()
cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
cur.execute("""
CREATE TABLE IF NOT EXISTS docs (
  id bigserial primary key,
  title text,
  content text,
  embedding vector(1536)
);
""")
cur.execute("""
CREATE OR REPLACE FUNCTION match_docs(
  query_embedding vector(1536),
  match_count int DEFAULT 5
) RETURNS TABLE (id bigint, title text, content text, similarity float)
LANGUAGE sql STABLE AS $$
  SELECT id, title, content, 1 - (embedding <=> query_embedding) as similarity
  FROM docs
  ORDER BY embedding <=> query_embedding
  LIMIT match_count;
$$;
""")
cur.execute("GRANT ALL PRIVILEGES ON TABLE docs TO service_role, authenticated, anon")
cur.execute("GRANT ALL PRIVILEGES ON SEQUENCE docs_id_seq TO service_role, authenticated, anon")
cur.execute("GRANT EXECUTE ON FUNCTION match_docs(vector, int) TO service_role, authenticated, anon")
print("schema + RPC ready")
PY

# ── Seed docs — a small knowledge base about Dagster's ecosystem ───────────
info "Seeding 8 knowledge-base docs + generating embeddings via OpenAI…"
/opt/homebrew/bin/python3.11 << PY
import os, psycopg2
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
docs = [
    ("Dagster components", "Dagster components are declarative YAML-driven abstractions. Each component ships as a Python class inheriting dg.Component, dg.Model, dg.Resolvable, with a build_defs method returning Definitions."),
    ("Temporal integration", "Dagster observes Temporal workflows via temporal_workflow_sensor, triggers them via temporal_workflow_trigger, and pushes/pulls state via temporal_signal_asset and temporal_query_asset."),
    ("Cube semantic layer", "Cube gives LLMs a governed interface to metrics — measures and dimensions — so the LLM never writes raw SQL. Dagster's cube_query_asset materializes Cube queries as DataFrame assets."),
    ("Vercel AI Gateway", "Vercel AI Gateway is an OpenAI-compatible proxy. One credential routes to any provider — openai/gpt-4o, anthropic/claude-sonnet-4-6, google/gemini-2.5-flash, xai/grok-4. Fallback chains supported."),
    ("dbt + ML mid-DAG", "The 'Python between two dbt layers' pattern that Airflow can't do. Dagster treats every dbt model as an asset, so a Python ML asset drops in between and cross-language lineage flows correctly."),
    ("MCP servers", "Model Context Protocol servers expose tools to LLM agents. Dagster's openai_agent and anthropic_agent both take an mcp_servers: config with stdio / http / sse transports."),
    ("Supabase pgvector", "Supabase Postgres includes pgvector for vector similarity search. The recommended pattern is an RPC function using the <=> operator for cosine distance."),
    ("Vercel deployment sensor", "Downstream Dagster data work fires the moment production hits READY. AssetObservation metadata captures the commit sha, branch, and deployment URL."),
]

# Compute embeddings — batched.
resp = client.embeddings.create(model="text-embedding-3-small", input=[d[1] for d in docs], dimensions=1536)
embeddings = [e.embedding for e in resp.data]
print(f"embedded {len(embeddings)} docs with text-embedding-3-small (1536d)")

conn = psycopg2.connect(host='127.0.0.1', port=54322, user='postgres', password='postgres', dbname='postgres')
conn.autocommit = True
cur = conn.cursor()
cur.execute("DELETE FROM docs")
for (title, content), emb in zip(docs, embeddings):
    emb_str = "[" + ",".join(str(x) for x in emb) + "]"
    cur.execute("INSERT INTO docs (title, content, embedding) VALUES (%s, %s, %s::vector)", (title, content, emb_str))
cur.execute("SELECT COUNT(*) FROM docs")
print(f"docs in Supabase: {cur.fetchone()[0]}")
PY
ok "Docs + embeddings loaded"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    'supabase>=2.0.0' 'openai>=1.0.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'supabase>=2.0.0' 'openai>=1.0.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

# ── Persistent IO manager (chained DataFrame assets need this) ─────────────
# The default fs IO manager uses ephemeral per-run tmp dirs — subprocesses
# in the multiprocess executor can't consistently read each other's output.
# Wire a filesystem IO manager rooted in the project so all steps share state.
mkdir -p "$PROJECT_DIR/.dagster_storage"
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
from pathlib import Path
from dagster import definitions, load_from_defs_folder, FilesystemIOManager

@definitions
def defs():
    root = Path(__file__).resolve().parent.parent.parent
    storage = root / ".dagster_storage"
    storage.mkdir(exist_ok=True)
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={"io_manager": FilesystemIOManager(base_dir=str(storage))},
    )
PY

# ── Dagster defs: resource + question embedding + vector search + RAG LLM ─
mkdir -p "src/${PROJECT_NAME}/defs/supabase_resource"
mkdir -p "src/${PROJECT_NAME}/defs/rag_query_embedding"
mkdir -p "src/${PROJECT_NAME}/defs/retrieved_context"
mkdir -p "src/${PROJECT_NAME}/defs/rag_answer"

# 1. Supabase resource — reused by the vector search asset.
cat > "src/${PROJECT_NAME}/defs/supabase_resource/defs.yaml" <<YAML
type: dagster_community_components.SupabaseResourceComponent
attributes:
  resource_key: supabase
  url_env_var: SUPABASE_URL
  key_env_var: SUPABASE_KEY
YAML

# 2. Embed the user's question via text_embedding_asset — pure YAML,
#    no bespoke Python. Change the `texts:` list to change the demo query.
cat > "src/${PROJECT_NAME}/defs/rag_query_embedding/defs.yaml" <<'YAML'
type: dagster_community_components.TextEmbeddingAssetComponent
attributes:
  asset_name: rag_query_embedding
  texts:
    - "How does Dagster orchestrate around long-running Temporal workflows?"
  model: text-embedding-3-small
  api_key_env_var: OPENAI_API_KEY
  dimensions: 1536
  text_column: question
  embedding_column: embedding
  group_name: rag
YAML

# 3. Similarity search — pgvector cosine via the match_docs RPC.
#    Reads the query embedding from the upstream text_embedding_asset.
cat > "src/${PROJECT_NAME}/defs/retrieved_context/defs.yaml" <<YAML
type: dagster_community_components.SupabaseVectorSearchAssetComponent
attributes:
  asset_name: retrieved_context
  upstream_asset_key: rag_query_embedding
  resource_name: supabase
  table_name: docs
  embedding_column: embedding
  query_embedding_column: embedding
  top_k: 3
  metric: cosine
  additional_columns:
    - title
    - content
  rpc_name: match_docs
  group_name: rag
YAML

# 4. RAG generator — take the retrieved docs and answer the question with an LLM.
cat > "src/${PROJECT_NAME}/defs/rag_answer/defs.yaml" <<YAML
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: rag_answer
  upstream_asset_key: retrieved_context
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.0
  max_tokens: 400

  system_message: |
    You are a documentation assistant. Answer using ONLY the retrieved
    context. Cite the retrieved title(s) inline like [title]. If the
    context doesn't answer the question, say so.

  prompt_template: |
    Retrieved context (top-3 by similarity):
    [{title}]  (similarity {similarity})
    {content}

    Question: How does Dagster orchestrate around long-running Temporal workflows?

  response_column: answer
  group_name: rag
YAML

ok "Wrote 4 defs.yaml (resource + embed question + retrieve + generate)"

# ── Materialize each stage in order ────────────────────────────────────────
# Materialized sequentially so the persistent FilesystemIOManager can hand
# off DataFrames between assets across separate CLI invocations.
DM="${PROJECT_NAME}.definitions"
info "Materializing rag_query_embedding (OpenAI 1536-d embedding of the question)…"
uv run dagster asset materialize --select rag_query_embedding -m "$DM" 2>&1 | tail -4 || fail "query embedding failed"
info "Materializing retrieved_context (pgvector cosine search top-3)…"
uv run dagster asset materialize --select retrieved_context -m "$DM" 2>&1 | tail -4 || fail "retrieval failed"
info "Materializing rag_answer (gpt-4o-mini grounded on retrieved context)…"
uv run dagster asset materialize --select rag_answer -m "$DM" 2>&1 | tail -4 || fail "generation failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Embedded the user question via OpenAI text-embedding-3-small (1536d)
  2. Ran a pgvector cosine-similarity RPC against 8 seeded docs
  3. gpt-4o-mini answered the question grounded ONLY in the top-3 retrieved
     docs, citing them inline

Inspect the retrieved rows + LLM answer:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: question_embedding → retrieved_context → rag_answer
    → click rag_answer → see the LLM's final answer + which docs it cited
    → click retrieved_context → see similarity scores per doc

Add your own docs:
  Edit the seed loop in this script, or write a docs_ingestion component
  that pulls from S3 / SharePoint / Google Drive → embeds → upserts to
  Supabase. Then this same RAG shape works over your live knowledge base.

Live services (still running):
  • Supabase Studio: http://127.0.0.1:54323
  • Supabase REST:   http://127.0.0.1:54321

To stop everything:
  (cd $SB_DIR && supabase stop)
EOF

trap - INT TERM
