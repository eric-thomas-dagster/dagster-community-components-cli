# Agentic Pipeline (AgenticPipelineComponent)

**One YAML file. Whole 5-step agentic workflow. Every step is a first-class Dagster asset with typed metadata — partitioned across 3 distinct questions.**

The sibling of `ml_pipeline` for the LLM domain. `source: + steps: + outputs:` shape you already know from `polars_pipeline` / `warehouse_pipeline` / `pyspark_pipeline` / `ml_pipeline`. Five ops in v1: `llm_call`, `route`, `debate`, `critique_loop`, `synthesize`.

**Setup script:** [`setup_agentic_pipeline_demo.sh`](./setup_agentic_pipeline_demo.sh) — scaffolds a full Dagster project + installs two components (`synthetic_prompt_generator` + `agentic_pipeline`) + wires them via `source: kind: upstream_asset`. All 5 ops run end-to-end, partitioned across 3 topics with a different composed prompt per partition. `bash setup_agentic_pipeline_demo.sh` and `uv run dg dev`.

## What the demo shows

One YAML declares a **5-step research bot**:

1. **`baseline`** (`op: llm_call`) — plain single completion. The control.
2. **`routed`** (`op: route`) — router LLM picks between a `technical` and `general` specialist; specialist answers.
3. **`refined`** (`op: critique_loop`) — drafter writes; critic reviews; drafter revises.
4. **`debated`** (`op: debate`) — two proposers rewrite in different styles (accessible vs. rigorous); arbitrator picks the winner.
5. **`final`** (`op: synthesize`) — merges all four upstream artifacts into one polished response.

Total cost per full run: **~$0.001** (all `gpt-4o-mini`, ~10 LLM calls). Ideal for CI + iteration.

## Why Dagster (not just a job runner)

Any tool can execute a chain of LLM calls. Any workflow engine can. A bash script can. What Dagster does that they don't:

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

In a job-based orchestrator, all of this is a `run.log` you `grep`.

### 2. Numeric metadata → Dagster+ Insights (promote via UI, few clicks)

Because `cost_usd`, `latency_ms`, `tokens_total`, `n_llm_calls` are typed **numeric** metadata (`FloatMetadataValue` / `IntMetadataValue`), you can **promote any of them into a Dagster+ Insights custom metric** — a few clicks in the UI, no code — and then:

- **Custom metrics dashboards** — plot median pipeline cost over time, per-step latency histograms.
- **Alerts** — configured on the custom metric in the UI ("alert if any pipeline's cost exceeds $0.10 per partition").

Without typed metadata on the platform, you'd first instrument the numeric values yourself, then build the whole export → Grafana → alertmanager pipeline before you could even define the alert.

### 3. Per-op kinds — filter the whole catalog by pipeline op

Every asset gets its op as a `kind` tag. `research_bot_debated` has kinds `[llm, agent, debate]`. `research_bot_routed` has `[llm, agent, route]`.

Now `dg list defs --kinds debate` shows every debate step across every pipeline in your org — impossible without an assets graph.

### 4. Time-travel to any partition — the demo actually does this

This demo ships partitioned across 3 topics (`attention`, `transformer_vs_rnn`, `retrieval_augmented_generation`), each with a **different prompt composed systematically** from `SyntheticPromptGeneratorComponent` (Mode D — persona/style/length/format_hint/depth levers, no LLM cost, reproducible per seed). Sinks land in `out/{partition_key}/` — one directory per partition.

**Backfill all 3 partitions** (~$0.003 total):

```bash
uv run dg launch --assets '*' --partition attention
uv run dg launch --assets '*' --partition transformer_vs_rnn
uv run dg launch --assets '*' --partition retrieval_augmented_generation
```

**In the UI:**

- Click `research_bot_debated` → the partitions strip at the top shows all 3 topics.
- Click any one → its materialization metadata: cost, latency, tokens, model fingerprint, arbitrator_reasoning, all_proposals — the full audit trail for **that specific decision on that specific partition**.
- Click `research_prompts` (the upstream) → see the composed prompt for each topic. Iterate on prompts by editing the levers in `prompts/defs.yaml` (persona / style / length / format_hint / depth); no code, no on-disk question files.
- Compare `attention` vs. `transformer_vs_rnn` vs. `retrieval_augmented_generation`. The router may pick differently, cost varies, arbitrator reasoning is fresh per partition.

