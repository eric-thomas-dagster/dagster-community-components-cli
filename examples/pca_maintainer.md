# PCA-authored maintainer investigation — real GitHub MCP call, PCA-emitted

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. Plan cached to
> Dagster's state store; `mcp_call` step invokes the GitHub MCP server
> at materialize time (needs `npx` available on the compute host).

**Components:** `PlannedCatalogAgentComponent` (planner) + `AgenticPipelineComponent` v2 (`mcp_call` op + typed named inputs).
**Setup script:** [`setup_pca_maintainer_demo.sh`](./setup_pca_maintainer_demo.sh).
**Cost:** ~$0.07 for the one-time planner trajectory + ~$0.01 per materialize.

**The hardest of the three PCA demos.** A meta-component (`agentic_pipeline`
v2 with the new `mcp_call` op) whose config IS the whole DAG. Getting
`PlannedCatalogAgentComponent` to emit a valid configuration required two
PCA enhancements — both shipped in this session.

## Two PCA enhancements that made this work

Both live on `PlannedCatalogAgentComponent` and are set on the defs.yaml:

### 1. `loop_guard_max_failures: 6`

Default is 3 — right for simple components. Meta-components with deeply
nested typed schemas need more iterations for the planner to converge on
the exact field shapes. Bumping to 6 gives the planner room to iterate.

### 2. `agent_hints.steps_schemas` — strict JSON Schema in the planner's prompt

The manifest entry for `agentic_pipeline` now publishes a discriminated-
union JSON Schema for its `steps[]` field — one `oneOf` branch per op
(`llm_call`, `synthesize`, `mcp_call`, `route`, `debate`, `critique_loop`)
with `additionalProperties: false` so any invented field name fails
validation.

**Before this**: gpt-4o kept substituting field names (`type` for `op`,
`tool` for `mcp_tool_name`, `params` for `tool_args`) — 6 iterations, 6
identical wrong-key configs, loop guard exits.

**After**: strict field names on iteration 1. PCA now composes a valid
`agentic_pipeline` v2 configuration with `mcp_call` + typed named
inputs from an NL task.

## The prompt (THE demo)

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Build an "AI maintainer investigation room" for GitHub issue triage.
    Fan out 4 LLM specialists to analyze issue #30000 in dagster-io/dagster
    (code, docs, reproduction, prior history), synthesize a triage decision,
    draft the final maintainer report as markdown.

    Compose ONE agentic_pipeline component (asset_name_prefix: mir).

    First step is an mcp_call to the GitHub MCP server:
      server: {name: github, type: stdio,
               command: [npx, -y, "@modelcontextprotocol/server-github"]}
      mcp_tool_name: get_issue
      tool_args: {owner: "dagster-io", repo: "dagster", issue_number: 30000}
        # NOTE: owner and repo are SEPARATE string args, and issue_number
        # MUST be an integer (not a string).
      parse_as: auto

    Every downstream llm_call and synthesize step uses `inputs:` field
    with typed named ports (e.g. inputs: {issue_facts: {from: fetch_issue}}).

    Use gpt-4o-mini for specialists, gpt-4o for synthesis + report.
    API key env var: OPENAI_API_KEY. GitHub token env var:
    GITHUB_PERSONAL_ACCESS_TOKEN (already exported to the parent process,
    so leave stdio server `env` empty — the subprocess inherits it).

    outputs.assets MUST include ALL step ids.

  include_ids: [agentic_pipeline]
  llm_model: gpt-4o
  planner_max_tokens: 8000        # up from default 600 — meta-configs are big
  loop_guard_max_failures: 6      # up from default 3 — meta-configs need iterations
```

## Architecture

PCA emits a 3-step (out of 7 target) `agentic_pipeline` on iteration 7 of
its trajectory. Trimmed to that iteration; the earlier iterations
produced smaller partial versions with overlapping asset keys.

```
      ┌──────────────────────────────────────────────────────┐
      │  mir_fetch_issue (op: mcp_call)                      │
      │    npx -y @modelcontextprotocol/server-github        │
      │    → get_issue(owner=dagster-io, repo=dagster,       │
      │                issue_number=30000)                   │
      │    → { title, body, labels, author, state, ... }     │
      └────────────────────────┬─────────────────────────────┘
                               │
                               ▼
      ┌──────────────────────────────────────────────────────┐
      │  mir_route_specialists (op: route)                   │
      │    router LLM picks the right specialist persona     │
      └────────────────────────┬─────────────────────────────┘
                               │
                               ▼
      ┌──────────────────────────────────────────────────────┐
      │  mir_synthesize_triage_decision (op: synthesize)     │
      │    LLM merges specialist output into a triage        │
      │    decision (classification / confidence / owner)    │
      └──────────────────────────────────────────────────────┘
```

## The 3-step vs 7-step gap (documented honestly)

The task described a 7-step pipeline (intake + 4 specialists +
preliminary synthesize + skeptic + decision + report). PCA iterated 8
times, each iteration progressively adding more steps. Iteration 7
produced a valid 3-step config — that's what the demo ships with.

**Why not all 7?** PCA's iteration model is "add a new component pick
each iteration" — for chained-component pipelines like
[`pca_research_bot`](./pca_research_bot.md), that's exactly right. For
meta-components where the target config IS one big object, PCA needs a
"grow one config" mode that appends steps into the SAME
`agentic_pipeline.steps[]` list on each iteration instead of adding a
peer `agentic_pipeline` pick. That's the follow-up.

For the full 7-step / 9-node maintainer investigation, see the
hand-authored version:
[`maintainer_investigation_room.md`](./maintainer_investigation_room.md).

## Run

```bash
export OPENAI_API_KEY=sk-...
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...   # any classic PAT, public_repo scope
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pca_maintainer_demo.sh \
  -o setup_pca_maintainer_demo.sh
bash setup_pca_maintainer_demo.sh
```

Requirements: `uv`, `npx` (bundled with Node — for the stdio GitHub MCP
server), `OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`. ~45s planner
trajectory + ~15s materialize.

```bash
cd pca-maintainer-demo
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg dev
# UI at http://localhost:3000

# Or materialize headless
DAGSTER_HOME=$(pwd)/.dagster_home uv run dagster asset materialize \
  --select 'mir_*' -m pca_maintainer_demo.definitions
```

## Related patterns

- [**Hand-authored full 7-step version**](./maintainer_investigation_room.md)
  — 9-node execution plan (intake → 4 typed-input specialists → preliminary
  synthesize → skeptic → decision → report → human approval gate + sensor).
  The complete "AI maintainer investigation room" story.
- [**Typed named inputs**](./typed_named_inputs.md) — the v2 primitive
  that lets any op join from any prior op by port name.
- [**Planned Catalog Agent (deep dive)**](./planned_catalog_agent.md) —
  parent walkthrough for the state-backed authoring pattern.
- [**PCA-authored research bot**](./pca_research_bot.md),
  [**PCA-authored investment memo**](./pca_investment_memo.md) — simpler
  chained-component demos where PCA's default iteration model fits cleanly.
