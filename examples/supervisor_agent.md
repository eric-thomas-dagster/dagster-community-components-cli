# Supervisor Agent — the agent picks *which agents to call*

**Component (new):** `SupervisorAgentComponent` — one YAML block emits `plan + N tool assets + synthesis`.

**Script:** [`setup_supervisor_agent_demo.sh`](./setup_supervisor_agent_demo.sh)
**Cost:** ~$0.005–$0.02 per run (planner + up to 4 tool calls + synthesis, all gpt-4o-mini)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end. Given a French pricing question, the planner picked 3 of 5 tools (math_expert / web_search / translator), skipped 2 (kb_expert / critic), and the synthesizer wrote a French-language answer with **inline tool citations** ("(Math Expert)", "(Web Search)").

## Why this exists

The next question everyone asks after seeing the [Data Doctor](./data_doctor.md) demo: *"can the agent pick which **agents** to call, not just which actions?"* Yes. This demo is that pattern.

A planner LLM reads a task and picks which specialist tools to invoke from a **bounded, YAML-declared set**. Each tool is a Dagster asset with its own LLM persona (`web_search`, `sql_expert`, `math_expert`, `translator`, `critic`, whatever you declare). A final synthesizer reads all invoked tools' outputs plus the task and writes the grounded answer — citing which tool provided each fact.

```
supervisor_plan     (planner LLM: picks tools + tool_inputs)
       ↓
├── web_search_result       (LLM persona: web search)
├── kb_expert_result        (LLM persona: docs QA)
├── math_expert_result      (LLM persona: calculator)
├── translator_result       (LLM persona: translator)
└── critic_result           (LLM persona: adversarial critic)
       ↓
final_answer        (synthesizer LLM reads all outputs + task → final)
```

## Why this shape is safe

- **Bounded.** The tool set is fixed in YAML at pipeline-write time. The planner picks **by name**. It cannot invent tools, write code, escape the sandbox.
- **Auditable.** The planner's picks + reasons are stored on the `supervisor_plan` asset. Every tool's output is an asset. Full lineage in `dg dev`.
- **Composable.** Gate the plan on an `asset_check`. Publish it to Slack for approval before tools run. Replay with the same task to reproduce.
- **Static DAG shape.** N asset nodes one-per-tool at YAML load time. Tools the planner didn't pick still materialize (as empty DataFrames) so the DAG looks the same across runs — makes the fan-out visible in `dg dev`.

## Where `DynamicOutput` fits

The demo uses **fixed N** tool assets so the DAG shape is visible and demoable. Dagster's `DynamicOutput` (or dynamic partitions on the tool executor) is the pattern for **truly runtime-decided N** — planner might pick 3 tools OR 300, and the pipeline shape adapts per run. Trade-off:

| Shape | When to use | Trade-off |
|---|---|---|
| **Fixed N assets** *(this demo)* | Small, curated tool set (5-20). Wants visible fan-out in `dg dev`. | Unused tools still materialize as empty. |
| **Dynamic partitions** | Large / unbounded tool set. Planner emits any subset. | Partitions are added at runtime; a bit more moving parts. Pair the planner asset with `MetadataValue.dynamic_partition_key`. |

