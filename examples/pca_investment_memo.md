# PCA-authored investment memo — same 3-analyst debate, authored by an LLM planner

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. Plan cached to
> Dagster's state store.

**Components:** `PlannedCatalogAgentComponent` (planner) + `AgenticPipelineComponent` (emitted).
**Setup script:** [`setup_pca_investment_memo_demo.sh`](./setup_pca_investment_memo_demo.sh).
**Cost:** ~$0.0006 for the one-time planner trajectory + ~$0.001 per materialize.

The `debate` pattern from [`agentic_debate.md`](./agentic_debate.md) —
3 analysts (bull / bear / neutral) + arbitrator — **authored by an LLM
planner at prepare time** instead of hand-written YAML. The prompt is
the whole demo.

## Architecture

```
       ┌───────────────────────────────────────────────────────┐
       │  PlannedCatalogAgentComponent                         │
       │    task: "One agentic_pipeline with a debate op —     │
       │           3 analyst proposers arguing BUY/SELL/HOLD   │
       │           for NVDA, arbitrator picks the winner..."   │
       │                                                       │
       │  planner LLM writes the full debate config → cached   │
       │  to state store → REAL debate asset emerges           │
       └────────────────────────────┬──────────────────────────┘
                                    │  emits
                                    ▼
       ┌───────────────────────────────────────────────────────┐
       │  investment_memo_recommendation   (op: debate)        │
       │                                                       │
       │  ┌────────┐  ┌────────┐  ┌────────┐                   │
       │  │  bull  │  │  bear  │  │neutral │                   │
       │  │argues  │  │argues  │  │argues  │                   │
       │  │  BUY   │  │  SELL  │  │  HOLD  │                   │
       │  └───┬────┘  └───┬────┘  └───┬────┘                   │
       │      └───────────┼───────────┘                        │
       │                  ▼                                    │
       │       ┌────────────────────┐                          │
       │       │  arbitrator picks  │                          │
       │       │  the winning stance│                          │
       │       └────────────────────┘                          │
       └───────────────────────────────────────────────────────┘
```

## The prompt (THE demo)

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Emit ONE agentic_pipeline component with this shape:

    - asset_name_prefix: investment_memo
    - source: {kind: literal, text: "Investment committee memo for ticker NVDA.
                                     Buy, hold, or sell?"}
    - steps:
        - id: recommendation
          op: debate
          proposers:
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a bull analyst. Argue for BUY."}
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a bear analyst. Argue for SELL."}
            - {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY,
               system_prompt: "You are a neutral analyst. Argue for HOLD."}
          arbitrator:
            model: gpt-4o-mini
            api_key_env_var: OPENAI_API_KEY
            system_prompt: "Pick the recommendation best for a moderate-risk,
                            long-horizon portfolio."
    - outputs: {assets: [recommendation]}

  include_ids: [agentic_pipeline]
  task_hints:
    - "Emit exactly ONE agentic_pipeline component."
    - "Use the `debate` op — 3 proposers (bull / bear / neutral) + 1 arbitrator."
```

## Two authoring paths, same substrate

| | Hand-authored ([`agentic_debate.md`](./agentic_debate.md)) | PCA-authored (this) |
|---|---|---|
| Trigger | Edit YAML, `dg check defs` | Edit `task:` string, `dg utils refresh-defs-state` |
| Partitioning | Ships with 3 tickers (NVDA / TSLA / META) preloaded | Single-ticker default; extend by re-planning with a different task |
| Reviewability in PRs | Full YAML in git — reviewer sees exact structure | Just the NL task in git — reviewer sees intent, the planner fills in the config |
| Best when | Team has settled on the shape and wants source-controlled changes | The task changes per environment / team, or the "UI-authored plan" UX matters |

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_pca_investment_memo_demo.sh \
  -o setup_pca_investment_memo_demo.sh
bash setup_pca_investment_memo_demo.sh
```

```bash
cd pca-investment-memo-demo
DAGSTER_HOME=$(pwd)/.dagster_home uv run dg dev
# UI at http://localhost:3000 — `investment_memo_recommendation` appears
```

## What's in the emitted asset's metadata

Same audit-friendly output as the hand-authored version — arbitrator
reasoning, all 3 proposals, cost, latency. See
[`agentic_debate.md`](./agentic_debate.md#whats-in-the-emitted-assets-metadata)
for the field-by-field breakdown.

## Extending to per-ticker partitions

The hand-authored `agentic_debate` demo ships with 3 ticker partitions.
PCA can be pointed at a similar shape by extending the task:

```yaml
task: |
  Same 3-analyst debate as above, BUT partition the pipeline over
  tickers [NVDA, TSLA, META]. Use `{partition_key}` in the source
  text and declare static partitions in `post_processing`.
```

Re-plan (`dg utils refresh-defs-state`) — the planner emits a
partitioned config the second time around.

## Related patterns

- [**Hand-authored debate**](./agentic_debate.md) — direct YAML, ships
  with per-ticker partitions.
- [**Planned Catalog Agent (deep dive)**](./planned_catalog_agent.md) —
  the parent walkthrough for the state-backed authoring pattern.
- [**PCA-authored research bot**](./pca_research_bot.md) — 5-step chain
  variant.
- [**PCA-authored maintainer**](./pca_maintainer.md) — the mcp_call + typed inputs case.
