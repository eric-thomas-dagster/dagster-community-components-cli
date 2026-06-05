# LiteLLM Agent with MCP — demo

Single-shot LLM agent that uses [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers as its tool layer. The demo points the agent at the official `@modelcontextprotocol/server-filesystem` server, asks it a question about a directory, and confirms the trajectory + final answer.

```
litellm_agent  (single asset)
       │
       ├─ on materialize: starts the MCP filesystem server as a subprocess
       ├─ discovers 14 tools (read_file, write_file, list_directory, …)
       ├─ asks the LLM the configured prompt with all tool defs attached
       ├─ loop: model emits tool_call → MCP runs it → result → next iteration
       └─ stops when the model returns plain text (no more tool calls)
```

## Run the demo

```bash
export OPENAI_API_KEY=sk-…
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_litellm_agent_demo.sh | bash
```

The script will:

1. Create a sandbox directory at `/tmp/litellm-agent-demo-fs` with four files of varying size (so the agent has something to find).
2. Scaffold a Dagster project + install the `litellm_agent` component via `dagster-component add`.
3. Write a `defs.yaml` that points the agent at the sandbox via `@modelcontextprotocol/server-filesystem` (launched via `npx`).
4. Materialize the agent (`dg launch --assets filesystem_agent_run`).
5. Load the pickled output and print the trajectory: tool calls, their args, and the final answer.

## What the agent does

The default prompt is *"List the three largest files in `/tmp/litellm-agent-demo-fs` by size. Return them as a JSON list of `{path, size_bytes}` entries."*

Typical successful run with `gpt-4o-mini`:

| iter | Tool call | Result |
|---|---|---|
| 1 | `fs__list_directory_with_sizes(path='/tmp/litellm-agent-demo-fs', sortBy='size')` | Returns the sorted directory listing |
| 2 | (no tool call — model returns the JSON answer) | `[{"path": ".../big_dataset.bin", "size_bytes": 2097152}, …]` |

If the agent hits a recoverable tool error (e.g. permission denied because the model passed `/tmp` instead of `/private/tmp` on Mac), it self-corrects on the next iteration. The MCP error message lands in the message stream as a `role: tool` content block, and the model re-plans.

## Configuration knobs

- **`model`**: any LiteLLM-supported model. `gpt-4o-mini`, `claude-haiku-4-5-20251001`, `gemini/gemini-2.5-flash`, `groq/llama-3.3-70b-versatile`, `openrouter/anthropic/claude-3.5-sonnet`, … just change `model` + `api_key_env_var`.
- **`max_iterations`**: hard cap on the loop (default 10). Set higher for multi-step chains, lower if you want to force the model to answer in one round.
- **`mcp_servers`**: list of `{name, type, command|url}` entries. `type` is `stdio` (subprocess), `http` (streamable-HTTP — modern transport, what `claude mcp add --transport http` uses), or `sse`. Tool names are auto-prefixed by `name` so multiple servers can be mixed without collisions.
- **Partitions**: `partition_type: daily` + `partition_start: 2026-06-01` make the agent partition-scheduled. `{partition_key}` in the prompt gets templated at materialization time.
- **Freshness / retry / kinds**: standard Dagster knobs supported across all four agent components.

## Validated end-to-end

| Step | Result |
|---|---|
| Materialize `filesystem_agent_run` | `RUN_SUCCESS` in ~10s |
| Tool discovery | `[mcp:fs] discovered 14 tools` |
| Tool calls | 3 (one error recovery + two successful list calls) |
| Final answer | JSON list of the 3 largest files with correct `size_bytes` |
| Materialization metadata | `final_answer` rendered as markdown, `iterations`, `tool_calls_made`, full `tool_calls` JSON |

## Connecting to remote MCP servers (HTTP + auth headers)

The component supports the streamable-HTTP transport with custom headers. The Dagster+ MCP server at `mcp.agent.dagster.cloud/mcp/` is a real-world target — it exposes 34 tools for runs, assets, deployments, alerts, issues, and metrics.

```yaml
type: dagster_component_templates.LiteLLMAgentComponent
attributes:
  asset_name: dagster_plus_daily_summary
  prompt: "Summarize what happened on {partition_key} in the prod deployment in 3 sentences."
  system_prompt: "You are a Dagster Cloud operator. Today's analysis is for {partition_key}."
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  max_iterations: 6
  mcp_servers:
    - name: dgp
      type: http
      url: https://mcp.agent.dagster.cloud/mcp/
      headers:
        Dagster-Cloud-Organization: my-org
      headers_env:
        Authorization: DAGSTER_PLUS_BEARER     # env var must hold "Bearer <token>"
  partition_type: daily
  partition_start: "2026-06-01"
  group_name: ai
```

Live-validated against `mcp.agent.dagster.cloud` partitioned daily — 3 iterations, 4 tool calls including parallel `list_runs` / `list_issues` / `list_alert_policies` on iteration 2.

## Output dict shape

The asset returns a Python `dict`:

```python
{
    "final_answer": "The three largest files in /private/tmp/... ",
    "iterations": 4,
    "tool_calls_made": 3,
    "tool_call_details": [
        {"iteration": 1, "tool": "fs__list_directory_with_sizes",
         "args": {"path": "/tmp", "sortBy": "size"},
         "result_preview": "Access denied - path outside allowed directories: /tmp not in /private/tmp",
         "is_error": True},
        {"iteration": 2, "tool": "fs__list_allowed_directories", "args": {},
         "result_preview": "Allowed directories: /private/tmp", "is_error": False},
        {"iteration": 3, "tool": "fs__list_directory_with_sizes",
         "args": {"path": "/private/tmp", "sortBy": "size"},
         "result_preview": "[FILE] big_dataset.bin    2.00 MB ...", "is_error": False},
    ],
    "transcript": [...],   # full OpenAI-shaped message history
    "stopped_reason": "final_answer",
    "model": "gpt-4o-mini",
    "mcp_servers": ["fs"],
}
```

Downstream assets can consume this directly: `def parse_agent_answer(filesystem_agent_run: dict) -> pd.DataFrame: ...`

## Other MCP servers worth trying

The MCP ecosystem has many no-/low-auth servers you can wire in by changing the `command`:

```yaml
mcp_servers:
  - name: git
    type: stdio
    command: [uvx, mcp-server-git, --repository, /path/to/repo]

  - name: sqlite
    type: stdio
    command: [uvx, mcp-server-sqlite, --db-path, /path/to/db.sqlite]

  - name: time
    type: stdio
    command: [uvx, mcp-server-time]

  - name: brave
    type: stdio
    command: [npx, -y, "@modelcontextprotocol/server-brave-search"]
    env: { BRAVE_API_KEY: "…" }
```

Full registry: [github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers).

## When NOT to reach for `litellm_agent`

| Use case | Right component |
|---|---|
| Per-row DataFrame enrichment via chat completion | `litellm_inference_asset` (or vendor natives `openai_llm`, `anthropic_llm`, `gemini_llm`) |
| Per-row structured-output extraction (Pydantic schema) | `litellm_structured_output` |
| Per-row single tool/function call (no loop) | `litellm_function_calling` |
| Deterministic single-shot MCP tool call (no LLM) | `mcp_tool_call` |
| Embed a corpus + retrieve + generate | `rag_pipeline` |
| Vendor-only agent (skip the LiteLLM dep) | `openai_agent` / `anthropic_agent` / `gemini_agent` |
| Run an agent **per row** of a DataFrame | not yet — could extend `litellm_agent` with an `upstream_asset_key` shape later |
