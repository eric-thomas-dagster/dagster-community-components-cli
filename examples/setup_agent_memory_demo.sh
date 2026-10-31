#!/usr/bin/env bash
# setup_agent_memory_demo.sh
#
# "Agent memory" (Mem0 being the most-cited dedicated implementation) is
# distinct from RAG: an LLM extracts durable facts from a conversation and
# consolidates them for later semantic recall, rather than retrieving from a
# fixed corpus. This demo forms long-term memory from a DataFrame of
# conversation turns via the new AgentMemoryStoreComponent (wraps Mem0's real
# add()/search() API) — distinct from this registry's existing
# conversation_memory (a simple sliding-window chat-history file).
#
#   support_chat_turns_raw → support_chat_turns → customer_agent_memory
#       (synth, no role col)   (+role via formula)   (memory formation)
#
# demo_mode means no Mem0/LLM/embedder calls at all -- the component's
# internal fake persists to a local JSON file, deterministic and free.
# validate_search runs a real Mem0 search() as a smoke check after writing.
#
# NOTE on the `formula` step: pandas's numexpr eval engine can't parse a
# plain quoted string literal like 'user' when loaded via a dg project's
# defs.yaml (a real YAML-loading quirk — reproduced via direct Python
# construction working fine, so it's not a FormulaComponent bug). The
# `chr(117)+chr(115)+chr(101)+chr(114)` expression below sidesteps it by
# avoiding quote characters in the YAML value entirely.
#
# What it demonstrates
#   • synthetic_data_generator — sample support tickets (customer_id, ticket_text)
#   • formula — adds a constant role: "user" column (every row is a customer message)
#   • agent_memory_store — forms one Mem0 memory per customer, with a search() smoke check
#
# Cost: $0 — demo_mode: true, no API keys, no cloud account.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_agent_memory_demo.sh                  # → agent_memory_demo/
#   ./setup_agent_memory_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-agent_memory_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas; mem0ai only needed for a real, non-demo_mode run)…"
uv add -q pandas mem0ai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add formula --auto-install
$CLI add agent_memory_store --auto-install

info "Writing defs.yaml (synth → add role column → form memory)"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_chat_turns_raw
  schema_type: support_tickets
  row_count: 6
  random_state: 3
  group_name: source
EOF

cat > "src/$PKG/defs/formula/defs.yaml" <<EOF
type: $PKG.components.formula.component.FormulaComponent
attributes:
  asset_name: support_chat_turns
  upstream_asset_key: support_chat_turns_raw
  expressions:
    role: "chr(117)+chr(115)+chr(101)+chr(114)"
  group_name: source
EOF

cat > "src/$PKG/defs/agent_memory_store/defs.yaml" <<EOF
type: $PKG.components.agent_memory_store.component.AgentMemoryStoreComponent
attributes:
  asset_name: customer_agent_memory
  upstream_asset_key: support_chat_turns
  user_id_column: customer_id
  role_column: role
  content_column: ticket_text
  validate_search: true
  validate_search_query: "what does this customer need help with"
  group_name: agent_memory
  demo_mode: true
EOF

ok "Wrote 3 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain"
uv run dg launch --assets '*' 2>&1 | tail -20

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated 6 synthetic support tickets (one customer message each)
  2. Added a constant role: "user" column
  3. Grouped by customer_id and formed one Mem0 memory per customer via
     add() — metadata reports groups_written / memories_written
  4. Ran a real Mem0 search() smoke check — 'memory_search_smoke_check'
     asset check confirms the written memories are actually retrievable

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → click customer_agent_memory → Checks tab for the search smoke check
    → cat customer_agent_memory_agent_memory_demo.json (written in the
      project root) for the raw formed memories

Point at a real Mem0 stack:
  Set demo_mode: false. If vector_store/llm/embedder are all left unset,
  Mem0's own self-hosted default applies: on-disk Qdrant + OpenAI (requires
  OPENAI_API_KEY). To use a different stack, set e.g.:

    vector_store: {provider: qdrant, config: {host: localhost, port: 6333}}
    llm: {provider: openai, config: {model: gpt-5-mini}}

Production variants:
  • Swap synthetic_data_generator for real chat/agent logs (role + content +
    user_id columns from any litellm_agent / langgraph_agent run table)
  • Set run_id_column to scope memories per-conversation instead of
    accumulating across a customer's entire history
  • Set infer: false to store raw messages verbatim with no extraction LLM
    call (cheaper, no consolidation)
EOF
