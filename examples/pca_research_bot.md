# PCA-authored research bot — same 5-op pipeline, authored by an LLM planner

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. Plan cached to
> Dagster's state store; no local file writes.

**Components:** `PlannedCatalogAgentComponent` (planner) + `AgenticPipelineComponent` (emitted at plan time).
**Setup script:** [`setup_pca_research_bot_demo.sh`](./setup_pca_research_bot_demo.sh).
**Cost:** ~$0.0007 for the one-time planner trajectory + ~$0.005 per materialize.

Same shape as [`agentic_pipeline.md`](./agentic_pipeline.md) — 5-step
research chain (baseline → critique → fan-out into accessible + precise
rewrites → synthesize final answer). Only difference: the plan is
**authored by an LLM planner at prepare time** instead of hand-typed
YAML. The prompt IS the artifact.

## Two authoring paths, one substrate

| | Hand-authored (`hand_research_bot`) | PCA-authored (this) |
|---|---|---|
| What lives in git | The full `AgenticPipelineComponent` defs.yaml | A `task:` field on a `PlannedCatalogAgentComponent` YAML |
| Re-plan | Edit the yaml directly, `dg check defs` | Edit the `task:` string + `dg utils refresh-defs-state` |
| Best when | Team wants the plan under source control + PR-reviewed | Team wants "input a task in the Dagster+ UI, real assets appear" UX |
| Assets emitted | Same 5 asset keys either way | Same 5 asset keys either way |

Both produce the same 5 assets:
`research_bot_baseline`, `research_bot_critique`,
`research_bot_rewrite_accessible`, `research_bot_rewrite_precise`,
`research_bot_final`.

## Architecture (what PCA emits)

```
         ┌───────────────────────────────────────────────────────┐
         │  PlannedCatalogAgentComponent                         │
         │    task: "5-step research chain: baseline → critique  │
         │           → 2 rewrites → synthesize final..."         │
         │                                                       │
         │    planner LLM (gpt-4o-mini) picks agentic_pipeline   │
         │    from ~960-component community registry, writes     │
         │    the full config to Dagster's state store           │
         │                                                       │
         │    every subsequent load → REAL Dagster assets from   │
         │    cached plan, ZERO LLM cost per run                 │
         └────────────────────────────┬──────────────────────────┘
                                      │  emits
                                      ▼
       ┌────────────────────────────────────────────────────────┐
       │  research_bot_baseline    (llm_call over source)       │
       └────────────────────┬───────────────────────────────────┘
                            │
                            ▼
       ┌────────────────────────────────────────────────────────┐
       │  research_bot_critique    (llm_call, source: baseline) │
       └────────────────────────────────────────────────────────┘

       ┌────────────────────────────────────────────────────────┐
       │  research_bot_rewrite_accessible  (llm_call, source: baseline)
       │  research_bot_rewrite_precise     (llm_call, source: baseline)
       │  — both fan out from `baseline`
       └────────────────────┬───────────────────────────────────┘
                            │
                            ▼
       ┌────────────────────────────────────────────────────────┐
       │  research_bot_final                                    │
       │   (synthesize — sources: [baseline, critique,          │
       │                           rewrite_accessible,          │
       │                           rewrite_precise])            │
       │   → text_sink /tmp/pca_research_final.txt              │
       └────────────────────────────────────────────────────────┘
```

## The prompt (THE demo)

The whole demo is this `task:` field. Everything else is scaffolding.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Emit ONE agentic_pipeline component with this shape (5 steps, 1 asset per step):

    - asset_name_prefix: research_bot
    - source: {kind: literal, text: "Explain how transformer attention works"}
    - steps:
        - id: baseline           op: llm_call        source: source
        - id: critique           op: llm_call        source: baseline
        - id: rewrite_accessible op: llm_call        source: baseline
        - id: rewrite_precise    op: llm_call        source: baseline
        - id: final              op: synthesize      sources: [baseline, critique, rewrite_accessible, rewrite_precise]

    Use gpt-4o-mini for llm_call, gpt-4o for the synthesize.
    outputs.assets: [baseline, critique, rewrite_accessible, rewrite_precise, final]
    text_sinks: [{from: final, path: /tmp/pca_research_final.txt}]

  include_ids: [agentic_pipeline]
  task_hints:
    - "Emit exactly ONE agentic_pipeline component instance."
    - "The 5 steps chain by `source: <step_id>`. `synthesize` uses `sources: [<step_ids>]` for its fan-in."
```

**Why this shape works:** PCA is at its best when the task tells it
*exactly* which component to pick (`include_ids: [agentic_pipeline]`)
and roughly which shape to build. The planner then figures out the exact
Pydantic field names, wires per-step models + api key env vars, and
emits a validating config in one iteration (~27s trajectory).

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pca_research_bot_demo.sh \
  -o setup_pca_research_bot_demo.sh
bash setup_pca_research_bot_demo.sh
```

Then:

```bash
cd pca-research-bot-demo
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg dev
# UI at http://localhost:3000 — all 5 assets appear as real Dagster assets
```

## What's in the state store (the cached plan)

After `dg utils refresh-defs-state` runs, look at
`src/pca_research_bot_demo/defs/.local_defs_state/PlannedCatalogAgent__<hash>__/state`.
That JSON is the full `AgenticPipelineComponent` config the planner
wrote. Every subsequent Dagster load reads this file — no LLM call.
`refresh-defs-state` again to re-plan.

## Re-plan against a new prompt

Change the `task:` string in `defs.yaml` — say, swap "transformer
attention" for "how retrieval-augmented generation improves LLM
factuality" — and run:

```bash
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg utils refresh-defs-state
```

State store is keyed by the task hash, so different tasks get different
cached plans. Perfect for the "swap tasks per environment / team" flow.

## Related patterns

- [**Agentic Pipeline (hand-authored)**](./agentic_pipeline.md) — same
  5 assets, written directly as YAML instead of authored by PCA.
- [**Planned Catalog Agent (deep dive)**](./planned_catalog_agent.md) —
  the parent walkthrough for the state-backed catalog-agent pattern.
- [**PCA-authored investment memo**](./pca_investment_memo.md) — same
  authoring path applied to the debate pattern.
- [**PCA-authored maintainer**](./pca_maintainer.md) — the harder case
  (`mcp_call` + typed inputs); shows the two PCA enhancements needed to
  make it work (`loop_guard_max_failures` + `steps_schemas` in
  `agent_hints`).
