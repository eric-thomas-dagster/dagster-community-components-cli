# MCP Tool Picker — the agent picks *which MCPs to call*

**Component (new):** `MCPToolPickerComponent` — one YAML block emits `plan + N MCP-tool assets + synthesis`.

**Script:** [`setup_mcp_tool_picker_demo.sh`](./setup_mcp_tool_picker_demo.sh)
**Cost:** ~$0.01 per run (planner + synthesizer on gpt-4o-mini; MCP calls are free — filesystem server)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end. Planner picked both declared MCP tools with correct args (`list_dir` on sandbox path + `read_file` on `notes.txt`), both MCP calls returned real filesystem data, synthesizer wrote a grounded summary citing the tools.

## Why this exists

Everyone asks: *"can the agent decide which MCP servers/tools to call?"* Yes. This is the MCP-flavored companion to [Supervisor Agent](./supervisor_agent.md) — same overall shape (planner + fan-out + synthesizer), but tools are **real MCP tool calls** instead of LLM personas.

```
mcp_plan             (planner LLM: picks {tool, args, reason} per invocation)
       ↓
├── list_dir_result       (real MCP call: list_directory_with_sizes)
└── read_file_result      (real MCP call: read_text_file)
       ↓
mcp_final_answer     (synthesizer LLM: grounded final answer w/ MCP citations)
```

Each per-tool asset actually spins up an `mcp.ClientSession`, calls the picked tool with the planner's args, parses the response, and materializes it as an asset. The planner picks BY NAME from a YAML-declared bounded set — it cannot invent servers or tool names.

## Why this shape is safe

- **Bounded.** The tool list is fixed in YAML at pipeline-write time. Each tool declares which MCP server it's on. Planner picks BY NAME.
- **Real MCP.** Not simulated. Every pick becomes a genuine MCP session — stdio subprocess, HTTP endpoint, or SSE transport.
- **Auditable.** Every planner pick + args + reason lands on the `mcp_plan` asset. Every MCP call's result is its own asset. Full lineage.
- **Composable.** Gate on asset checks. Restrict which servers appear via YAML. Swap MCP servers per-environment (dev fs sandbox vs prod GitHub) with a config change.

## Prerequisites

- `uv` + `node` (for `npx`) + `OPENAI_API_KEY`
- The demo uses `@modelcontextprotocol/server-filesystem` launched via `npx` — Node.js needs to be available.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mcp_tool_picker_demo.sh -o setup_mcp_tool_picker_demo.sh
chmod +x setup_mcp_tool_picker_demo.sh
./setup_mcp_tool_picker_demo.sh
```

The setup script:
1. Seeds a sandbox directory with 4 files (`tiny.py`, `medium.py`, `big.py`, `notes.txt`) at varying sizes.
2. Scaffolds a Dagster project + installs deps (including the `mcp` python client).
3. Writes ONE `defs.yaml` that declares the planner + 2 filesystem MCP tools.
4. Runs planner → per-tool MCP assets → synthesizer end-to-end.

## Validated run output (2026-07-07)

**Planner's picks:**

```
tool       args                                                    reason
list_dir   {"path": "/tmp/mcp_tool_picker_demo_sandbox"}           List all files with sizes.
read_file  {"path": "/tmp/mcp_tool_picker_demo_sandbox/notes.txt"} Read notes.txt as instructed.
```

**Real MCP responses (from the filesystem server via stdio):**

```
list_dir  → [FILE] big.py 796 B / medium.py 252 B / notes.txt 74 B / tiny.py 15 B  (Total 4 files, 1.11 KB)
read_file → "Not a Python file — should NOT be picked by the 'largest .py' question."
```

**Synthesized answer (grounded in the MCP outputs):**

> In the directory `/tmp/mcp_tool_picker_demo_sandbox`, there are four files: `big.py` (796 B), `medium.py` (252 B), `notes.txt` (74 B), and `tiny.py` (15 B). The contents of `notes.txt` state: *"Not a Python file — should NOT be picked by the 'largest .py' question."* The directory contains three Python files and one text file.

## Single-shot planning vs iterative

This component's planner picks all MCP calls **upfront** in a single LLM pass. That works when:

- The args are inferrable from the task alone (paths given, resource names known, etc.)
- Calls are independent — no output of one is needed to formulate the args of another

For **iterative** patterns (planner needs to see one tool's output to formulate the next call — the ReAct loop), pair with `LangGraphAgentComponent` or use the `OpenAIAgentComponent` / `AnthropicAgentComponent` (which do agent loops internally). Or run this component in a schedule where each run refines its own plan based on the previous run's outputs.

The trade-off: single-shot is cheaper, more predictable, and shows a static DAG in `dg dev`. Iterative is more capable but harder to reason about. Pick the pattern that fits the task complexity.

## In production — swap the filesystem server for real ones

The demo uses `@modelcontextprotocol/server-filesystem` because it's universally available (npm). Real MCP servers to consider:

| MCP Server | Use case |
|---|---|
| `mcp-server-postgres` | Warehouse queries — planner picks table + WHERE per task. |
| `mcp-server-github` | Issues / PRs / repo introspection. |
| `mcp-server-slack` | Read channels, post messages. |
| `mcp-server-notion` | Docs QA, page updates. |
| Dagster+'s MCP server | Runs / assets / materializations. Set `type: http`, `url: https://mcp.agent.dagster.cloud/mcp/`, and add auth headers via `headers_env`. |

Each is a config swap — the component doesn't know the difference.

## Extension patterns

- **Multiple servers in one demo.** Nothing prevents each tool from pointing at a different `server`. The planner can pick a Postgres query AND a Slack message post AND a Notion page read in the same run.
- **Human-in-the-loop.** Add an `asset_check` on `mcp_plan` that fails if the planner picked any tool marked as "destructive" — forces manual approval before MCP calls run.
- **Args validation.** Add a `filter` component between `mcp_plan` and each per-tool asset to catch obviously-bad args (wrong types, out-of-range paths). Prevents runtime MCP failures.
- **Retry on isError.** Wire retries via each tool asset's `retry_policy` fields.

## Part of the agent-pipeline patterns family

See [agent_pipeline_patterns.md](./agent_pipeline_patterns.md) — overview of all seven agent-pipeline demos with a selection guide + adjacent-but-not-agentic patterns (`langgraph_agent`, `dbt_llm_pipeline`, `pii_redaction`, `data_quality_agent`, `cube_llm`).

## Related

- [Supervisor Agent](./supervisor_agent.md) — same pattern with LLM-persona tools.
- [Agent + MCP tool loop (agent_family)](./agent_family.md) — tighter agent-loop pattern; one agent, one loop of MCP calls per asset. Different mechanic (agent iteration vs pre-plan).
- [`mcp_tool_call`](../../dagster-component-templates/blob/main/assets/sources/mcp_tool_call/README.md) — one-shot MCP call, no LLM planning. Use when you already know the tool + args.
