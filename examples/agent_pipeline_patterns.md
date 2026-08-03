# Agent-pipeline patterns

Seven Dagster demos where **the agent decides what the pipeline does** — a specific
shape distinct from "LLM in the middle" or "multi-step ReAct." Read this first if you're
picking which one to build.

## The shared shape

Every demo in this family follows the same skeleton:

```
input data / question  →  LLM makes a bounded decision  →  Dagster executes the decision
                                                          (with full asset lineage)
```

Three properties fall out:

- **The action space is enumerated.** The LLM picks from a fixed set (drop_nulls / fill_median / …
  or route_to_billing / route_to_bug / …). Never free-form Python.
- **Every decision is a materialization.** The plan itself is a Dagster asset — you get an
  audit trail of "what did the LLM decide, why, and what did we do about it" for free.
- **The downstream execution is deterministic components.** No LLM ever writes SQL, calls an
  API, or mutates state directly. It only tags rows / picks tools / chooses configs; other
  components carry them out.

That's the safety envelope. If you're evaluating LLM-driven pipelines for production, this
is the shape to reach for first.

## Selection guide

Pick along two axes: **what** the agent decides, and **how many times** it decides in one run.

| # | What the agent decides | Cardinality | Demo |
|---|---|---|---|
| 1 | Column remediation action from a bounded set | Per-column, single-shot | [Data Doctor](./data_doctor.md) |
| 2 | Downstream route (which sink each row goes to) | Per-row, single-shot | [Adaptive Triage Router](./adaptive_triage.md) |
| 3 | Fill strategy for a data gap | Per-partition, single-shot | [Adaptive Backfill Detective](./adaptive_backfill.md) |
| 4 | Which specialist tools to invoke | Whole run, single-shot | [Supervisor Agent](./supervisor_agent.md) |
| 5 | Which MCP tools to invoke | Whole run, single-shot | [MCP Tool Picker](./mcp_tool_picker.md) |
| 6 | How many items to work on | Whole run, single-shot | [Adaptive Research Brief](./adaptive_research_brief.md) |
| 7 | Which tools + what next, given prior step outputs | Whole run, iterative (chained) | [Iterative Supervisor Agent](./iterative_supervisor_agent.md) |

**Rules of thumb:**

- Row-level or partition-level decision that fans out to downstream sinks → **2 or 3**.
- Whole-run tool selection where each tool runs independently and you synthesize at the end → **4 or 5**.
- Same shape as #4/#5 but each tool sees prior step outputs (research → critique → refine → …) → **7**.
- Column-level QC choice on structured data → **1**.
- "How much work is this task worth?" as the decision → **6**.

If the "decision" is actually a natural-language answer, or the LLM is generating the content
itself (a story, an email, SQL), you're not in this family — see the **adjacent shapes** below.

## Adjacent shapes (NOT this family)

These sit next to the agent-pipeline patterns and are often confused with them, but the mental
model is different:

| Demo | Why it's adjacent | Why it's NOT this family |
|---|---|---|
| [LangGraph Agent](./langgraph_agent.md) | Multi-step LLM reasoning inside a single asset | The LLM is doing the WORK (planning, researching, synthesizing) — not just picking from a bounded set. Use when each step's output feeds the next inside one tight loop. |
| [dbt + LLM + dbt](./dbt_llm_pipeline.md) | LLM in the middle of a data pipeline | The LLM GENERATES content (personalized emails, summaries) between deterministic dbt models. It's not deciding what the pipeline does — the dbt DAG is fixed. |
| [PII Detection + LLM Redaction](./pii_redaction.md) | LLM as a second pass on a data pipeline | The LLM is a DOUBLE-CHECKER on statistical output, not a decider. Two-pass compliance shape (statistical + LLM), not agent-decides-action. |
| [Data Quality Agent](./data_quality_agent.md) | Similar name, LLM writes per-anomaly explanations | The LLM NARRATES anomalies for on-call — it doesn't pick remediations. Closest cousin to Data Doctor but with narration semantics instead of action semantics. |
| [Cube + LLM](./cube_llm.md) | LLM over structured data | The LLM SUMMARIZES governed metrics from Cube. No decision, no action space, no downstream fan-out. |

If you're pitching to a customer, the fast disambiguator is: **"does the LLM's output tell
Dagster what to run next, or is it the final artifact the user sees?"** Former → this family.
Latter → adjacent.

## Why this family matters (compared to "just call an LLM")

Every demo above satisfies four properties that a bare `openai.chat.completions.create()` call
doesn't:

1. **Bounded actions.** The LLM output is constrained to an enum or JSON schema. No prompt-inject
   can make it "run rm -rf" — the action space doesn't contain that.
2. **Auditable plans.** The plan is a Dagster asset with metadata. Someone can look at last
   Tuesday's run and see exactly which route the LLM chose for which ticket, and why.
3. **Deterministic downstream.** Execution components don't touch the LLM. Reruns of the
   execution step are reproducible against the same plan.
4. **Failure isolation.** If the LLM step fails or produces bad output, Dagster marks that
   asset failed — downstream execution doesn't fire. No half-applied changes.

That's the shape most enterprises actually want when they say "AI pipeline." Pointing customers
at one of these seven is almost always better than "let me spin up a LangChain agent for you."

## See also

- [`examples/README.md`](./README.md) — the flat index of all demos in this repo.
- [Component registry](https://dagster-component-ui.vercel.app/) — search the ~950 community components; the ones marked `agent`/`ai` are the building blocks these demos compose.
