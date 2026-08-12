# Agentic Pipeline (AgenticPipelineComponent)

**One YAML file. Whole 5-step agentic workflow. Every step is a first-class Dagster asset with typed metadata — partitioned across 3 distinct questions.**

The sibling of `ml_pipeline` for the LLM domain. `source: + steps: + outputs:` shape you already know from `polars_pipeline` / `warehouse_pipeline` / `pyspark_pipeline` / `ml_pipeline`. Five ops in v1: `llm_call`, `route`, `debate`, `critique_loop`, `synthesize`.

**Setup script:** [`setup_agentic_pipeline_demo.sh`](./setup_agentic_pipeline_demo.sh) — scaffolds a full Dagster project + installs the component + writes a `defs.yaml` running all 5 ops end-to-end, partitioned across 3 dates with a different question per partition. `bash setup_agentic_pipeline_demo.sh` and `uv run dg dev`.

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

This demo ships partitioned across 3 dates (`2026-08-05`, `2026-08-06`, `2026-08-07`), each reading a **different question** from `questions/{partition_key}.txt`. Sinks land in `out/{partition_key}/` — one directory per partition.

**Backfill all 3 partitions** (~$0.003 total):

```bash
uv run dg launch --assets '*' --partition 2026-08-05
uv run dg launch --assets '*' --partition 2026-08-06
uv run dg launch --assets '*' --partition 2026-08-07
```

**In the UI:**

- Click `research_bot_debated` → the partitions strip at the top shows all 3 dates.
- Click any one → its materialization metadata: cost, latency, tokens, model fingerprint, arbitrator_reasoning, all_proposals — the full audit trail for **that specific decision on that specific partition**.
- Compare `2026-08-05` (transformer attention question) vs. `2026-08-06` (RNN comparison) vs. `2026-08-07` (RAG question). The router may pick differently, cost varies, arbitrator reasoning is fresh per partition.

That's the story you can't get from a job runner: an agent decision is a **first-class, per-partition, browsable artifact**, not a line in a log.

**How the partitioning is declared** — one `post_processing:` block at the bottom of `defs.yaml`:

```yaml
post_processing:
  assets:
    - target: "*"        # every asset the component emits
      attributes:
        partitions_def:
          type: static
          partition_keys: ["2026-08-05", "2026-08-06", "2026-08-07"]
```

Swap `type: static` for `type: daily` (with `start_date`) if you want a rolling daily backfill window instead. The `{partition_key}` templating in `source.path` + `text_sinks.path` + `json_sinks.path` already works — nothing else changes.

### 5. Lineage — pipeline connects to your data graph

Change `source: {kind: literal, ...}` to `source: {kind: upstream_asset, upstream_asset_key: my_data}` and the pipeline shows up as a downstream node of your existing data asset. Job-based flows have no such graph.

## Where the sinks land (per-partition)

Each partition writes three files under `<project>/out/{partition_key}/`:

```
out/
├── 2026-08-05/
│   ├── final_answer.txt          # synthesized final response
│   ├── debate_transcript.json    # all proposals + arbitrator reasoning + cost + winner
│   └── critique_history.json     # drafter/critic iteration transcript
├── 2026-08-06/
│   └── ...
└── 2026-08-07/
    └── ...
```

Every partition is independently browsable — filesystem AND asset catalog.

## Deploying to Dagster+ Serverless

The `defs.yaml` uses **relative paths** (`questions/{partition_key}.txt`, `out/{partition_key}/...`) so the same file works locally AND on Dagster+ Serverless. Absolute `$PROJECT_ABS/...` paths would bake local `/tmp/...` into the YAML and hard-fail on the Serverless container.

**One-time deploy:**

```bash
dagster-cloud serverless deploy-python-executable \
  --location-name agentic-pipeline \
  --package-name <your_pkg> \
  --python-version 3.12
```

(See [`session_serverless_deploy_learnings`](https://github.com/eric-thomas-dagster/dagster-community-components-cli) for the five gotchas hit deploying uvx-create-dagster projects to Serverless.)

**What changes on Serverless:**

- **Questions ship with the code** — the `questions/*.txt` files bundle into the deploy package. Relative paths resolve inside the container.
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
