#!/usr/bin/env bash
# LiteLLM Agent with MCP — end-to-end demo, live-validated.
#
# WHAT THIS DEMONSTRATES
#   The litellm_agent component running a tool-calling loop against
#   the official MCP filesystem server (no auth, runs via npx).
#
#   Asset graph:
#
#     litellm_agent (single asset)
#       │
#       ├─ on materialize: starts MCP filesystem server as subprocess
#       ├─ discovers 14 tools (read_file, write_file, list_directory, ...)
#       ├─ asks the LLM the configured prompt with all tool defs attached
#       ├─ loops: tool_call → MCP → result → next iteration
#       └─ stops when the model returns a plain text answer
#
# COST: ~$0.0005 per run with gpt-4o-mini (3-4 iterations).
#
# REQUIREMENTS
#   - OPENAI_API_KEY (or swap model + api_key_env_var to use anthropic/gemini/groq/etc)
#   - npx (Node.js) — used to launch the MCP filesystem server
#   - uv

set -eo pipefail

PROJECT_DIR="${1:-litellm-agent-demo}"
FS_DIR="${FS_DIR:-/tmp/litellm-agent-demo-fs}"

# --- 0. Tool / env check -----------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1
fi
if ! command -v npx >/dev/null 2>&1; then
  echo "✗ npx required — install Node.js so the MCP filesystem server can launch"; exit 1
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "✗ OPENAI_API_KEY not set. The demo defaults to gpt-4o-mini."
  echo "  To use a different provider, edit the defs.yaml after scaffolding."
  exit 1
fi

# --- 1. Drop a few files in a sandbox dir so the agent has something to find
echo ">>> Preparing sandbox at $FS_DIR"
rm -rf "$FS_DIR"
mkdir -p "$FS_DIR"
echo "tiny config file" > "$FS_DIR/config.txt"
dd if=/dev/zero of="$FS_DIR/big_dataset.bin" bs=1024 count=2048 2>/dev/null
dd if=/dev/zero of="$FS_DIR/medium_log.bin" bs=1024 count=512 2>/dev/null
echo "{\"version\": 42}" > "$FS_DIR/manifest.json"
ls -lah "$FS_DIR"

# --- 2. Scaffold Dagster project ---------------------------------------
echo ""
echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'litellm>=1.30.0' 'mcp>=1.0.0'

# --- 3. Install the litellm_agent component ----------------------------
echo ">>> Installing litellm_agent component into the project"
uvx --from dagster-community-components-cli dagster-component \
  add litellm_agent --auto-install >/dev/null 2>&1

# --- 4. Write defs.yaml ------------------------------------------------
cat > "src/$PKG/defs/litellm_agent/defs.yaml" <<EOF
# yaml-language-server: \$schema=https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/litellm_agent/schema.json
type: $PKG.components.litellm_agent.component.LiteLLMAgentComponent
attributes:
  asset_name: filesystem_agent_run
  prompt: "List the three largest files in $FS_DIR by size. Return them as a JSON list of {path, size_bytes} entries."
  system_prompt: "You are a filesystem assistant. Use the available tools to answer questions precisely."
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

# --- 5. Validate + materialize -----------------------------------------
echo ""
echo ">>> Validating defs"
uv run dg check defs 2>&1 | tail -3

echo ""
echo ">>> Materializing the agent (this calls the OpenAI API; ~5-15s)"
export DAGSTER_HOME="$(pwd)/.demo_dagster_home"
mkdir -p "$DAGSTER_HOME"
touch "$DAGSTER_HOME/dagster.yaml"
uv run dg launch --assets filesystem_agent_run 2>&1 \
  | grep -E "\[mcp:|\[iter|RUN_SUCCESS|RUN_FAILURE|STEP_FAILURE|ERROR" | tail -15

# --- 6. Inspect what came back ----------------------------------------
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "  Agent run result"
echo "──────────────────────────────────────────────────────────────────────"
uv run python <<EOF
import pickle, json
with open("$DAGSTER_HOME/storage/filesystem_agent_run", "rb") as f:
    r = pickle.load(f)
print(f"Iterations:      {r['iterations']}")
print(f"Tool calls made: {r['tool_calls_made']}")
print(f"Stopped reason:  {r['stopped_reason']}")
print(f"MCP servers:     {r['mcp_servers']}")
print()
print("--- TOOL CALL TRAJECTORY ---")
for tc in r["tool_call_details"]:
    err = " [ERR]" if tc.get("is_error") else ""
    print(f"  iter {tc['iteration']}: {tc['tool']}{err}")
    print(f"    args: {tc['args']}")
print()
print("--- FINAL ANSWER ---")
print(r["final_answer"])
EOF

echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "Teardown when you're done:"
echo "  rm -rf $PROJECT_DIR $FS_DIR"
echo ""
echo "Re-run the agent only:"
echo "  cd $PROJECT_DIR"
echo "  export DAGSTER_HOME=\$(pwd)/.demo_dagster_home"
echo "  uv run dg launch --assets filesystem_agent_run"
echo "──────────────────────────────────────────────────────────────────────"
