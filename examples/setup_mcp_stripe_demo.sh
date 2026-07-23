#!/usr/bin/env bash
# setup_mcp_stripe_demo.sh
#
# Stripe MCP server — wire MCPToolCallComponent to hit
#   https://mcp.stripe.com/
# via HTTP transport with:
#   Authorization: Bearer <STRIPE_API_KEY>
#
# Validates:
#   MCPToolCallComponent  — single-shot deterministic MCP call (no LLM)
#
# The demo scaffolds a Dagster project and wires a defs.yaml that connects
# to Stripe's official MCP server. `dg check defs` validates the YAML shape
# locally; end-to-end materialization requires your Stripe API key (a
# restricted key is strongly recommended — never commit the raw key).
#
# Cost: $0 (Stripe MCP is free — you pay only for whatever tools do
# billable Stripe API calls; read-only queries are free).
# Requirements: uv. Key: create a restricted key at
#   https://dashboard.stripe.com/apikeys with only the scopes you need.

set -eo pipefail

PROJECT_NAME="${1:-mcp_stripe_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" mcp >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    mcp >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml — one MCPToolCallComponent per Stripe MCP tool ────────────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/stripe_mcp_list_customers"

cat > "src/${PKG}/defs/stripe_mcp_list_customers/defs.yaml" <<'YAML'
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: stripe_recent_customers
  server:
    name: stripe
    type: http
    url: "https://mcp.stripe.com/"
    headers_env:
      Authorization: STRIPE_MCP_AUTHORIZATION_HEADER   # value = "Bearer <key>"
  # NOTE: swap `tool_name` + `tool_args` for a tool Stripe MCP exposes.
  # Discover the surface via `tools/list` — see walkthrough for the snippet.
  #
  # Common shapes (subject to Stripe MCP evolution):
  #   tool_name: list_customers        args: {limit: 25}
  #   tool_name: retrieve_balance      args: {}
  #   tool_name: list_charges          args: {limit: 50}
  #   tool_name: create_payment_link   args: {price: "price_...", quantity: 1}
  tool_name: list_customers
  tool_args:
    limit: 25
  parse_as: auto
  group_name: stripe_mcp
  description: |
    Recent customers from Stripe's official MCP server. Materializes as a
    first-class Dagster asset alongside your normal pipelines — same
    catalog, same lineage graph.
YAML

ok "Wrote 1 defs.yaml (100% components — no custom Python)"

info "Running dg check defs (validates YAML shape; requires real env vars for materialization)…"
# Set a placeholder env var so dg check defs doesn't fail on EnvVar resolution.
export STRIPE_MCP_AUTHORIZATION_HEADER="Bearer placeholder"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated (code-level — YAML shape is correct)"

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  MCP endpoint: https://mcp.stripe.com/

To materialize against your real Stripe account:
  cd ${PROJECT_NAME}
  # 1. Create a RESTRICTED key at https://dashboard.stripe.com/apikeys
  #    Grant only the scopes you need — restricted keys are safer than
  #    the full-access secret key.
  # 2. Combine the Bearer prefix into the env var value:
  export STRIPE_MCP_AUTHORIZATION_HEADER="Bearer rk_live_YOUR_RESTRICTED_KEY"
  uv run dg dev            # → http://localhost:3000

The stripe_recent_customers asset will emit a real MCP tool call over
HTTPS to Stripe's MCP endpoint. Swap tool_name / tool_args in
src/${PKG}/defs/stripe_mcp_list_customers/defs.yaml for whatever tools
Stripe MCP surfaces.

Discover available tools:
  uv run python -c "
import asyncio, os
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    headers = {'Authorization': os.environ['STRIPE_MCP_AUTHORIZATION_HEADER']}
    async with streamablehttp_client('https://mcp.stripe.com/', headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            for t in tools.tools:
                print(f'  {t.name:30}  {t.description}')
asyncio.run(main())
"

No cleanup needed — nothing runs locally.
EOF
