# Local AI A/B — "should we go local" as a Dagster pipeline

> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is. For Serverless, local Ollama isn't reachable from the deployment — use OpenAI + Anthropic candidates (still a valid A/B). For Hybrid on a GPU box, add `OLLAMA_URL=http://localhost:11434` as a location env var and the same YAML picks up the local candidate.

**Components:** `InferenceProviderABTestComponent` (candidates) + `ProviderABEvaluatorComponent` (LLM-as-judge quality) + `InferenceCostReportComponent` (aggregate report).
**Setup script:** [`setup_local_ai_ab_demo.sh`](./setup_local_ai_ab_demo.sh).
**Cost:** ~$0.005 per end-to-end run (gpt-4o-mini candidates + gpt-4o judge).

Runs the same triage prompt through 2-3 LLM providers side-by-side; an LLM-as-judge scores them comparatively; a cost report aggregates cost + latency + quality with baseline deltas and projected daily savings. Every step is a first-class Dagster asset with materialization events and Insights-queryable metadata.

## Why this pattern exists

"Should we go local?" is a decision problem, not a benchmarking exercise. The typical failure modes:

- **Vendor benchmark theater.** A model card claims 82% on MMLU. Your workload isn't MMLU.
- **Spreadsheet drift.** A one-off comparison eight months ago said local was 4% worse. Models have improved twice since then.
- **Spec-based decisions.** "Local qwen2.5:14b is probably good enough." No numbers.

The Dagster answer: run the A/B **on your actual production prompts, in the same substrate you run production on, as a persistent asset**. Cost/latency/quality deltas live with the assets, queryable from Insights, updateable via `dagster asset materialize` on a schedule. When the question comes back next quarter, the answer is already in your graph.

## Architecture (3 assets → 1 report)

```
                ┌──────────────────────────────────────┐
                │  InferenceProviderABTestComponent    │
                │  same prompt through N providers     │
                │  (LiteLLM: 250+ providers supported) │
                └──────┬──────┬──────┬─────────────────┘
                       │      │      │
                       ▼      ▼      ▼
                  ┌──────┐ ┌──────┐ ┌──────┐
                  │gpt   │ │claude│ │qwen  │  ← each is an asset with
                  │_4o_  │ │_haiku│ │_local│    cost_usd + latency_ms +
                  │mini  │ │      │ │      │    tokens_* in metadata
                  └──┬───┘ └──┬───┘ └──┬───┘
                     │        │        │
                     └────────┼────────┘
                              ▼
                  ┌───────────────────────────────────┐
                  │  ProviderABEvaluatorComponent     │
                  │  LLM-as-judge, ONE pass across    │
                  │  all candidates → comparable      │
                  │  scores (no judge-drift)          │
                  └───────────┬───────────────────────┘
                              ▼
                  ┌───────────────────────────────────┐
                  │  triage_ab_scored                 │
                  │  scores + winner + delta_vs_base  │
                  │  + optional asset_check           │
                  │    "winner_meets_threshold"       │
                  └───────────┬───────────────────────┘
                              ▼
                  ┌───────────────────────────────────┐
                  │  InferenceCostReportComponent     │
                  │  aggregate: per-provider metrics  │
                  │  + baseline deltas + projected    │
                  │  daily savings + recommendation   │
                  └───────────┬───────────────────────┘
                              ▼
                  ┌───────────────────────────────────┐
                  │  triage_ab_report                 │
                  │  → time-series in Insights when   │
                  │    materialized on a schedule     │
                  └───────────────────────────────────┘
```

## Try it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_local_ai_ab_demo.sh \
  -o setup_local_ai_ab_demo.sh
export OPENAI_API_KEY=sk-...
# optional — more candidates = better story
export ANTHROPIC_API_KEY=sk-ant-...
# ollama daemon must be running on this port
export OLLAMA_URL=http://localhost:11434

