# Pipeline Incident Triage (tool_use_loop + extract)

**One agent, three REAL tools, iterating freely until it has enough evidence -- then a forced-structure pass turns its free-text diagnosis into clean JSON.**

Inspired by a real cross-stack pipeline-incident-triage tool: when something breaks, synthesize vendor status + institutional knowledge + recent deploys into a root cause AND a decision -- critically, **"wait, don't debug" vs. actually troubleshoot**, since debugging a vendor outage wastes engineering time the vendor is already spending.

**Setup script:** [`setup_pipeline_incident_triage_demo.sh`](./setup_pipeline_incident_triage_demo.sh) — scaffolds a full Dagster project, installs `agentic_pipeline`, writes a 3-tool MCP server + a `tool_use_loop` → `extract` pipeline. `bash setup_pipeline_incident_triage_demo.sh` and `uv run dg dev`.

## What the demo shows

1. **`investigate`** (`op: tool_use_loop`) — ONE agent, THREE real tools:
   - `check_vendor_status` — a live HTTP call to the vendor's actual public status API (the same Statuspage.io-backed endpoints real uptime-monitoring integrations use): `githubstatus.com`, `status.fivetran.com`, `status.getdbt.com`, `status.snowflake.com`.
   - `get_recent_commits` — a real `git log -n --oneline` against the project.
   - `search_runbooks` — real keyword search over a small institutional-knowledge fixture.

   The agent decides which tools to call and in what order -- not a fixed script -- then calls `finalize` with a labeled diagnosis once it has enough evidence.
2. **`diagnosis`** (`op: extract`) — forces the labeled free text into clean `{root_cause, action, reasoning}` JSON via tool-choice-forced extraction.

Total cost per run: **~$0.001** (`gpt-4o-mini`, ~4 LLM calls).

## Live-validated output

Run against the incident *"Asset `stg_orders` failed to materialize: the dbt model's query against Snowflake timed out / connection reset... No recent code changes are suspected, but please confirm."* — at the time this was validated, Snowflake's real status page happened to show **"Partially Degraded Service"**, live:

```
iter 1: check_vendor_status(vendor=snowflake) → {"status": "minor", "description": "Partially Degraded Service"}
iter 1: get_recent_commits(n=5) → real commits from the project's actual git log
iter 2: search_runbooks(query="dbt model query timeout connection reset") → matched vendor-degradation-wait runbook
iter 3: finalize(...)
```

```json
{
  "root_cause": "Snowflake is experiencing a minor degraded service affecting connections, which is causing the dbt model to timeout.",
  "action": "wait",
  "reasoning": "The Snowflake status page indicates a partially degraded service, which aligns with the connection reset issue reported. Additionally, there are no recent code changes that could have caused this."
}
```

Genuinely synthesized from three independently-verifiable real sources, not a canned response -- re-run this after Snowflake's status returns to fully operational and expect a correctly different diagnosis.

## Why `tool_use_loop` instead of three `delegate` calls

You could model this as three registered specialist agents and three `delegate` calls (see [Route to a Specialist](route_to_specialist.md)). `tool_use_loop` is a better fit here because the *order and number* of checks genuinely depends on what the agent finds -- it might skip the runbook search if vendor status alone is conclusive. `delegate` is for "pick which registered specialist handles this" (one decision); `tool_use_loop` is for "figure out, as you go, which combination of tools you need" (an open-ended investigation).

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
