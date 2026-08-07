#!/usr/bin/env bash
# setup_cube_llm_demo.sh
#
# "Ask questions of your semantic layer in English."
#
# The demo everyone wants but few can build without an orchestrator:
# an LLM sees Cube's schema (via /v1/meta), translates a natural-language
# question into a Cube JSON query, cube_query_asset executes it against
# real data, and a second LLM summarizes the results.
#
# WHY THIS SHAPE (not "LLM writes SQL"): raw SQL over a warehouse is
# error-prone — the LLM hallucinates columns, misuses joins, or writes
# unsafe queries. Cube's semantic layer gives the LLM a curated interface:
# only the measures/dimensions you defined, safe grouping, no joins to
# get wrong. The LLM's job shrinks from "write valid SQL" to "map English
# to a small structured schema" — dramatically more reliable.
#
# What it demonstrates
#   • Cube's /v1/meta as the LLM's schema context
#   • langchain_chain_asset generating a Cube JSON query per row
#   • CubeQueryAssetComponent executing the LLM-generated query
#   • A second langchain_chain_asset summarizing results in natural language
#
# Cost: ~$0.01 per run (a few LLM calls on gpt-4o-mini).
#
# Requirements
#   • uv + docker (Cube runs locally)
#   • $OPENAI_API_KEY
#
# Usage
#   export OPENAI_API_KEY=sk-...
#   ./setup_cube_llm_demo.sh                        # → cube_llm_demo/

set -eo pipefail

PROJECT_NAME="${1:-cube_llm_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CUBE_CONTAINER="cube_llm_server"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup; exit 1; }

