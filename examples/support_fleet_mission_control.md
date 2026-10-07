# Support Fleet Mission Control (map + invoke_component + AgentCardWorkspaceComponent + delegate + synthesize)

**The "at scale" story, finally exercised for real: categorize a mixed batch of tickets, deterministically split by category, then route EACH category to the right specialist out of a SIX-agent fleet loaded from one external manifest.**

**Setup script:** [`setup_support_fleet_mission_control_demo.sh`](./setup_support_fleet_mission_control_demo.sh) — scaffolds a Dagster project, installs `agentic_pipeline` + `agent_card_workspace` + `filter`, writes a 6-agent fleet manifest + an 11-step pipeline. `bash setup_support_fleet_mission_control_demo.sh` and `uv run dg dev`.

## What the demo shows — five mechanisms in one pipeline

1. **`categorize`** (`op: map`, structured output) — 12 tickets, each forced into `{category, reason}`.
2. **`filter_billing` / `_technical` / `_security` / `_refunds`** (`op: invoke_component`, 4x) — real `FilterComponent` calls, one deterministic split per category, no new lineage nodes.
3. **The fleet** (`AgentCardWorkspaceComponent`) — bulk-loads SIX specialist agents from one `manifest.json`: billing, technical, security, refunds, legal, enterprise. Built earlier this project's life, never actually run end-to-end until this demo.
4. **`route_billing` / `_technical` / `_security` / `_refunds`** (`op: delegate`, 4x) — each `required_capabilities`-narrows the 6-agent fleet down to exactly 1 before any picker reasoning happens. `legal_specialist` and `enterprise_specialist` are registered but never picked by this pipeline — proving the narrowing actually works at a pool bigger than "just the 2-3 agents this one pipeline happens to need."
5. **`digest`** (`op: synthesize`) — fans all four specialists' response plans into one daily digest.

Total cost per run: **~$0.01** (`gpt-4o-mini`, ~17 LLM calls).

## Live-validated output

12 tickets in, real categorization:

```
T001 → refunds    T005 → technical   T009 → refunds
T002 → billing    T006 → technical   T010 → billing
T003 → billing    T007 → security    T011 → other (correctly excluded)
T004 → technical  T008 → security    T012 → other (correctly excluded)
```

3 billing / 3 technical / 2 security / 2 refunds survive their respective filters; 2 "other" tickets are correctly excluded from all four. Each `route_*` step picks among a full 6-agent pool and lands on exactly the matching specialist every time — `legal_specialist` and `enterprise_specialist` are never touched, confirmed across two independent full runs.

## Why this is the "at scale" demo

Every other `delegate` demo in this repo narrows a pool of 2-3 agents. This is the first with a genuinely larger registry (6) loaded from a single external file rather than six hand-written `AgentCardComponent` YAMLs — add a seventh specialist by adding one entry to `manifest.json`, zero pipeline edits. That's the actual point of `AgentCardWorkspaceComponent`: a fleet maintained by someone other than whoever writes any one pipeline that calls into it.

## Requirements

- `uv`, `OPENAI_API_KEY`
- Deps installed by the setup script: `litellm`, `requests`, `mcp>=1.0.0,<2`
