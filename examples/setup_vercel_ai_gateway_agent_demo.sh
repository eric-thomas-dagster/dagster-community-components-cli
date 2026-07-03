#!/usr/bin/env bash
# setup_vercel_ai_gateway_agent_demo.sh
#
# Single-shot LLM agent routed through Vercel AI Gateway — one credential,
# any provider (OpenAI, Anthropic, Google, xAI, Groq, …), unified billing,
# automatic fallback.
#
# What it demonstrates
#   • VercelAIGatewayAgentComponent — asset that runs an LLM turn (or
#     tool-calling loop) via https://ai-gateway.vercel.sh/v1.
#   • Model routing via '<provider>/<model>' strings.
#   • Fallback config (auto-retry across providers on rate-limit / outage).
#
# Cost: ~$0.001 per run on openai/gpt-4o-mini via the Vercel AI Gateway.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • $VERCEL_AI_TOKEN in your shell (AI-Gateway–scoped key, format vck_...)
#     Create at your Vercel dashboard → AI Gateway → API Keys.
#     The account needs a positive credit balance (top up at
#     Vercel dashboard → AI → billing).
#
# Usage
#   export VERCEL_AI_TOKEN=vck_...
#   ./setup_vercel_ai_gateway_agent_demo.sh                # → vercel_aig_demo/
#   ./setup_vercel_ai_gateway_agent_demo.sh my_project     # custom name

set -eo pipefail

PROJECT_NAME="${1:-vercel_aig_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${VERCEL_AI_TOKEN:-}" ] && fail "VERCEL_AI_TOKEN not set. Create an AI Gateway key in the Vercel dashboard (AI → API Keys)."
command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

info "VERCEL_AI_TOKEN: set (prefix ${VERCEL_AI_TOKEN:0:8}…)"
info "Target project: $PROJECT_DIR"

info "Verifying AI Gateway auth + credits…"
CURL_OUT=$(curl -s -X POST "https://ai-gateway.vercel.sh/v1/chat/completions" \
  -H "Authorization: Bearer $VERCEL_AI_TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"reply with exactly one word: ok"}],"max_tokens":32}' || true)
if echo "$CURL_OUT" | grep -q '"content"'; then
  ok "AI Gateway reachable + credited"
elif echo "$CURL_OUT" | grep -q "insufficient_funds"; then
  fail "AI Gateway auth OK but the account has no credits. Top up at Vercel dashboard → AI → billing."
else
  echo "$CURL_OUT" | head -5
  fail "AI Gateway smoke call failed. Check the token."
fi

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + openai)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'openai>=1.0.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components>=0.10.0' 'openai>=1.0.0' || fail "uv add failed"
fi
ok "Dependencies installed"

# ── Multi-model demo: three agents routed through the same gateway ──────────
mkdir -p "src/${PROJECT_NAME}/defs/research_openai"      \
         "src/${PROJECT_NAME}/defs/research_anthropic"   \
         "src/${PROJECT_NAME}/defs/research_google"

QUESTION="In 40 words: what's the practical difference between Dagster's asset lineage and dbt's model DAG?"

cat > "src/${PROJECT_NAME}/defs/research_openai/defs.yaml" <<YAML
type: dagster_community_components.VercelAIGatewayAgentComponent
attributes:
  asset_name: research_openai
  prompt: "${QUESTION}"
  system_prompt: "You are a concise technical writer."
  model: openai/gpt-4o-mini
  api_key_env_var: VERCEL_AI_TOKEN
  temperature: 0.2
  max_tokens: 200
  group_name: ai_gateway
YAML

cat > "src/${PROJECT_NAME}/defs/research_anthropic/defs.yaml" <<YAML
type: dagster_community_components.VercelAIGatewayAgentComponent
attributes:
  asset_name: research_anthropic
  prompt: "${QUESTION}"
  system_prompt: "You are a concise technical writer."
  model: anthropic/claude-haiku-4-5
  api_key_env_var: VERCEL_AI_TOKEN
  temperature: 0.2
  max_tokens: 200
  group_name: ai_gateway
YAML

cat > "src/${PROJECT_NAME}/defs/research_google/defs.yaml" <<YAML
type: dagster_community_components.VercelAIGatewayAgentComponent
attributes:
  asset_name: research_google
  prompt: "${QUESTION}"
  system_prompt: "You are a concise technical writer."
  model: google/gemini-2.5-flash
  api_key_env_var: VERCEL_AI_TOKEN
  temperature: 0.2
  max_tokens: 200

  # Fallback: if Gemini rate-limits or errors, hop to another provider.
  fallback_models:
    - openai/gpt-4o-mini
    - anthropic/claude-haiku-4-5
  group_name: ai_gateway
YAML

ok "Wrote 3 agent defs (one per provider, all routed through Vercel AI Gateway)"

DM="${PROJECT_NAME}.definitions"
info "Materializing research_openai (routed via Vercel → OpenAI)…"
uv run dagster asset materialize --select research_openai -m "$DM" 2>&1 | tail -3
info "Materializing research_anthropic (routed via Vercel → Anthropic)…"
uv run dagster asset materialize --select research_anthropic -m "$DM" 2>&1 | tail -3
info "Materializing research_google (routed via Vercel → Gemini, with fallbacks)…"
uv run dagster asset materialize --select research_google -m "$DM" 2>&1 | tail -3

echo
ok "Demo complete."
echo
cat <<EOF
Next steps:
  cd $PROJECT_NAME
  uv run dg dev

In the UI browse to the 'ai_gateway' asset group:
  • Three agents, three providers, ONE Vercel API key.
  • Each asset's metadata shows model_requested + model_used (differ if fallback fired).
  • Vercel dashboard → AI Gateway shows every call, cost, latency across providers.

Extend with tools:
  Add an mcp_servers: block to any of the defs.yaml — same shape as
  anthropic_agent / openai_agent (stdio / http / sse). The gateway-routed
  models will call the MCP tools via OpenAI's tool-calling API.
EOF
