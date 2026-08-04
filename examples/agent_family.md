# Agent family demo — `mcp_tool_call` + `openai_agent` + `llm_evaluator`
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

End-to-end demo of the three new agent-stack components in one Dagster project, one MCP server, one materialization chain. All against the no-auth `@modelcontextprotocol/server-filesystem` so anyone with an `OPENAI_API_KEY` can run it.

```
                        @modelcontextprotocol/server-filesystem  (one stdio MCP server)
                                              │
        ┌─────────────────────────────────────┼─────────────────────────────────────┐
        ▼                                     ▼                                     │
mcp_tool_call                         openai_agent                                  │
(no LLM — pure tool dispatch)         (gpt-4o-mini + MCP tool loop)                 │
        │                                     │                                     │
        ▼                                     ▼                                     │
deterministic_fs_listing              fs_agent_answer  ────────────────► llm_evaluator
                                      {final_answer,                     (gpt-4o-mini as judge)
                                       transcript,                              │
                                       tool_call_details,                       ▼
                                       iterations, ...}                  fs_agent_eval
                                                                         {evaluations: {
                                                                            answer_relevance: …,
                                                                            helpfulness: …,
                                                                            coherence: …,
                                                                         }}
```

## Components used

- `llm_evaluator`
- `mcp_tool_call`
- `openai_agent`

## Run

```bash
export OPENAI_API_KEY=sk-…
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agent_family_demo.sh | bash
```

The script:

1. Creates a sandbox at `/tmp/agent-family-demo-fs` with four files of known sizes (canonicalized to `/private/tmp/...` on Mac to play nicely with the MCP filesystem server's symlink rejection).
2. Scaffolds a Dagster project, installs `mcp_tool_call` + `openai_agent` + `llm_evaluator` via `dagster-component add`.
3. Wires three `defs.yaml`s — all three components share the same MCP server config block (`stdio` + `npx @modelcontextprotocol/server-filesystem $FS_DIR`).
4. Materializes the whole chain.
5. Pickle-loads each asset and prints the structured output.

Cost: ~$0.0005 total (gpt-4o-mini for both agent + judge).

## Three different shapes of MCP use

| Component | Trajectory | When to reach for it |
|---|---|---|
| **`mcp_tool_call`** | 0 LLM calls. YAML specifies the tool + args; component dispatches once. | Scheduled deterministic queries. "Every morning, fetch yesterday's Dagster+ runs." |
| **`openai_agent`** | 1+ LLM calls. Model picks tools, results feed back, loops until final answer. | When the question requires reasoning or chained tool use. "Find the 3 largest files." |
| **`llm_evaluator`** | 1 LLM call per evaluation. Scores an upstream agent / LLM output on configurable feedbacks. | Quality monitoring + alerting. Daily failure-rate dashboards. |

The MCP server is identical in all three — the value of the family is that you can move work between "deterministic", "agentic", and "evaluated" purely by changing which component an asset uses.

## Typical run output

```
>>> deterministic_fs_listing  (mcp_tool_call — no LLM)
[FILE] big_dataset.bin                   2.00 MB
[FILE] medium_log.bin                  512.00 KB
[FILE] manifest.json                        15 B
[FILE] config.txt                           12 B
Total: 4 files, 0 directories
Combined size: 2.50 MB

>>> fs_agent_answer  (openai_agent — gpt-4o-mini + MCP tool loop)
  iterations: 2  tool_calls: 1  stopped: final_answer
  final_answer:
    The three largest files in `/private/tmp/agent-family-demo-fs` are:
    [{"path": ".../big_dataset.bin",  "size_bytes": 2097152},
     {"path": ".../medium_log.bin",   "size_bytes": 524288},
     {"path": ".../manifest.json",    "size_bytes": 15}]

>>> fs_agent_eval  (llm_evaluator — scores the agent's answer)
  answer_relevance      score=1.00  reason=The answer directly provides the three largest files…
  helpfulness           score=1.00  reason=The response provides a clear and accurate JSON list…
  coherence             score=0.80  reason=The response is clear and logically presents…
```

## Validated end-to-end

| Component | Step | Result |
|---|---|---|
| `mcp_tool_call` | Connect to stdio MCP, call `list_directory_with_sizes` | 14 tools discovered, listing returned |
| `openai_agent` | Run agent loop with same MCP | 2 iterations, 1 tool call, correct byte-accurate JSON |
| `llm_evaluator` | Score the agent's answer on 3 metrics | 1.00 / 1.00 / 0.80, reasoning matches the actual output |
| Full chain | `dg launch --assets '*'` | `RUN_SUCCESS` in ~15s |

## Swap the LLM vendor

Change `openai_agent` → `anthropic_agent` / `gemini_agent` / `litellm_agent` by editing one `defs.yaml`:

```yaml
type: agent_family_demo.components.anthropic_agent.component.AnthropicAgentComponent
attributes:
  asset_name: fs_agent_answer
  prompt: "Find the three largest files…"
  model: claude-haiku-4-5-20251001
  api_key_env_var: ANTHROPIC_API_KEY
  # ...same mcp_servers block...
```

All four agent components share the same MCP server spec, same output dict shape (`{final_answer, iterations, tool_calls_made, tool_call_details, transcript, stopped_reason, model, mcp_servers}`), and same partition/freshness/retry/kinds knobs. `llm_evaluator` downstream of any of them works unchanged.

## Connect to a real remote MCP server (Dagster+, GitHub, etc.)

Swap `type: stdio` for `type: http` + headers:

```yaml
mcp_servers:
  - name: dgp
    type: http
    url: https://mcp.agent.dagster.cloud/mcp/
    headers:
      Dagster-Cloud-Organization: my-org
    headers_env:
      Authorization: DAGSTER_PLUS_BEARER     # env var holds "Bearer <token>"
```

The `dgp` MCP server exposes 34 tools spanning runs, assets, deployments, alerts, issues, and metrics. See [litellm_agent.md](litellm_agent.md) for a Dagster+ walkthrough.

## Teardown

```bash
rm -rf agent-family-demo /private/tmp/agent-family-demo-fs
```

## See also

- [litellm_agent.md](litellm_agent.md) — multi-vendor agent demo + Dagster+ MCP example
- The component READMEs at the registry UI: [mcp_tool_call](https://dagster-component-ui.vercel.app/c/mcp_tool_call), [openai_agent](https://dagster-component-ui.vercel.app/c/openai_agent), [llm_evaluator](https://dagster-component-ui.vercel.app/c/llm_evaluator)
