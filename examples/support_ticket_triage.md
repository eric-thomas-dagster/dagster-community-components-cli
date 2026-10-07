# Support Ticket Triage (map + invoke_component + delegate + agent_card)

**Agent categorizes, a REAL existing component deterministically filters, then a dynamically-discovered agent triages -- three mechanisms, one pipeline.**

**Setup script:** [`setup_support_ticket_triage_demo.sh`](./setup_support_ticket_triage_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card` + `filter`, writes two registered agents + a 3-step pipeline. `bash setup_support_ticket_triage_demo.sh` and `uv run dg dev`.

## What the demo shows

1. **`categorize`** (`op: map`, `output_schema` + `output_join: records`) — fans an LLM call out over each ticket, forced structured output (`category`, `reason`) instead of hopeful free text.
2. **`filter_severe`** (`op: invoke_component`) — calls the REAL `FilterComponent`'s actual asset compute function directly, in-process, against this step's data -- no reimplementation, **no new Dagster asset/lineage node** for the filter itself. `dagster.build_asset_context()` (a real Dagster testing utility) invokes the asset body outside of a real run.
3. **`triaged`** (`op: delegate`) — picks from TWO registered `AgentCardComponent` instances: `triage_agent` (`capabilities: [triage]`) and `billing_lookup_agent` (`capabilities: [lookup]`, deliberately wrong, never actually called). `required_capabilities: [triage]` excludes the billing agent *before* the picker LLM even runs.

The triage agent itself is just an LLM call behind an MCP tool with its own persona -- no external service required, just an API key.

Total cost per run: **~$0.002** (`gpt-4o-mini`, ~8 LLM calls for 6 tickets).

## A real gotcha worth knowing: `{prompt}` is not your data

`triage_agent`'s `tool_args_template` is `{ticket_context: "{extra.src_text}"}`, **not** `{prompt}`. `{prompt}` substitutes the `delegate` step's `task:` *instruction* text, not the actual upstream data -- template it as `{prompt}` alone and the agent only ever sees the instruction, confidently answering from nothing. `{extra.src_text}` is the real upstream data. This only matters when `delegate` has upstream data flowing in via `source:`/a prior step.

## A real layout gotcha: one `defs.yaml` per directory

Each `AgentCardComponent` needs its **own sibling directory** under `defs/` (`defs/triage_agent/defs.yaml`, `defs/billing_agent/defs.yaml`) -- Dagster's component scanner only loads a component from a file literally named `defs.yaml`, one per directory. Dropping multiple component YAMLs in one folder means only one of them actually loads. `delegate`'s sibling-discovery scans the shared *parent* directory, so siblings need to be, structurally, siblings.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