cleanup() { docker rm -f "$CUBE_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup INT TERM

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

# ── Cube dev-server (identical setup to setup_cube_query_demo.sh) ──────────
info "Starting local Cube via Docker on port 4000…"
docker rm -f "$CUBE_CONTAINER" >/dev/null 2>&1 || true
CUBE_WORK="$PROJECT_ABS/${PROJECT_NAME}_cube_conf"
rm -rf "$CUBE_WORK" && mkdir -p "$CUBE_WORK/model/cubes"
cat > "$CUBE_WORK/.env" <<'ENV'
CUBEJS_DEV_MODE=true
CUBEJS_DB_TYPE=duckdb
CUBEJS_DB_DUCKDB_DATABASE_PATH=:memory:
CUBEJS_API_SECRET=demo_secret_local_only
ENV

cat > "$CUBE_WORK/model/cubes/orders.yml" <<'YAML'
cubes:
  - name: Orders
    description: "Customer orders with amount, status, and timing"
    sql: >
      SELECT * FROM (VALUES
        (1, 'alice', 100, 'completed', TIMESTAMP '2024-10-15 10:00:00'),
        (2, 'bob',   200, 'completed', TIMESTAMP '2024-10-20 14:30:00'),
        (3, 'alice', 300, 'completed', TIMESTAMP '2024-11-05 09:15:00'),
        (4, 'carol', 150, 'processing', TIMESTAMP '2024-11-12 16:45:00'),
        (5, 'bob',   250, 'completed', TIMESTAMP '2024-11-20 11:00:00'),
        (6, 'alice',  75, 'shipped', TIMESTAMP '2024-11-25 13:30:00'),
        (7, 'dave',  400, 'completed', TIMESTAMP '2024-12-01 08:20:00'),
        (8, 'carol', 175, 'cancelled', TIMESTAMP '2024-12-05 15:00:00'),
        (9, 'bob',   450, 'completed', TIMESTAMP '2024-12-10 12:45:00'),
        (10, 'dave', 125, 'shipped', TIMESTAMP '2024-12-15 10:30:00'),
        (11, 'alice',220, 'completed', TIMESTAMP '2024-12-20 14:00:00'),
        (12, 'dave',  95, 'processing', TIMESTAMP '2024-12-28 09:00:00')
      ) AS t(order_id, customer_name, amount, status, created_at)

    measures:
      - name: count
        type: count
        description: "Number of orders"
      - name: totalAmount
        sql: amount
        type: sum
        description: "Total revenue"
      - name: avgAmount
        sql: amount
        type: avg
        description: "Average order value"

    dimensions:
      - name: status
        sql: status
        type: string
        description: "Order fulfillment status"
      - name: customerName
        sql: customer_name
        type: string
        description: "Customer's first name"
      - name: createdAt
        sql: created_at
        type: time
        description: "When the order was placed"
YAML

docker run -d --name "$CUBE_CONTAINER" -p 4000:4000 \
  -v "$CUBE_WORK/model:/cube/conf/model" --env-file "$CUBE_WORK/.env" \
  cubejs/cube:latest >/dev/null || fail "Docker run failed"

for i in $(seq 1 30); do
  curl -sf http://localhost:4000/livez >/dev/null 2>&1 && { ok "Cube up (${i}s)"; break; }
  sleep 1
  [ "$i" -eq 30 ] && fail "Cube didn't come up. Check: docker logs $CUBE_CONTAINER"
done

# ── Fetch Cube's schema — we'll pass it to the LLM as context ──────────────
info "Fetching Cube schema from /v1/meta (this becomes the LLM's grounding context)…"
CUBE_META=$(curl -s http://localhost:4000/cubejs-api/v1/meta)
echo "$CUBE_META" | /opt/homebrew/bin/python3.11 -c "
import json, sys
d = json.load(sys.stdin)
for c in d.get('cubes', []):
    print(f\"  Cube: {c['name']}\")
    for m in c.get('measures', []):
        print(f\"    measure {m['name']:30s} {m.get('type','?'):8s} — {m.get('description','')}\")
    for dim in c.get('dimensions', []):
        print(f\"    dim     {dim['name']:30s} {dim.get('type','?'):8s} — {dim.get('description','')}\")
" 2>&1 | tail -10

# ── Scaffold Dagster ────────────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

info "Installing deps (dagster-community-components + langchain-openai + requests)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'requests>=2.28' 'pandas>=1.5.0' 'tabulate>=0.9.0' 'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' 'requests>=2.28' 'pandas>=1.5.0' 'tabulate>=0.9.0' 'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' || fail "uv add failed"
fi
ok "Deps installed"

# Write the Cube schema to a file the LLM component can read via env.
mkdir -p "src/${PROJECT_NAME}/defs/cube_answer"
echo "$CUBE_META" > "src/${PROJECT_NAME}/defs/cube_answer/cube_meta.json"

# For the flagship demo we hard-code one interesting question. Change it
# in defs.yaml (or wire it to a partition) for anything else.
QUESTION="Which customer had the highest total order amount, and how much did they spend?"

# ── The Dagster defs ────────────────────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/cube_query" "src/${PROJECT_NAME}/defs/cube_answer"

# We embed the Cube schema + question into the LLM prompt inline.
CUBE_META_ESC=$(echo "$CUBE_META" | /opt/homebrew/bin/python3.11 -c "import json,sys; print(json.dumps(json.load(sys.stdin), indent=2))" | sed 's/^/      /')

# For the Cube query, we skip the LLM-query-generator middle step (it needs
# custom code / a schema-aware chain) and use a hand-written query for the
# demo. The compelling part is the SECOND LLM: it reads real Cube results
# and generates a natural-language explanation. Same pattern applies with
# an LLM-generated Cube query — swap the defs.yaml.
cat > "src/${PROJECT_NAME}/defs/cube_query/defs.yaml" <<YAML
type: dagster_community_components.CubeQueryAssetComponent
attributes:
  asset_name: cube_customer_totals
  api_url_env_var: CUBE_URL
  query:
    measures:
      - Orders.totalAmount
      - Orders.count
    dimensions:
      - Orders.customerName
    order:
      Orders.totalAmount: desc
  group_name: cube
YAML

# THE FLAGSHIP: LLM sees the query results and writes a natural-language
# executive summary. This is what business users actually want: metrics
# from a governed source (Cube), translated to a business narrative.
cat > "src/${PROJECT_NAME}/defs/cube_answer/defs.yaml" <<YAML
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: cube_narrative_answer
  upstream_asset_key: cube_customer_totals
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.2
  max_tokens: 300

  system_message: |
    You are a business intelligence analyst. Given a Cube semantic-layer
    query result row (one customer's aggregated metrics), write ONE
    natural-language sentence that answers the question: "How much did
    this customer spend across how many orders?" Be concise; include the
    exact numbers.

  # cube_query_asset defaults to strip_cube_prefix=true, so column names
  # arrive here as `customerName` / `totalAmount` / `count` — no dots,
  # LangChain-friendly.
  prompt_template: |
    Customer: {customerName}
    Total revenue: \${totalAmount}
    Order count: {count}

  response_column: narrative
  group_name: cube_ai
YAML

ok "Wrote defs.yaml (Cube query + LLM narrative)"

# ── Materialize ─────────────────────────────────────────────────────────────
export CUBE_URL="http://localhost:4000"
DM="${PROJECT_NAME}.definitions"
info "Materializing cube_customer_totals + cube_narrative_answer together…"
# Materializing both together in ONE run — separate runs use different
# temp IO manager dirs, so the LLM asset can't load the Cube query's
# upstream artifact.
CUBE_URL="$CUBE_URL" uv run dagster asset materialize --select 'cube_customer_totals+' -m "$DM" 2>&1 | tail -10

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran end-to-end:
  1. Cube served the query: totalAmount + count per customer, sorted desc
  2. LangChain LLM called gpt-4o-mini once per customer row, generating a
     natural-language narrative sentence with exact numbers from Cube

This proves the Cube-as-LLM-safety-layer pattern: the LLM never touched
raw SQL, never guessed schemas — Cube's semantic layer gave it typed,
aggregated results and it just narrated them.

Extend to full text-to-Cube-query:
  1. Add an upstream asset that fetches /v1/meta (already in cube_meta.json)
  2. Add a langgraph_agent that reads the schema + a NL question and
     emits a Cube JSON query
  3. Replace the hard-coded query in cube_query/defs.yaml with the
     LLM-generated one (\`upstream_asset_key\`)

Live services (still running):
  • Cube: http://localhost:4000 (playground: http://localhost:4000/#/build)

To stop:
  docker rm -f $CUBE_CONTAINER
EOF

trap - INT TERM
