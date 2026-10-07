# Debate the Best Answer (delegate, registered personas + inputs: ports)

**Two debater agents (shared capability tag, distinguished by semantics) + one arbitrator (distinct tag), joined with typed `inputs:` ports into a final verdict that genuinely weighs both arguments.**

**Setup script:** [`setup_debate_best_answer_demo.sh`](./setup_debate_best_answer_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card`, writes three registered persona agents + a 3-step pipeline. `bash setup_debate_best_answer_demo.sh` and `uv run dg dev`.

## What the demo shows

1. **`for_argument`** / **`against_argument`** (`op: delegate`, both `required_capabilities: [debate]`) — both steps draw from the same two-candidate pool (`debater_advocate`, `debater_skeptic`); the picker distinguishes them by reading each step's FOR/AGAINST task phrasing against each card's own skill description, not by tag.
2. **`verdict`** (`op: delegate`, `required_capabilities: [arbitrate]`) — zero ambiguity, picks the one agent tagged for arbitration. Driven entirely by typed `inputs:` ports (`for_arg`, `against_arg`) substituted directly into its `task:` string, joining the two prior steps by name -- not a `source:` chain.

Total cost per run: **~$0.001** (`gpt-4o-mini`, 6 LLM calls: 3 picks + 3 agent responses).

## Live-validated output

On the proposal *"our team should switch from a 2-week sprint cadence to continuous/weekly releases"*:

- **FOR** (picked `debater_advocate`): *"...continuous releases reduce the risk of large-scale failures, as smaller, incremental updates are easier to manage and troubleshoot."*
- **AGAINST** (picked `debater_skeptic`): *"...the pressure of constant releases could overwhelm team members, leading to burnout..."*
- **VERDICT** (picked `arbitrator`, genuinely weighing both -- not just summarizing): *"...A phased implementation or hybrid model may be a more prudent path forward rather than a full switch."*

## Native `debate` op vs this registered-agent version

The native `debate` op does this in **one step**: `proposers: [{system_prompt: "..."}, ...]` + an `arbitrator:` config, no agent cards needed -- simpler for a one-off pipeline (see [Agentic Pipeline](agentic_pipeline.md) and the existing `agentic_debate` demo). This version is more setup, in exchange for: the advocate/skeptic/arbitrator personas become a *shared, reusable registry* -- any other pipeline in the project can `delegate` to the same `arbitrator` agent for a different decision, without redefining its persona.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
