# LangGraph Agent — Multi-Step Research Pipeline Demo
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Component:** `LangGraphAgentComponent` (`dagster_community_components`)
**Script:** [`setup_langgraph_agent_demo.sh`](./setup_langgraph_agent_demo.sh)
**Cost:** ~$0.005 per run on `gpt-4o-mini` (2,000-3,000 tokens across 4 calls)
**Duration:** ~15 seconds end-to-end
**Validated:** 2026-07-02 (RUN_SUCCESS)

## What it demonstrates

A four-step LangGraph `StateGraph` executed as a single Dagster asset:

```
┌──────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐
│ plan │──▶│ research │──▶│ critique │──▶│ synthesize │
└──────┘   └──────────┘   └──────────┘   └────────────┘
```

Each step is a real OpenAI call. Each step's output lands in shared state so downstream steps can reference it via `{outputs.<step_name>}`. The final materialized value contains the full transcript — plan, research, critique, and final synthesized answer.

## Why LangGraph vs a plain chain

- **Explicit stateful graph.** Every node reads/writes a typed state dict — inspectable, replayable, richer than a linear prompt-and-response chain.
- **Cheap conditional routing.** Early-exit or branch-on-classification is a one-liner (`condition_regex`).
- **Composable at the Dagster level.** Multiple `langgraph_agent` assets can plug into wider DAGs via `deps`.

## Prerequisites

- `uv` installed (`https://docs.astral.sh/uv/`)
- `OPENAI_API_KEY` exported in your shell

Get an OpenAI key at https://platform.openai.com/api-keys — the demo uses ~2,500 tokens per run on `gpt-4o-mini`, well within the free-tier budget.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_langgraph_agent_demo.sh -o setup_langgraph_agent_demo.sh
chmod +x setup_langgraph_agent_demo.sh
./setup_langgraph_agent_demo.sh                          # → langgraph_demo/
./setup_langgraph_agent_demo.sh my_research_agent        # custom name
```

## What the script does

1. Preflight: verifies `uv` + `OPENAI_API_KEY`.
2. `uvx create-dagster project <name> --uv-sync` — scaffolds a fresh Dagster project.
3. `uv add` the deps: `dagster-community-components`, `langgraph`, `langchain-core`, `langchain-openai`.
4. Writes `src/<project>/defs/research_agent/defs.yaml` — a `LangGraphAgentComponent` with 4 steps.
5. `uv run dagster asset materialize --select research_report -m <project>.definitions` — runs the graph.
6. Reports success and next-steps for `dg dev`.

## The generated defs.yaml

```yaml
type: dagster_community_components.LangGraphAgentComponent
attributes:
  asset_name: research_report
  input_prompt: "How do modern vector databases handle high-cardinality metadata filters efficiently without collapsing recall?"
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  system_message: "You are a rigorous senior engineer. Be specific and cite techniques by name."
  temperature: 0.2
  max_tokens: 700
  group_name: research

  steps:
    - name: plan
      prompt: |
        Break the question below into EXACTLY 3 focused sub-questions...
        Question: {input}
      next: research

    - name: research
      prompt: |
        Answer each sub-question below in 2-3 sentences...
        Sub-questions:
        {outputs.plan}
      next: critique

    - name: critique
      prompt: |
        Grade the research below on rigor (0-10)...
        Original question: {input}
        Research:
        {outputs.research}
      next: synthesize

    - name: synthesize
      prompt: |
        Combine the findings and the critique into a single tight paragraph...
        Original question: {input}
        Findings:
        {outputs.research}
        Critique:
        {outputs.critique}
```

## Validated run output (2026-07-02)

```
[langgraph] step=plan       model=gpt-4o-mini prompt_chars=285
[langgraph] step=research   model=gpt-4o-mini prompt_chars=676
[langgraph] step=critique   model=gpt-4o-mini prompt_chars=1684
[langgraph] step=synthesize model=gpt-4o-mini prompt_chars=1920
research_report — STEP_SUCCESS — 14.48s
RUN_SUCCESS
```

Materialized asset value:

```python
{
  "input": "How do modern vector databases handle high-cardinality metadata filters efficiently without collapsing recall?",
  "outputs": {
    "plan":       "1. What indexing structures do modern vector databases use...\n2. How is filter selectivity balanced against ANN recall...\n3. What tradeoffs exist between pre-filter, post-filter, and hybrid...",
    "research":   "1. HNSW and IVF-PQ are the two dominant index families... Pinecone uses ...\n2. Selectivity affects recall through the number of candidate neighbors ...\n3. Pre-filter guarantees correctness at the cost of throughput; post-filter is fast but recall drops when filters are selective ...",
    "critique":   "Rigor: 7/10. Missing angles:\n• No discussion of Weaviate's inverted-index sidecars\n• No mention of DiskANN's post-filter reranking",
    "synthesize": "Modern vector databases combine HNSW or IVF-PQ ANN indexes with metadata filters through one of three strategies: pre-filter (correct but slow at high cardinality), post-filter (fast but recall-lossy when filters are selective), and hybrid (Pinecone's namespace + block-scan approach or Weaviate's inverted-index sidecar). ..."
  },
  "final": "Modern vector databases combine HNSW or IVF-PQ ANN indexes with metadata filters ...",
  "steps_run": ["plan", "research", "critique", "synthesize"],
  "stopped_by": "end_of_pipeline",
  "model": "gpt-4o-mini",
  "provider": "openai"
}
```

## Extension ideas

- **Branch on classification.** Add a step at the front with `condition_regex: "TECHNICAL"` and route non-technical questions to a lightweight `explain` step instead of the full research pipeline.
- **Multi-provider.** Set `model: claude-haiku-4-5-20251001` + `llm_provider: anthropic` on the `critique` step to get an independent-model second opinion.
- **Chain to Dagster assets.** Add `deps: ["upstream_asset"]` and read from state in prompts — the LangGraph pipeline becomes a stage in a larger DAG.

## Bugs surfaced during validation

1. **Pre-existing `aws_glue` component** shipped with `from __future__ import annotations`, which broke Dagster's schema resolver (string annotations). Removed the future-import.
2. **Pre-existing lazy-loader** in `dagster_community_components/__init__.py` used synthetic module names (`_dcc_<name>`), which broke relative imports in sibling modules. Rewrote to use canonical dotted names via `importlib.import_module`.
3. **`pyproject.toml` force-include** was missing `jobs/`, `sinks/`, `transforms/`, `pipelines/`, `schedules/`, `vendors/`, `core/`, `ingestion/`. Added.
4. **Missing-deps handling** — a single missing optional dep (e.g. `azure-mgmt-streamanalytics`) aborted plugin discovery for the whole registry. Made the lazy loader return an inert placeholder so scans skip unavailable components silently.

All four fixes landed in the same PR as the `LangGraphAgentComponent` addition.

## See also

<!-- TODO: link related walkthroughs -->