That's the story you can't get from a job runner: an agent decision is a **first-class, per-partition, browsable artifact**, not a line in a log.

**How the partitioning is declared** — both the upstream prompt asset (via Mode D's `topics:` list) and the pipeline (via `post_processing:` block) use identical static partitions:

```yaml
# defs/prompts/defs.yaml — the upstream prompt asset
type: dagster_community_components.SyntheticPromptGeneratorComponent
attributes:
  asset_name: research_prompts
  topics: [attention, transformer_vs_rnn, retrieval_augmented_generation]
  persona: engineer
  style: technical
  length: medium
  # ... more levers

# defs/agentic_pipeline/defs.yaml — pipeline mirrors the topic set
post_processing:
  assets:
    - target: "*"
      attributes:
        partitions_def:
          type: static
          partition_keys: [attention, transformer_vs_rnn, retrieval_augmented_generation]
```

Dagster wires the partitioned dependency automatically because both sides share the same partition keys. Bump `count_per_topic: 3` in `prompts/defs.yaml` to emit 3 variants per topic (partition keys become `{topic}__v0` / `__v1` / `__v2`) — great for eval sweeps.

### 5. Lineage — pipeline connects to your data graph

This demo already shows it — the pipeline's `source: {kind: upstream_asset, upstream_asset_key: research_prompts}` wires it into the asset graph as a downstream of `research_prompts`. Materialize just the upstream to iterate on prompts; materialize just the pipeline to iterate on prompts / models / steps. Job-based flows have no such graph.

## Where the sinks land (per-partition)

Each partition writes three files under `<project>/out/{partition_key}/`:

```
out/
├── attention/
│   ├── final_answer.txt          # synthesized final response
│   ├── debate_transcript.json    # all proposals + arbitrator reasoning + cost + winner
│   └── critique_history.json     # drafter/critic iteration transcript
├── transformer_vs_rnn/
│   └── ...
└── retrieval_augmented_generation/
    └── ...
```

Every partition is independently browsable — filesystem AND asset catalog.

## Deploying to Dagster+ Serverless

The two `defs.yaml` files use **relative paths** (`out/{partition_key}/...`) and **no on-disk question files** — the `SyntheticPromptGeneratorComponent` composes prompts in-process from YAML levers, so nothing needs to ship except the project code itself. Absolute `$PROJECT_ABS/...` paths would bake local `/tmp/...` into the YAML and hard-fail on the Serverless container.

**One-time deploy:**

```bash
dagster-cloud serverless deploy-python-executable \
  --location-name agentic-pipeline \
  --package-name <your_pkg> \
  --python-version 3.12
```

(See [`session_serverless_deploy_learnings`](https://github.com/eric-thomas-dagster/dagster-community-components-cli) for the five gotchas hit deploying uvx-create-dagster projects to Serverless.)

**What changes on Serverless:**

- **Prompts are YAML config, not disk files** — the `SyntheticPromptGeneratorComponent` composes prompts in-process from `topics:` + levers, so nothing extra needs to ship. Editing prompts is a one-file YAML change.
- **Filesystem sinks are ephemeral** — `out/{partition_key}/...` writes work per-run inside the container but disappear when the run finishes. **Not a problem** — all decision data (cost, arbitrator reasoning, all proposals, critique history) is already durably stored in the asset materialization metadata. That's what you browse post-deploy anyway. The filesystem sinks are convenience for local iteration.
- **For durable sink outputs across runs**, swap `text_sinks` / `json_sinks` for `table_sinks` writing to a warehouse. Same YAML, different sink type. Adds a warehouse resource; every partition's decision lands in an analytics-friendly table.
- **`OPENAI_API_KEY`** — set as a Dagster+ Serverless location env var:
  ```
  dagster-cloud config secrets set --location-name agentic-pipeline OPENAI_API_KEY sk-...
  ```
  Same `api_key_env_var: OPENAI_API_KEY` in the YAML; the deploy pipeline injects it.

**In the Dagster+ UI post-deploy:** every partition's materialization + full metadata (cost, latency, tokens, router reasoning, arbitrator reasoning, critique history) is browsable in the asset catalog. Dagster+ Insights users can **promote the numeric metadata** (`cost_usd`, `latency_ms`, `tokens_total`) into custom metrics from the UI — once promoted, dashboards per pipeline / per step / per partition + alert rules follow.

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
