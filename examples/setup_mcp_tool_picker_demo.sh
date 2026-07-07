#!/usr/bin/env bash
# setup_mcp_tool_picker_demo.sh
#
# MCP Tool Picker — the agent picks WHICH MCP tools to call.
#
# The MCP-flavored version of Supervisor Agent. Same overall shape:
# a planner LLM picks tools + args to invoke from a bounded set. The
# difference: each tool is a REAL MCP call (not an LLM persona).
#
# Pipeline (one YAML block emits all of this):
#   mcp_plan          (planner LLM: picks {tool, args, reason} per invocation)
#         ↓
#   ├── list_dir_result       (real MCP call: list_directory_with_sizes)
#   └── read_head_result      (real MCP call: read_text_file)
#         ↓
#   mcp_final_answer  (synthesizer LLM: grounded final answer w/ MCP citations)
#
# MCP server: @modelcontextprotocol/server-filesystem, launched via npx
# in stdio mode. Reads from /tmp/mcp_picker_sandbox (created by the setup
# script and pre-seeded with a few files of varying sizes).
#
# COST: ~$0.01 per run (planner + synthesis on gpt-4o-mini; MCP calls are free)
#
# REQUIREMENTS
#   • uv, node/npx, OPENAI_API_KEY
#
# USAGE
#   export OPENAI_API_KEY=sk-...
#   ./setup_mcp_tool_picker_demo.sh                # → mcp_tool_picker_demo/

set -eo pipefail

PROJECT_NAME="${1:-mcp_tool_picker_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
SANDBOX_DIR="${BASE_DIR}/${PROJECT_NAME}_sandbox"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
command -v npx >/dev/null 2>&1 || fail "npx not found (install Node.js)."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Building sandbox directory: $SANDBOX_DIR"
rm -rf "$SANDBOX_DIR" && mkdir -p "$SANDBOX_DIR"
# Seed a few .py files of varying sizes so "the largest .py" is unambiguous
cat > "$SANDBOX_DIR/tiny.py" <<'PY'
print("hello")
PY
cat > "$SANDBOX_DIR/medium.py" <<'PY'
"""A slightly-larger example module."""
def add(a, b): return a + b
def sub(a, b): return a - b
def mul(a, b): return a * b
def div(a, b): return a / b if b else None
if __name__ == "__main__":
    print(add(2, 3), sub(10, 4), mul(6, 7), div(100, 25))
PY
cat > "$SANDBOX_DIR/big.py" <<'PY'
"""Largest example module — has several classes + docs + main."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Point:
    """A 2D point."""
    x: float
    y: float

    def distance_to(self, other: "Point") -> float:
        dx, dy = self.x - other.x, self.y - other.y
        return (dx * dx + dy * dy) ** 0.5


@dataclass
class Circle:
    """A circle at a given center + radius."""
    center: Point
    radius: float

    @property
    def area(self) -> float:
        return 3.141592653589793 * self.radius * self.radius


def demo() -> None:
    p1 = Point(0, 0)
    p2 = Point(3, 4)
    c = Circle(center=p1, radius=5)
    print(f"distance = {p1.distance_to(p2)}")
    print(f"circle area = {c.area}")


if __name__ == "__main__":
    demo()
PY
cat > "$SANDBOX_DIR/notes.txt" <<'TXT'
Not a Python file — should NOT be picked by the "largest .py" question.
TXT
ok "Sandbox seeded ($(ls "$SANDBOX_DIR" | wc -l | tr -d ' ') files)"

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    'pandas>=1.5.0' 'tabulate>=0.9.0' 'openai>=1.0.0' 'mcp>=1.0.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'pandas>=1.5.0' 'tabulate>=0.9.0' 'openai>=1.0.0' 'mcp>=1.0.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

mkdir -p "$PROJECT_DIR/.dagster_storage"
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
from pathlib import Path
from dagster import definitions, load_from_defs_folder, FilesystemIOManager

@definitions
def defs():
    root = Path(__file__).resolve().parent.parent.parent
    storage = root / ".dagster_storage"; storage.mkdir(exist_ok=True)
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={"io_manager": FilesystemIOManager(base_dir=str(storage))},
    )
PY

mkdir -p "src/${PROJECT_NAME}/defs/mcp_picker"

cat > "src/${PROJECT_NAME}/defs/mcp_picker/defs.yaml" <<YAML
type: dagster_community_components.MCPToolPickerComponent
attributes:
  plan_asset_name: mcp_plan
  synthesis_asset_name: mcp_final_answer
  task: |
    Look at the directory ${SANDBOX_DIR}.
    First, list all files with their sizes.
    Second, read the file named notes.txt and report its contents.
    Then summarize: what files are in the directory, and what does
    notes.txt say?
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.1
  tools:
    - name: list_dir
      description: "List files with sizes in a directory (use this first to find the largest .py)."
      args_schema_hint: '{path: string}'
      server:
        name: fs
        type: stdio
        command:
          - npx
          - -y
          - "@modelcontextprotocol/server-filesystem"
          - ${SANDBOX_DIR}
      mcp_tool_name: list_directory_with_sizes

    - name: read_file
      description: "Read a text file's full contents by path (use only for a file you know exists)."
      args_schema_hint: '{path: string}'
      server:
        name: fs
        type: stdio
        command:
          - npx
          - -y
          - "@modelcontextprotocol/server-filesystem"
          - ${SANDBOX_DIR}
      mcp_tool_name: read_text_file

  group_name: mcp_picker_demo
YAML

ok "Wrote defs.yaml (MCPToolPickerComponent — 2 MCP tools)"

DM="${PROJECT_NAME}.definitions"

info "Planner picking MCP calls (gpt-4o-mini)…"
uv run dagster asset materialize --select mcp_plan -m "$DM" 2>&1 | tail -3 || fail "plan failed"

info "Executing MCP tool assets…"
uv run dagster asset materialize --select 'mcp_plan+' -m "$DM" 2>&1 | tail -3 || fail "tools failed"

info "Synthesizing final answer…"
uv run dagster asset materialize --select mcp_final_answer -m "$DM" 2>&1 | tail -3 || fail "synthesis failed"

echo
ok "Demo complete."
echo
cat <<EOF
The MCP tool-picker pattern just ran end-to-end:
  1. Planner LLM saw the task + the 2 declared MCP tools + their arg schemas
  2. Planner picked which tool(s) to invoke with what args
  3. Each pick actually called the MCP filesystem server (npx-spawned)
  4. Synthesizer LLM wrote the final answer with MCP-tool citations

Inspect the audit trail:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: mcp_plan → list_dir_result + read_file_result → mcp_final_answer
    → click mcp_plan to see the picks + reasons + JSON args
    → click each *_result asset to see the raw MCP response
    → click mcp_final_answer to see the synthesized answer

Sandbox files (real .py + .txt seeded for the demo):
  $SANDBOX_DIR/
    tiny.py     (~14 bytes)
    medium.py   (~200 bytes)
    big.py      (~700 bytes)  ← the largest .py
    notes.txt   (should be excluded — not .py)

In prod, swap the filesystem MCP server for real ones:
  • mcp-server-postgres → SQL access with planner-picked queries
  • mcp-server-github    → issues/PRs/repos
  • mcp-server-slack     → channel/DM interactions
  • Dagster+'s own MCP server → runs/assets/materializations
EOF