The bounded-action-space principle is the same either way — the planner picks by name, Dagster executes.

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_supervisor_agent_demo.sh -o setup_supervisor_agent_demo.sh
chmod +x setup_supervisor_agent_demo.sh
./setup_supervisor_agent_demo.sh
```

## The demo task

The demo asks the supervisor to answer a French pricing question: *"Combien coûte l'abonnement annuel de 149 euros par mois?"* — plus compare to competitor pricing. Available tools:

| Tool | Description |
|---|---|
| `web_search` | Search the current web for competitor / pricing info. |
| `math_expert` | Do the arithmetic (149 × 12). |
| `translator` | Convert the answer to French for the customer. |
| `kb_expert` | Answer from internal Dagster+ pricing docs (governed KB). |
| `critic` | Adversarial sanity-check the plan before synthesis. |

## Validated run output (2026-07-07)

**Plan the supervisor picked:**

```
tool           tool_input                                    reason
math_expert    149 * 12                                      Calculate the total annual subscription cost.
web_search     annual subscription pricing competitors       Gather recent competitor pricing for comparison.
translator     The annual subscription costs 1788 euros …    Ensure final response is in French.
```

**Tools invoked → their outputs (excerpts):**

- `math_expert_result` → *"1788. Calculated by multiplying 149 by 12."*
- `web_search_result` → *"Company A offers annual subscription for $120 with all premium features. Company B $100, Company C $150 with analytics included…"* (fabricated but realistic snippets with plausible source names)
- `translator_result` → translates the interim answer to French

**Skipped:**
- `kb_expert_result` = empty (the KB was about Dagster+, not relevant to this generic-pricing task — planner correctly skipped)
- `critic_result` = empty (planner didn't think it needed critique)

**Final synthesized answer (with tool citations):**

> Bonjour,
> Merci pour votre question concernant l'abonnement annuel. Si vous payez 149 euros par mois, cela revient à un coût annuel de 1 788 euros **(Math Expert)**.
> En ce qui concerne la comparaison avec les prix des concurrents:
> - **Company A** propose un abonnement annuel à 120 euros…
> - **Company B** et **Company C** offrent des plans à 100 et 150 euros… **(Web Search)**

The synthesizer inline-cites which tool provided each fact — full audit trail from the customer's answer back to the tool call back to the planner's decision.

## Extension patterns

- **Human-in-the-loop.** Add an `asset_check` on `supervisor_plan` that raises if the planner picked any destructive tool (e.g., `send_email`, `delete_record`) — forces manual approval before tools run.
- **Multi-turn.** Chain a second `SupervisorAgentComponent` where the task is *"Here's the previous answer. Was it complete? If not, plan additional tool calls."* Dagster gives you replay + lineage across turns.
- **Real tools.** Replace the LLM-persona tools with real integrations:
  - `web_search` → `duckduckgo_search_asset` or a Tavily component
  - `kb_expert` → `SupabaseVectorSearchAssetComponent` over your real docs
  - `sql_expert` → `sql_transform` against your warehouse
  - `math_expert` → keep as LLM (or use a Python `python_callable_job`)
- **Vercel AI Gateway.** Set `api_base_env_var` to point at Vercel — one key routes across OpenAI/Anthropic/Google/xAI, each tool can use a different model if you fork per-tool.
- **Reasoner + doer split.** Use a stronger model (gpt-4o) for the planner + synthesizer and a cheaper one (gpt-4o-mini) for tool executions. Cost-optimal without losing planning quality.

## The family of agentic-pipeline demos

Supervisor Agent is one of an evolving family. The common pattern: **the LLM picks by name from a bounded, safe set. Dagster executes declaratively.**

1. [**Data Doctor**](./data_doctor.md) — agent picks column REMEDIATIONS.
2. [**Adaptive Triage Router**](./adaptive_triage.md) — agent picks per-row DOWNSTREAM ROUTE.
3. [**Adaptive Backfill Detective**](./adaptive_backfill.md) — agent picks per-partition FILL STRATEGY.
4. **Supervisor Agent** *(this demo)* — agent picks WHICH SPECIALIST TOOLS to call.
5. **MCP Tool Picker** *(coming)* — same shape, but tools are MCP servers.
6. **Adaptive Research Brief** *(coming)* — planner emits a variable-N list of sub-topics, each becomes a parallel research asset.

Together, this covers most patterns customers ask for when they say "we want AI in our pipelines."

## Related

- [Agent + MCP tool loop](./agent_family.md) — deterministic MCP tool call vs agentic vs evaluated; a different agentic shape (single agent with real MCP tools).
- [LangGraph Agent](./langgraph_agent.md) — multi-step reasoning inside a single asset (plan → research → critique → synthesize). Supervisor is the *between-assets* orchestration primitive; LangGraph is the *within-asset* one.
- [Cube + LLM](./cube_llm.md) — LLM over structured metrics with a governed schema layer (the "safety layer" pattern).
