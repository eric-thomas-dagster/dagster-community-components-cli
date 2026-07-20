# Adaptive Research Brief — the agent decides *how many* things to research

**Component (new):** `AdaptiveResearchBriefComponent` — one YAML block emits `plan + notes + brief`.

**Script:** [`setup_adaptive_research_brief_demo.sh`](./setup_adaptive_research_brief_demo.sh)
**Cost:** ~$0.01–$0.05 per run (planner + N × researcher + synthesizer, all gpt-4o-mini)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end. Planner decided N=5 subtopics for the Anthropic competitive brief (product / pricing / safety / research / differentiation), each got a research note with plausible citations, synthesizer produced a well-structured markdown brief with headings + executive summary.

## Why this exists (and where DynamicOutput fits)

The Data Doctor / Supervisor / MCP-Picker demos all have a **fixed action space** — the tools or actions are declared in YAML at pipeline-write time. This demo is different: **N is truly runtime-decided.** The planner LLM reads a topic and picks how many sub-topics to research — could be 3, could be 12 — based on what the topic actually needs.

That's where Dagster's `DynamicOutput` / dynamic partitions primitives come in. When N is genuinely dynamic per run, you need a mechanism for the pipeline to grow/shrink to fit. This component demonstrates the **single-asset iteration** version of the pattern (all N researcher calls happen inside one asset). The walkthrough shows how to upgrade to **dynamic partitions** for per-subtopic asset visibility.

```
research_plan       (planner LLM: decides N and emits {angle, focus} per subtopic)
       ↓
subtopic_notes      (row-wise LLM: writes a research note per subtopic — N calls)
       ↓
research_brief      (synthesizer LLM: markdown brief with headings + exec summary)
```

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_adaptive_research_brief_demo.sh -o setup_adaptive_research_brief_demo.sh
chmod +x setup_adaptive_research_brief_demo.sh
./setup_adaptive_research_brief_demo.sh
```

## Validated run output (2026-07-07)

**Planner picked N=5 subtopics** for the Anthropic brief:

```
# angle                     focus
1 Product Lineup            Analyze Anthropic's current AI products and capabilities.
2 Pricing Model             Investigate subscription and usage-based pricing strategies.
3 Safety Approach           Examine methodologies and frameworks for safe AI deployment.
4 Recent Research Directions Summarize notable research initiatives and publications.
5 Differentiation Factors   Key differentiators vs OpenAI and Google DeepMind.
```

**Researcher wrote 5 notes** (excerpt, subtopic #1):

> Anthropic's current product lineup is anchored by its flagship AI model, Claude, tailored for conversational tasks with a strong emphasis on safety and user alignment. Claude is available in various tiers, accommodating different business needs, and offers features such as multi-turn dialogue, contextual understanding, and customizable behavior settings *(AI Insights Weekly, 2023)*. Additionally, Anthropic provides API access for developers, facilitating seamless integration…

**Synthesizer emitted a full markdown brief** with 5 subtopic sections + an executive summary. Each section preserves the researcher's citations *(TechCrunch 2026, Anthropic blog, etc.)*.

## The runtime-N part

Every run, the planner independently decides N. Rerun with a narrower topic ("what's the difference between Claude 3.5 Sonnet and Claude 3 Opus?") — N will likely be 2-3. Rerun with a broader topic ("state of the AI industry in 2026") — N will be closer to `max_subtopics` (default 8).

The `max_subtopics` field is a **safety rail**, not a target. The planner is told about the cap and picks the N it thinks best covers the topic.

## Upgrading to dynamic partitions (per-subtopic visibility)

The v1 shape runs all N researcher calls inside `subtopic_notes` — one asset materialization, N LLM calls under the hood. Great for simplicity + demoability. But you don't get per-subtopic UI visibility or per-subtopic retry.

For truly per-subtopic assets:

1. Pre-declare a `DynamicPartitionsDefinition(name="subtopics")` on the notes asset.
2. In the planner asset, after emitting the plan DataFrame, register each `angle` as a new partition key via `context.instance.add_dynamic_partitions("subtopics", [...])`.
3. Change the notes asset to be dynamic-partitioned; its compute reads its partition key, looks up the corresponding row in the plan, and runs the researcher LLM for that ONE row.
4. Materialize the notes asset for all partitions (`--partition-range` or all-partitions).

Result: N separate materializations of the notes asset in `dg dev`, each independently retryable. Same overall pattern — planner picks by name from a bounded conceptual space, Dagster executes.

## Extension patterns

- **Real tools as researchers.** Swap the LLM-persona researcher for a `web_search_asset` per row, or an MCP call, or a Supabase vector search over your knowledge base. Same 3-asset shape, real research grounded in real data.
- **Human review of the plan.** Add an `asset_check` on `research_plan` that fails if N > threshold or a subtopic looks off-topic. Forces manual approval before the (potentially expensive) researcher iterations run.
- **Multi-turn refinement.** After `research_brief`, add another planner that reads the brief + user feedback and picks a set of "improvements to research further." Chain another Adaptive Research Brief for that follow-up scope.
- **Different sink.** Instead of a DataFrame-in-asset, write the brief to a Notion page (`notion_page_writer`), a Google Doc, or Slack (`slack_notification`).

## Part of the agent-pipeline patterns family

See [agent_pipeline_patterns.md](./agent_pipeline_patterns.md) — overview of all seven agent-pipeline demos with a selection guide + adjacent-but-not-agentic patterns (`langgraph_agent`, `dbt_llm_pipeline`, `pii_redaction`, `data_quality_agent`, `cube_llm`).

## Related

- [`langchain_chain_asset`](https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/assets/ai/langchain_chain_asset/README.md) — the row-wise LLM primitive this component wraps for the researcher step.
- [Supervisor Agent](./supervisor_agent.md) — picks which specialist to call (N of DIFFERENT kinds). This demo picks how many of the SAME kind of work.
- [LangGraph Agent](./langgraph_agent.md) — multi-step ReAct loop inside a single asset. Use for tight tool-use loops where each step depends on the previous.
