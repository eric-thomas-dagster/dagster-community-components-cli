#!/usr/bin/env bash
# setup_mcp_dagster_plus_demo.sh
#
# Dagster+ MCP server — wire MCPToolCallComponent to hit
#   https://mcp.agent.dagster.cloud/mcp/
# via HTTP transport with two required headers:
#   Authorization: Bearer <DAGSTER_CLOUD_TOKEN>
#   Dagster-Cloud-Organization: <DAGSTER_CLOUD_ORG>
#
# Validates:
#   MCPToolCallComponent  — single-shot deterministic MCP call (no LLM)
#
# The demo scaffolds a Dagster project and wires a defs.yaml that connects
# to your Dagster+ org's MCP endpoint. `dg check defs` validates the YAML
# shape locally; end-to-end materialization requires your real token +
# org slug (both exported as env vars — never committed).
#
# Cost: $0 (Dagster+ MCP is included with your Dagster+ subscription).
# Requirements: uv. Token: get one from Admin → tokens in Dagster+.

set -eo pipefail

PROJECT_NAME="${1:-mcp_dagster_plus_demo}"
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

# ── defs.yaml — one MCPToolCallComponent per tool you want to call ──────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/mcp_dagster_plus_list_runs"

cat > "src/${PKG}/defs/mcp_dagster_plus_list_runs/defs.yaml" <<'YAML'
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: dagster_plus_recent_runs
  server:
    name: dagster_plus
    type: http
    url: "https://mcp.agent.dagster.cloud/mcp/"
    headers_env:
      Authorization: DAGSTER_CLOUD_TOKEN_HEADER   # value = "Bearer <token>"
      Dagster-Cloud-Organization: DAGSTER_CLOUD_ORG
  # NOTE: swap `tool_name` + `tool_args` for a tool your org's MCP exposes.
  # To discover available tools, ask the MCP server via `tools/list` from
  # the mcp Python client, or run any MCP-inspecting UI (Claude Desktop,
  # Cursor, etc.) against the same URL + headers.
  #
  # Common shapes worth trying (subject to Dagster+ MCP evolution):
  #   tool_name: list_runs
  #   tool_args: {limit: 25}
  #   tool_name: get_asset
  #   tool_args: {asset_key: "my_asset"}
  tool_name: list_runs
  tool_args:
    limit: 25
  parse_as: auto
  group_name: dagster_plus_mcp
  description: |
    Recent runs from your Dagster+ org's MCP server. Materializes into a
    Dagster asset alongside your normal pipelines — same catalog, same
    lineage graph.
YAML

ok "Wrote 1 defs.yaml (100% components — no custom Python)"

info "Running dg check defs (validates YAML shape; requires real env vars for materialization)…"
# Set placeholder env vars so dg check defs doesn't fail on EnvVar resolution.
export DAGSTER_CLOUD_TOKEN_HEADER="Bearer placeholder"
export DAGSTER_CLOUD_ORG="placeholder"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated (code-level — YAML shape is correct)"


cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  MCP endpoint: https://mcp.agent.dagster.cloud/mcp/

To materialize against your real Dagster+ org:
  cd ${PROJECT_NAME}
  # 1. Grab a user token from Dagster+: Admin → tokens
  # 2. Combine the Bearer prefix into the env var value:
  export DAGSTER_CLOUD_TOKEN_HEADER="Bearer YOUR_USER_TOKEN"
  export DAGSTER_CLOUD_ORG="your-org-slug"
  uv run dg dev            # → http://localhost:3000

The dagster_plus_recent_runs asset will emit a real MCP tool call over HTTPS
to your org's MCP endpoint. Swap tool_name / tool_args in
src/${PKG}/defs/mcp_dagster_plus_list_runs/defs.yaml for whatever tools
your Dagster+ MCP surfaces.

Discover available tools:
  uv run python -c "
import asyncio, os
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    headers = {
        'Authorization': os.environ['DAGSTER_CLOUD_TOKEN_HEADER'],
        'Dagster-Cloud-Organization': os.environ['DAGSTER_CLOUD_ORG'],
    }
    async with streamablehttp_client('https://mcp.agent.dagster.cloud/mcp/', headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            for t in tools.tools:
                print(f'  {t.name:30}  {t.description}')
asyncio.run(main())
"

No cleanup needed — nothing runs locally.
EOF
