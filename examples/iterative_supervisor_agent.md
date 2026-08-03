# Iterative Supervisor Agent — chained tool use with per-step Dagster lineage

**Component (new):** `IterativeSupervisorAgentComponent` — one YAML block emits `N step assets + synthesis`.

**Script:** [`setup_iterative_supervisor_demo.sh`](./setup_iterative_supervisor_demo.sh)
**Cost:** ~$0.02 per run (planner + tool calls across ~2–3 non-noop steps, all gpt-4o-mini)
**Validated:** 2026-07-07 — RUN_SUCCESS end-to-end. Same French pricing question that showed [Supervisor Agent](./supervisor_agent.md)'s placeholder limitation. This time the agent chained `math_expert(149 * 12) → 1788 → writer("L'abonnement annuel coûte 1788 euros")` and produced a real French sentence with the actual number. Step 3 declared done; steps 4-5 short-circuited.

## Why this exists

[Supervisor Agent](./supervisor_agent.md) does **single-shot planning**: the planner picks all tools upfront in one LLM call. That's fine when tools are independent — but breaks down when step 2 needs to see step 1's output.

In the French pricing demo, the single-shot planner had to write the translator's input BEFORE the math tool actually ran, so it left a placeholder: *"The annual subscription costs [placeholder] euros. Here are some competitor prices: [insert competitor prices]."* The synthesizer patched it up at the end, but the trajectory wasn't ideal.

**Iterative Supervisor fixes this.** The planner runs once *per step*, sees all prior tool outputs, and picks the next tool call. Each step is its own Dagster asset — so you can inspect the full ReAct loop in `dg dev`. Whichever step declares `done` short-circuits subsequent steps to no-ops. Static DAG shape (`max_iterations` step assets pre-declared), dynamic termination.

```
agent_step_1     (planner sees task alone → picks 1st tool → runs it)
       ↓
agent_step_2     (planner sees step_1 output → picks next tool OR done)
       ↓
agent_step_3     (planner sees step_1 + step_2 → picks or done)
       ↓
agent_step_4     (short-circuits if step_3 was done)
       ↓
agent_step_5     (short-circuits if any prior step was done)
       ↓
agent_final_answer   (synthesizer reads full trajectory → final answer)
```

## Single-shot vs Iterative — the trade-off

| Feature | Supervisor Agent | Iterative Supervisor Agent |
|---|---|---|
| Planning | Single-shot (1 planner call) | Per-step (N planner calls) |
| Tool chaining | ❌ No | ✅ Yes |
| Cost | Cheaper | ~N× more expensive |
| DAG shape | Fan-out (N tools in parallel) | Chain (N steps sequential) |
| Best for | Independent tools | Tasks where step 2 depends on step 1 |

Pick single-shot when tools don't chain (e.g., "look up info from web search + KB + math in parallel, then combine"). Pick iterative when tools chain (e.g., "compute a number, then use it to format a message").

## Prerequisites

- `uv` + `OPENAI_API_KEY`

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_iterative_supervisor_demo.sh -o setup_iterative_supervisor_demo.sh
chmod +x setup_iterative_supervisor_demo.sh
./setup_iterative_supervisor_demo.sh
```

## Validated trajectory (2026-07-07)

Same task as the single-shot Supervisor Agent demo — the French pricing question — with `max_iterations: 5`. Available tools: `math_expert`, `translator`, `writer`.

```
step 1 — done=False tool=math_expert
  reasoning: "We need to compute the annual cost before translating and drafting a customer-friendly response."
  args:      149 * 12
  output:    1788 - This is the product of 149 and 12.

step 2 — done=False tool=writer
  reasoning: "We need to draft a polished, customer-friendly sentence in French to respond to the customer."
  args:      L'abonnement annuel coûte 1788 euros.       ← REAL VALUE from step 1
  output:    Nous sommes ravis de vous informer que notre abonnement annuel est disponible pour seulement 1788 euros.

step 3 — done=True tool=(none)
  reasoning: "The task is complete as we have computed the annual cost and drafted a polished, customer-friendly response."

step 4 — done=True tool=(none)  (short-circuit — prior step done)
step 5 — done=True tool=(none)  (short-circuit — prior step done)

FINAL ANSWER (2 tool calls):
  "Nous sommes ravis de vous informer que notre abonnement annuel est disponible pour seulement 1788 euros. (Step 2)"
```

The key contrast: in Supervisor Agent, step 2's args had a placeholder because the planner couldn't see step 1's output yet. In Iterative Supervisor Agent, **step 2's args contain the real "1788"** — because the planner ran AFTER step 1 completed and saw its output.

## The static-DAG-with-dynamic-termination trick

The N step assets are declared at YAML load time (this is a Dagster requirement: assets must be known at load, not runtime). Every step's compute checks whether any prior step already reported `done` — if so, it short-circuits to a no-op row. So:

- Task finishes in 2 steps → steps 3, 4, 5 materialize as no-ops
- Task uses all 5 steps → step 5 forces `done=True` if the planner picks another tool
- Task needs more than `max_iterations` → the agent runs out of budget; synthesizer works with whatever it has

Bump `max_iterations` up if tasks tend to need more turns; drop it down if the agent tends to loop unnecessarily.

## Extension patterns

- **Real MCP tools in a loop.** Combine this component's iterative shape with `mcp_tool_picker`'s MCP execution. Substantial new component — the natural next primitive.
- **Human-in-the-loop.** Add an `asset_check` on each step that requires manual approval when the planner picks a destructive tool.
- **Cost cap.** Bake a `max_total_tokens` field into the component; on exceed, force `done=True` in the next step.
- **Reasoner + doer split.** Use a stronger model for the planner (gpt-4o) and cheaper for tools (gpt-4o-mini) — set different `model` fields per tool (would require component extension).

## Part of the agent-pipeline patterns family

See [agent_pipeline_patterns.md](./agent_pipeline_patterns.md) — overview of all seven agent-pipeline demos with a selection guide + adjacent-but-not-agentic patterns (`langgraph_agent`, `dbt_llm_pipeline`, `pii_redaction`, `data_quality_agent`, `cube_llm`).

## See also

- [Supervisor Agent](./supervisor_agent.md) — single-shot version. Cheaper. Use when tools don't chain.
- [LangGraph Agent](./langgraph_agent.md) — ReAct loop inside a single asset (no per-step Dagster visibility). Use for tight tool-use loops where you don't need per-step lineage.
- [Agent Family](./agent_family.md) — three shapes of MCP use (deterministic vs agentic vs evaluated). This component is a fourth shape.
