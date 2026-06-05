#!/usr/bin/env bash
# Agent family demo — one Dagster project, three new components, one MCP server.
#
# WHAT THIS DEMONSTRATES
#   The full agent stack we just shipped, in one materialization chain:
#
#     mcp_tool_call (no LLM — pure tool dispatch)
#                    │
#                    ▼
#         deterministic_fs_listing  (the result of the tool call)
#
#     openai_agent  (LLM picks tools + iterates)
#                    │
#                    ▼
#         fs_agent_answer  (final_answer + transcript + tool_call_details)
#                    │
#                    ▼
#     llm_evaluator (scores the agent's answer)
#                    │
#                    ▼
#         fs_agent_eval  (answer + evaluations: {relevance, helpfulness, ...})
#
#   All three share the same MCP server (the official no-auth
#   @modelcontextprotocol/server-filesystem launched via npx).
#
# COST: <$0.001 — gpt-4o-mini for both the agent (~3 iterations) + the
# judge (3 evals).
#
# REQUIREMENTS: uv, npx, OPENAI_API_KEY.

set -eo pipefail

PROJECT_DIR="${1:-agent-family-demo}"
FS_DIR="${FS_DIR:-/tmp/agent-family-demo-fs}"

# Canonicalize the sandbox path so the MCP filesystem server (which
# rejects symlink traversal — e.g. /tmp → /private/tmp on Mac) is happy
# with whatever we hand it. Without this, the deterministic mcp_tool_call
# fails on the first call; the agent's loop self-corrects but a one-shot
# can't.
if command -v greadlink >/dev/null 2>&1; then
  FS_DIR="$(greadlink -f "$FS_DIR" 2>/dev/null || echo "$FS_DIR")"
elif readlink -f "$FS_DIR" >/dev/null 2>&1; then
  FS_DIR="$(readlink -f "$FS_DIR")"
else
  # Mac fallback: resolve the parent then re-append the leaf.
  parent="$(dirname "$FS_DIR")"; leaf="$(basename "$FS_DIR")"
  if [ -d "$parent" ]; then
    FS_DIR="$(cd "$parent" && pwd -P)/$leaf"
  fi
fi
echo ">>> Using canonical FS_DIR=$FS_DIR"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if ! command -v npx >/dev/null 2>&1; then echo "✗ npx required (install Node.js)"; exit 1; fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "✗ OPENAI_API_KEY not set. Either set it or swap model + api_key_env_var"
  echo "  in the defs.yaml files after scaffolding (anthropic / gemini / litellm all work)."
  exit 1
fi

# --- 1. Sandbox filesystem so the agent has something to find -----------
echo ">>> Sandbox at $FS_DIR"
rm -rf "$FS_DIR"
mkdir -p "$FS_DIR"
echo "tiny config" > "$FS_DIR/config.txt"
dd if=/dev/zero of="$FS_DIR/big_dataset.bin" bs=1024 count=2048 2>/dev/null
dd if=/dev/zero of="$FS_DIR/medium_log.bin"  bs=1024 count=512  2>/dev/null
echo "{\"version\":42}" > "$FS_DIR/manifest.json"
ls -lah "$FS_DIR"
echo ""

# --- 2. Scaffold Dagster project ----------------------------------------
echo ">>> Scaffolding $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'openai>=1.30.0' 'litellm>=1.30.0' 'mcp>=1.0.0' 'requests>=2.28'

# --- 3. Install the three components via the CLI ------------------------
echo ">>> Installing mcp_tool_call + openai_agent + llm_evaluator"
CLI="uvx --from dagster-community-components-cli dagster-component"
$CLI add mcp_tool_call  --auto-install >/dev/null 2>&1
$CLI add openai_agent   --auto-install >/dev/null 2>&1
$CLI add llm_evaluator  --auto-install >/dev/null 2>&1

