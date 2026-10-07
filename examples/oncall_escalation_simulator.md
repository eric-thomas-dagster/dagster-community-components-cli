# On-Call Escalation Simulator (tool_use_loop + extract + delegate)

**Extends [Pipeline Incident Triage](pipeline_incident_triage.md) one stage further: after a real investigation lands on a diagnosis, route the escalation to whichever on-call team actually owns it — with a team-appropriate drafted message, not a generic alert.**

**Setup script:** [`setup_oncall_escalation_simulator_demo.sh`](./setup_oncall_escalation_simulator_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card`, writes three on-call team agents + a 3-step pipeline. `bash setup_oncall_escalation_simulator_demo.sh` and `uv run dg dev`.

## What the demo shows

1. **`investigate`** / **`diagnosis`** — the exact same real investigation as Pipeline Incident Triage: live vendor status check, real `git log`, real runbook search, forced into clean `{root_cause, action, reasoning}` JSON.
2. **`escalate`** (`op: delegate`, **no** `required_capabilities`) — picks among three registered on-call teams (`infra_oncall`, `data_oncall`, `payments_oncall`) based on genuine semantic judgment about who owns this *kind* of incident, not a hardcoded owner-mapping table. Drafts a team-appropriate Slack-style message.

Total cost per run: **~$0.002** (`gpt-4o-mini`, ~5 LLM calls).

## Honest about the ambiguity — on purpose

The default incident (a dbt model timing out against a degraded Snowflake) is a genuinely ambiguous ownership call: it's plausibly "infra" (Snowflake, the vendor) or "data" (dbt, the model, the downstream dashboards). Across two separate real runs validating this demo, the picker chose `infra_oncall` once and `data_oncall` once — both with sound, specific reasoning tied to the actual incident. That's not a bug to paper over: a fixed `if vendor == "snowflake": team = "infra"` lookup table can't make a judgment call like this at all. Semantic picking can, and reasonable people (and models) can land on either side of a genuinely ambiguous case. Run it yourself and read `picker_reasoning` on the `escalate` asset.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
