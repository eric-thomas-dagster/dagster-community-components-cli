# Planner + Specialists — When One Retrieval Isn't Enough

A different orchestration shape than the classic retrieve-then-generate pipeline. Instead of one linear chain, a **planner LLM** reads the task, picks specialist tools from a bounded YAML-declared set, and **each pick becomes its own named asset**. A synthesizer LLM combines the invoked tools' outputs into a final grounded answer.

This is the "agent of agents" pattern with **full Dagster lineage**. Every runtime routing decision is a named entity — inspectable, replayable, gate-able.

## Why this shape

- **Bounded and safe.** Tools are declared in YAML. The planner picks BY NAME. It cannot invent new tools, cannot write code, cannot escape the sandbox.
- **Auditable.** The planner's picks are stored as an asset. Every tool's output is an asset. Every synthesized answer is an asset. Full lineage.
- **Static DAG shape, dynamic routing.** All N tool assets are declared at YAML load — the fan-out is visible in `dg dev`. Tools the planner *didn't* pick still materialize (as empty DataFrames) so the graph stays visually consistent across runs.
- **Gate-able.** Attach an `@asset_check` to `supervisor_plan` to require Slack-approval before tools run. Attach one to `supervisor_final_answer` to require faithfulness ≥ threshold before it advances downstream.

## Asset graph

```
                         ┌───────────────────────────────────────────┐
                         │  task (declared in YAML)                  │
                         │  "How should we harden this nightly job?" │
                         └────────────────────┬──────────────────────┘
                                              ▼
                         ┌───────────────────────────────────────────┐
                         │  supervisor_plan                          │
                         │  (planner LLM reads task, picks tools)    │
                         │  → DataFrame [tool, tool_input, reason]   │
                         └────────────────────┬──────────────────────┘
                                              │
                     ┌────────────────────────┼────────────────────────┐
                     ▼                        ▼                        ▼
   ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
   │ retry_policy_expert  │  │ partitions_expert    │  │ asset_check_expert   │
   │   _result            │  │   _result            │  │   _result            │
   │ (specialist LLM      │  │ (specialist LLM      │  │ (specialist LLM      │
   │  persona)            │  │  persona)            │  │  persona)            │
   │ empty if not picked  │  │ empty if not picked  │  │ empty if not picked  │
   └──────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
              │                         │                         │
              └─────────────────────────┼─────────────────────────┘
                                        ▼
                         ┌───────────────────────────────────────────┐
                         │  supervisor_final_answer                  │
                         │  (synthesizer LLM combines invoked tools) │
                         │  → single grounded answer                 │
                         └───────────────────────────────────────────┘
```

## Run

```bash
export OPENAI_API_KEY=sk-...

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_rag_supervisor_demo.sh \
  -o setup_rag_supervisor_demo.sh
bash setup_rag_supervisor_demo.sh
```

Requirements: `uv` + an OpenAI-compatible key. ~2 min total. Cost: <$0.01 (four short `gpt-4o-mini` calls).

## Sample run

**Task the demo poses:**
> A user asked: "We're seeing intermittent asset failures in a nightly job that syncs from a partitioned source. How should we harden this?" Diagnose which Dagster mechanisms are relevant, propose a concrete configuration, and note quality guardrails.

**What lands in the assets:**

| Asset | What it contains |
|---|---|
| `supervisor_plan` | DataFrame: 3 rows, one per specialist. Each row has the specialist name + a `reason` for the pick. |
| `retry_policy_expert_result` | Specialist LLM output — `RetryPolicy(max_retries=3, delay=5, backoff=exponential)` with tradeoffs |
| `partitions_expert_result` | Specialist LLM output — recommends daily time-based partitioning, notes when dynamic is preferable |
| `asset_check_expert_result` | Specialist LLM output — attach row-count-drop + freshness checks with severity=ERROR |
| `supervisor_final_answer` | Synthesizer LLM output — a 4-6 sentence recommendation combining all three specialists |

Every asset is browsable in `dg dev`. Click the plan → see the reasoning. Click each specialist → see its opinion. Click the final → see the synthesized answer. Six weeks later when someone asks "why did the agent recommend X?", the whole trajectory is one click each.

## Contrast with the linear RAG pipeline

- **[`rag_complete.md`](rag_complete.md)** — linear: `docs → chunks → embeddings → index → retrieve → rerank → answer`. Best when the question is answerable from a specific corpus and quality depends on retrieval + grounding.
- **`rag_supervisor.md`** (this) — decomposed: `task → plan → specialists (fan-out) → synthesis`. Best when the task has multiple facets requiring different personas / knowledge, and no single retrieval is enough.

**Both shapes are compatible** — the specialist tools in a supervisor can themselves invoke retrieval assets from a `rag_complete` pipeline (each tool's system message would tell it to consult the retrieved chunks). That's a natural next iteration; this walkthrough keeps them separate for clarity.

## When to reach for which

| You have... | Reach for |
|---|---|
| A corpus and a specific question | `rag_complete` (Path B — retrieve + rerank + answer) |
| A corpus and you're taking it to prod | `rag_complete` (Path A + B) — versioned snapshots + quality gates |
| A complex multi-facet task | `rag_supervisor` — planner decomposes; specialists execute; synthesizer combines |
| Both | Compose — use `rag_supervisor` where each tool consults the `rag_complete` retrieval pipeline |

## Extending

- **Add more tools.** Every new tool is a YAML entry with `name`, `description` (the planner uses this to decide), and `system_message` (the specialist's persona). Adding a tool doesn't require restart-with-code — just re-materialize.
- **Give tools retrieval access.** Pipe the corresponding `retrieved` asset from `rag_complete` into a tool's `context` (an upcoming enhancement to `SupervisorAgentComponent` — see the component README).
- **Gate the plan on approval.** Attach an `@asset_check` to `supervisor_plan`; when severity=ERROR fails, the tool assets don't run. Post the plan to Slack via a sensor; require thumbs-up before allowing execution.
- **Automation.** Wire `AutomationCondition` on `supervisor_final_answer` so it re-materializes when the plan changes — useful for continuously-evolving tasks like "monitor the state of X."
