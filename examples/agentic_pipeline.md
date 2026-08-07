# Agentic Pipeline (AgenticPipelineComponent)

**One YAML file. Whole 5-step agentic workflow. Every step is a first-class Dagster asset with typed metadata.**

The sibling of `ml_pipeline` for the LLM domain. `source: + steps: + outputs:` shape you already know from `polars_pipeline` / `warehouse_pipeline` / `pyspark_pipeline` / `ml_pipeline`. Five ops in v1: `llm_call`, `route`, `debate`, `critique_loop`, `synthesize`.

**Setup script:** [`setup_agentic_pipeline_demo.sh`](./setup_agentic_pipeline_demo.sh) — scaffolds a full Dagster project + installs the component + writes a `defs.yaml` running all 5 ops end-to-end. `bash setup_agentic_pipeline_demo.sh` and `uv run dg dev`.

## What the demo shows

One YAML declares a **5-step research bot**:

1. **`baseline`** (`op: llm_call`) — plain single completion. The control.
2. **`routed`** (`op: route`) — router LLM picks between a `technical` and `general` specialist; specialist answers.
3. **`refined`** (`op: critique_loop`) — drafter writes; critic reviews; drafter revises.
4. **`debated`** (`op: debate`) — two proposers rewrite in different styles (accessible vs. rigorous); arbitrator picks the winner.
5. **`final`** (`op: synthesize`) — merges all four upstream artifacts into one polished response.

Total cost per full run: **~$0.001** (all `gpt-4o-mini`, ~10 LLM calls). Ideal for CI + iteration.

## Why Dagster (not just a job runner)

Any tool can execute a chain of LLM calls. Prefect can. A bash script can. What Dagster does that they don't:

### 1. Every step is a versioned asset — browsable, not log-grepable

Click `research_bot_debated` in the catalog. See **every prior materialization** with:

| Metadata (all shown inline) | Type |
|---|---|
| `debated__text` | Markdown — the winning proposal, rendered |
| `debated__cost_usd` | Float — this step's total cost |
| `debated__latency_ms` | Int — how long it took |
| `debated__tokens_total` | Int — total tokens consumed |
| `debated__n_llm_calls` | Int — 3 (2 proposers + 1 arbitrator) |
| `debated__model_fingerprint` | Text — `[gpt-4o-mini,gpt-4o-mini]→gpt-4o-mini` |
| `debated__materialized_at` | Timestamp — when it fired |
| `debated__op` | Text — `debate` |
| `debated__arbitrator_reasoning` | Text — "Proposal 1 is more precise because..." |
| `debated__proposals` | JSON — every proposal in full, model per proposal |

In Prefect, all of this is a `run.log` you `grep`.

### 2. Numeric metadata → Dagster+ Insights automatically

Because `cost_usd`, `latency_ms`, `tokens_total`, `n_llm_calls` are typed **numeric** metadata (`FloatMetadataValue` / `IntMetadataValue`), Dagster+ Insights automatically turns them into:

- **Custom metrics** dashboards — plot median pipeline cost over time, per-step latency histograms.
- **Alerts** — "alert if any pipeline's cost exceeds $0.10 per partition" in the UI, no code.

In Prefect, that's a manual export → Grafana → alertmanager pipeline you build.

### 3. Per-op kinds — filter the whole catalog by pipeline op

Every asset gets its op as a `kind` tag. `research_bot_debated` has kinds `[llm, agent, debate]`. `research_bot_routed` has `[llm, agent, route]`.

Now `dg list defs --kinds debate` shows every debate step across every pipeline in your org — impossible without an assets graph.

### 4. Time-travel to any partition

`{partition_key}` in your source text / URL / file path templates at compute time. Add partitions via `post_processing:`:

```yaml
post_processing:
  assets:
    - target: "*"
      attributes:
        partitions_def: {type: daily, start_date: "2026-01-01"}
```

Now every day's materialization is independently browsable. "What did the router pick for `2026-03-05`?" — one click on the partition, no log-search.

### 5. Lineage — pipeline connects to your data graph

Change `source: {kind: literal, ...}` to `source: {kind: upstream_asset, upstream_asset_key: my_data}` and the pipeline shows up as a downstream node of your existing data asset. Prefect flows have no such graph.

## Where the sinks land

The demo writes three files to `<project>/out/`:

- **`final_answer.txt`** — the final synthesized answer (from `text_sinks`)
- **`debate_transcript.json`** — full debate step dict: all proposals + arbitrator reasoning + cost + latency + winner index (from `json_sinks`)
- **`critique_history.json`** — full critique step dict: drafter/critic iteration history + models + cost (from `json_sinks`)

All paths are `{partition_key}`-templated when the pipeline is partitioned.

## When to reach for this vs. the narrow AI components

| You want... | Reach for |
|---|---|
| One LLM call per row of a DataFrame | `litellm_inference_asset` / `openai_llm` |
| An agent with MCP tools (single-shot ReAct) | `litellm_agent` / `openai_agent` |
| Planner picks N tools in parallel → synthesize | `supervisor_agent` |
| ReAct chain with per-step assets | `iterative_supervisor_agent` |
| Multi-output branching per case | `llm_multi_path_router` |
| **Compose several of the above shapes into one pipeline** | **`agentic_pipeline` (this)** |

## The op catalog (v1 = 5)

| op | Signature | Output shape |
|---|---|---|
| `llm_call` | one LLM call over source text | `{text, model, model_fingerprint, cost_usd, latency_ms, tokens_total, n_llm_calls=1, materialized_at, op="llm_call", usage, prompt}` |
| `route` | router picks specialist; specialist answers | `{text, selected_specialist, router_reasoning, routing_source, router_model, specialist_model, model_fingerprint, cost_usd, latency_ms, tokens_total, n_llm_calls=2, materialized_at, op="route", usage}` |
| `debate` | N proposers → arbitrator picks | `{text, winner_index, winner_model, arbitrator_reasoning, all_proposals, arbitrator_model, model_fingerprint, cost_usd, latency_ms, tokens_total, n_llm_calls=N+1, materialized_at, op="debate", usage}` |
| `critique_loop` | drafter → critic → drafter, N iterations | `{text, iterations_done, history, drafter_model, critic_model, model_fingerprint, cost_usd, latency_ms, tokens_total, n_llm_calls=1+2N, materialized_at, op="critique_loop", usage}` |
| `synthesize` | merge N upstream step texts (fan-in) | `{text, sources_used, model, model_fingerprint, cost_usd, latency_ms, tokens_total, n_llm_calls=1, materialized_at, op="synthesize", usage}` |

## Requirements

- `uv` (for `uvx create-dagster` scaffold)
- `OPENAI_API_KEY` env var (get one at https://platform.openai.com/api-keys)
- Deps installed by the setup script: `litellm`, `requests`