bash setup_local_ai_ab_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY` (always). `ANTHROPIC_API_KEY` optional. `OLLAMA_URL` optional (start ollama first: `ollama serve && ollama pull qwen2.5:14b`). ~30s first run.

## The `triage_ab_report` output

Open the report asset in `dg dev` → Materializations. The markdown comparison table looks like:

```
| provider     | cost      | latency | tokens | quality | Δ cost   | Δ quality |
|--------------|-----------|---------|--------|---------|----------|-----------|
| gpt_4o_mini⭐| $0.0012   | 850ms   | 340    | 82      | —        | —         |
| claude_haiku | $0.0038   | 1100ms  | 340    | 89      | +216.7%  | +7        |
| qwen_local   | $0.0000   | 2300ms  | 340    | 76      | -100.0%  | -6        |
```

And a recommendation like:
> cheapest: qwen_local; highest quality: claude_haiku; best value (70% quality / 30% cost): qwen_local

`projected_daily_savings_usd` at 10k calls/day for this run: `{"claude_haiku": -26.00, "qwen_local": 12.00}` (Claude costs $26/day more; local saves $12/day at $0 GPU cost — swap in your real amortized rate via `cost_per_1k_tokens_override`).

## Merge-gate pattern (branch-deploy quality gate)

`ProviderABEvaluatorComponent` with `min_winner_score: 70` emits an asset check `winner_meets_threshold` that fails ERROR when the winner's score drops below the threshold. Wire it into branch-deploy CI:

```bash
# In your PR runner (e.g. GitHub Actions on branch deploy)
uv run dagster asset materialize \
  --select 'triage_ab_gpt_4o_mini,triage_ab_claude_haiku,triage_ab_qwen_local,triage_ab_scored' \
  -m my_project.definitions
# Exit code non-zero if the check failed → PR blocked
```

Turns "should we go local" into an **auto-checked merge gate**: swap the provider in a PR, branch deploy runs the A/B against real partitions, quality holds → merge; drops → merge blocked. No vendor pitches, no spreadsheets, no judgment calls — the number is the number.

## At scale: partition per prompt

For a real "should we go local" decision you want 50-500 prompts, not one. Partition the whole pipeline on a `dynamic` partitions_def keyed to your prompt corpus:

```yaml
type: dagster_community_components.InferenceProviderABTestComponent
attributes:
  asset_name_prefix: triage_ab
  partition_type: dynamic
  dynamic_partition_name: triage_prompts

  prompt:
    kind: file
    path: /prompts/{partition_key}.md    # one file per partition
  # ... providers, etc.
```

Same shape for evaluator + report. Materialize per-partition (or via a `PartitionedAssetLauncherJobComponent` — see `maintainer_investigation_room.md` for that pattern). Each partition materializes a full A/B + evaluator + report. Over hundreds of partitions you get real distributional statistics — not "one prompt says qwen is 6 points behind" but "across 200 triage prompts qwen is on average 4.2 ± 1.1 points behind claude on classification accuracy specifically."

## Composition with existing agentic pipelines

Point the A/B at outputs from an existing `AgenticPipelineComponent` and you can A/B the whole triage pipeline, not just a single prompt. Downstream of any `llm_call` step, add a variant `agentic_pipeline` YAML with a different model per step + wire the two `<pipeline>_report` assets into a single `ProviderABEvaluatorComponent`. The evaluator scores the full pipeline outputs against a rubric; the report shows which pipeline shape wins on cost + quality.

## What this does NOT do

- **Live routing between providers.** LiteLLM already handles fallback / weighted routing per request; if you want *runtime* routing based on load or provider health, use LiteLLM's `Router` directly. This component is for **decision-time A/B**, not runtime traffic management.
- **Runtime tool-use loops.** Each candidate is one LLM call, not an agent loop with tool use. For that shape use `AgenticPipelineComponent`'s `critique_loop`, `debate`, or `route` ops — or (roadmap) the upcoming `tool_use_loop` op for open-ended tool-picking loops.

## Related components

- **`AgenticPipelineComponent`** — full agentic pipelines with typed named inputs, `route`/`debate`/`critique_loop`/`synthesize`/`mcp_call` ops. Each candidate in the A/B could be an instance of this component instead of a raw LLM call.
- **`PartitionedAssetLauncherJobComponent`** — config-driven entry point for partitioned pipelines. Wraps the A/B in a launcher job so external systems can trigger runs via GraphQL run config.
- **`LLMEvaluatorComponent`** — single-upstream, multi-dimension quality scoring (groundedness, helpfulness, harmfulness, ...). Complementary to `ProviderABEvaluatorComponent`, which is comparative across candidates.