# --- 4. Wire defs.yaml for each of the three ----------------------------
# 4a. deterministic_fs_listing — pure mcp_tool_call, no LLM.
cat > "src/$PKG/defs/mcp_tool_call/defs.yaml" <<EOF
# yaml-language-server: \$schema=https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/sources/mcp_tool_call/schema.json
type: $PKG.components.mcp_tool_call.component.MCPToolCallComponent
attributes:
  asset_name: deterministic_fs_listing
  server:
    name: fs
    type: stdio
    command:
      - npx
      - -y
      - "@modelcontextprotocol/server-filesystem"
      - "$FS_DIR"
  tool_name: list_directory_with_sizes
  tool_args:
    path: "$FS_DIR"
    sortBy: size
  parse_as: text                 # filesystem MCP returns plain-text listing
  group_name: ai
  description: "Deterministic 'show me /agent-family-demo-fs sorted by size' — no LLM."
EOF

# 4b. fs_agent_answer — openai_agent against the same MCP server.
cat > "src/$PKG/defs/openai_agent/defs.yaml" <<EOF
# yaml-language-server: \$schema=https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/openai_agent/schema.json
type: $PKG.components.openai_agent.component.OpenAIAgentComponent
attributes:
  asset_name: fs_agent_answer
  prompt: "Find the three largest files in $FS_DIR. Return them as a JSON list of {path, size_bytes} entries, biggest first."
  system_prompt: "You are a filesystem assistant. Use the available tools precisely."
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 8
  mcp_servers:
    - name: fs
      type: stdio
      command:
        - npx
        - -y
        - "@modelcontextprotocol/server-filesystem"
        - "$FS_DIR"
  group_name: ai
EOF

# 4c. fs_agent_eval — llm_evaluator scoring the agent's answer.
cat > "src/$PKG/defs/llm_evaluator/defs.yaml" <<EOF
# yaml-language-server: \$schema=https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/llm_evaluator/schema.json
type: $PKG.components.llm_evaluator.component.LLMEvaluatorComponent
attributes:
  asset_name: fs_agent_eval
  upstream_asset_key: fs_agent_answer
  evaluations:
    - answer_relevance
    - helpfulness
    - coherence
  model: gpt-4o-mini             # cheap judge model
  api_key_env_var: OPENAI_API_KEY
  group_name: ai
EOF

# --- 5. Validate + materialize ------------------------------------------
echo ""
echo ">>> Validating defs"
uv run dg check defs 2>&1 | tail -3

echo ""
echo ">>> Materializing the chain: deterministic_fs_listing → fs_agent_answer → fs_agent_eval"
export DAGSTER_HOME="$(pwd)/.demo_dagster_home"
mkdir -p "$DAGSTER_HOME"
touch "$DAGSTER_HOME/dagster.yaml"
uv run dg launch --assets 'deterministic_fs_listing,fs_agent_answer,fs_agent_eval' 2>&1 \
  | grep -E "\[mcp:|\[iter|\[eval:|calling|RUN_SUCCESS|RUN_FAILURE|STEP_FAILURE|ERROR" | tail -20

# --- 6. Surface the materialized values ---------------------------------
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Chain output"
echo "──────────────────────────────────────────────────────────────────────"
uv run python <<EOF
import pickle
print(">>> deterministic_fs_listing  (mcp_tool_call — no LLM)")
with open("$DAGSTER_HOME/storage/deterministic_fs_listing", "rb") as f:
    v = pickle.load(f)
print(str(v)[:500])
print()
print(">>> fs_agent_answer  (openai_agent — gpt-4o-mini + MCP tool loop)")
with open("$DAGSTER_HOME/storage/fs_agent_answer", "rb") as f:
    a = pickle.load(f)
print(f"  iterations: {a['iterations']}  tool_calls: {a['tool_calls_made']}  stopped: {a['stopped_reason']}")
print("  final_answer:")
print(a["final_answer"])
print()
print(">>> fs_agent_eval  (llm_evaluator — scores the agent's answer)")
with open("$DAGSTER_HOME/storage/fs_agent_eval", "rb") as f:
    e = pickle.load(f)
for name, sc in e["evaluations"].items():
    print(f"  {name:<20}  score={sc['score']:.2f}  reason={sc['reason'][:120]}")
EOF

echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "Teardown:"
echo "  rm -rf $PROJECT_DIR $FS_DIR"
echo ""
echo "Re-run only the agent (chain already materialized):"
echo "  cd $PROJECT_DIR"
echo "  export DAGSTER_HOME=\$(pwd)/.demo_dagster_home"
echo "  uv run dg launch --assets fs_agent_answer"
echo "──────────────────────────────────────────────────────────────────────"
