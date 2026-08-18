# Maintainer investigation room — AI triage for a GitHub issue, ending in a human sign-off

> ⚠️ **Dagster+ Serverless / Hybrid:** the runnable demo writes an approval-token
> directory + a draft markdown file to the project directory. Deploys, but the
> filesystem sensor + approval-file pattern only progresses on the box those
> paths live on. For a serverless variant, swap `approvals/` for Google Cloud
> Storage (there's `gcs_monitor`) or an object-store sensor of your choice.

You give an agent a real, open GitHub issue on a public repo. Multiple
specialists fan out — one reasons about the codebase, one about the docs,
one about how it'd repro, one about prior similar reports. A triage lead
synthesizes. A skeptic critiques the triage. A human signs off. Then the
report ships.

This is the pattern **Prefect's Aug-13 pre-MVP Loom** calls "agentic
orchestration." Dagster's take: **every specialist is an asset, every
decision is a materialization, the report is a warehouse-ready
artifact, and the whole graph lives next to your dbt models.** No
sidecar workflow tab.

## Architecture — 9-node "execution plan" as ONE YAML

The whole pipeline is a single `AgenticPipelineComponent` using the new
`mcp_call` op + typed named `inputs:` field. Every node has explicit
input port names (Prefect's execution-plan shape); every node is a
first-class Dagster asset. Edges wire specific outputs to specific
inputs by name.

```
                    ┌──────────────────────────────────────┐
                    │  intake  (mcp_call — GitHub MCP)     │
                    │  outputs: facts (issue title, body,  │
                    │           labels, author, state)     │
                    └───────────────┬──────────────────────┘
                                    │  every specialist reads
                                    │  { issue_facts: from intake }
        ┌───────────────┬───────────┼───────────┬───────────────┐
        ▼               ▼           ▼           ▼               ▼
  ┌───────────┐  ┌───────────┐  ┌─────────┐  ┌───────────────┐
  │ repo_     │  │ docs_     │  │ reprod- │  │ history_      │
  │ evidence  │  │ evidence  │  │ uction  │  │ evidence      │
  │(llm_call) │  │(llm_call) │  │(llm_call│  │ (llm_call)    │
  └─────┬─────┘  └─────┬─────┘  └─────┬───┘  └───────┬───────┘
        │              │              │              │
        └──────────────┴──────┬───────┴──────────────┘
                              ▼
      ┌───────────────────────────────────────────────────────┐
      │  preliminary  (synthesize — 6 typed named inputs)      │
      │  inputs:                                              │
      │    issue_facts:      {from: intake}                   │
      │    reproduction:     {from: reproduction}             │
      │    docs_evidence:    {from: docs_evidence}            │
      │    repo_evidence:    {from: repo_evidence}            │
      │    history_evidence: {from: history_evidence}         │
      │    triage_policy:    {literal: "defect | expected |…"}│
      └────────────────────────┬──────────────────────────────┘
                               │
                               ▼
      ┌───────────────────────────────────────────────────────┐
      │  skeptic  (llm_call — 2 typed inputs)                 │
      │  inputs: { issue_facts: from intake,                  │
      │            preliminary:  from preliminary }           │
      └────────────────────────┬──────────────────────────────┘
                               │
                               ▼
      ┌───────────────────────────────────────────────────────┐
      │  decision  (synthesize — 4 typed inputs, Prefect's    │
      │            "Final routing decision" node)             │
      │  inputs: { skeptic, issue_facts, preliminary,         │
      │            triage_policy }                            │
      └────────────────────────┬──────────────────────────────┘
                               │
                               ▼
      ┌───────────────────────────────────────────────────────┐
      │  report  (llm_call — inputs: { decision } → markdown) │
      └────────────────────────┬──────────────────────────────┘
                               │  materializes as mir_report
                               ▼
      ┌───────────────────────────────────────────────────────┐
      │  report_approval  (HumanApprovalGateComponent)        │
      │  gate: reads approvals/default.json                   │
      │    missing / rejected → asset check 'approved' fails  │
      │                         (WARN / ERROR)                │
      │    {"approved": true}  → asset check 'approved' passes│
      │                         + approver, reason, timestamp │
      │                         in materialization metadata   │
      └────────────────────────┬──────────────────────────────┘
                               │
        ┌── approval_watcher (FilesystemMonitorSensorComponent) ──┐
        │    watches approvals/*.json → launches ship_report_job  │
        │    the moment a new token appears                       │
        └─────────────────────────────────────────────────────────┘
```

Every input arrow is a **named port**. Every port maps to a
`{port_name}` placeholder in the target step's `prompt_template` +
`system_prompt`. That's the same shape Prefect's execution-plan graph
shows in their Aug-13 pre-MVP Loom — same primitive, on Dagster's
substrate, where every port output is a queryable asset key.

## What each component does

- **`maintainer_investigation`** — [`AgenticPipelineComponent`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/assets/ai/agentic_pipeline). One YAML declares the whole 9-node execution plan. Uses **two v2 features**:
  - **`mcp_call` op** — the `intake` step calls the GitHub MCP directly (no planner LLM, just a deterministic MCP tool invocation). Prefect's "MCP server config with deferred secrets" story maps 1:1 to `server: {type: stdio, command: [npx, -y, "@modelcontextprotocol/server-github"]}` (token from parent process env, no YAML inlining). For http-transport MCP with deferred secrets, use `headers_env: {Authorization: GITHUB_MCP_TOKEN}`.
  - **`inputs:` field** — every node with more than one upstream declares `inputs: {port_name: {from: step_id} | {literal: value}}` for typed named multi-input joins. `preliminary` reads 6 named inputs, `skeptic` reads 2, `decision` reads 4, `report` reads 1. Each port becomes a `{port_name}` placeholder in the step's `prompt_template` + `system_prompt` — same shape as Prefect's node cards showing named ports.

  Every one of the 9 step outputs is a first-class Dagster asset key. `mir_intake` / `mir_repo_evidence` / `mir_preliminary` / `mir_decision` / `mir_report` (etc.) can all be depended on by downstream dbt models, warehouse writes, notification sinks, whatever. That's the seam Prefect can't cross — their execution plan is bound to one Flow, so no port has an addressable key that other things depend on.

- **`report_approval`** — [`HumanApprovalGateComponent`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/assets/ai/human_approval_gate). Always materializes so the asset stays green in the UI; state comes from the `approved` asset check. Missing token → `passed=False (WARN)` with `status: approval_pending` + hint metadata (re-materialize once the token appears, or let the sensor do it). `{approved: false}` → `passed=False (ERROR)` with rejection reason. `{approved: true}` → `passed=True` + report passes through with approver / reason / approved_at in metadata. Dagster+ Insights + alerts key off the check result — a rejected report pages someone without you writing any alerting code. `upstream_asset_key: mir_report` uses bare-string form (multi-part keys use slash notation; both map to `AssetKey.from_user_string()`).

- **`approval_watcher`** — [`FilesystemMonitorSensorComponent`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/sensors/filesystem_monitor). Polls `approvals/` every 5s. New `default.json` → launches `ship_report_job` → `report_approval` materializes → gate passes.

- **`ship_report_job`** — [`AssetJobComponent`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/schedules/asset_job). Named job over `[report_approval]` so the sensor has a discrete target. Also gives you a one-click "run this now" button in the Jobs tab.

## Run

```bash
export OPENAI_API_KEY=sk-...
export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_...   # any classic PAT, public_repo scope
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_maintainer_investigation_room_demo.sh \
  -o setup_maintainer_investigation_room_demo.sh
bash setup_maintainer_investigation_room_demo.sh
```

Requirements: `uv`, `npx` (Node — for the stdio GitHub MCP server),
`OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`. ~90s first run. Uses ~15
gpt-4o-mini calls + 3 gpt-4o calls per issue (roughly $0.02).

Point at any dagster-io/dagster issue with:

```bash
DAGSTER_ISSUE_NUM=25000 bash setup_maintainer_investigation_room_demo.sh
```

## Two scenarios the script walks

**Scenario A — approved path.** The script materializes the whole graph
up through `maintainer_investigation_report`. The report gets written to
`reports/investigation_draft.md` via the AgenticPipeline's `text_sinks`.
Then it drops an approval token and re-materializes `report_approval`.
The gate passes, records the approver, downstream unblocks.

**Scenario B — the wait state.** After the script exits, the
`approval_watcher` sensor is still running under `dg dev`. Drop a second
token:

```bash
echo '{"approved":true,"approver":"you"}' > $PROJECT/approvals/second.json
```

The sensor picks it up within 5s and auto-launches `ship_report_job`
for you — no manual re-materialize. That's the "human input mid-run"
shape (via file drop / Slack modal / Retool form) — different mechanism
than Prefect's promised UI-native mid-run input, same end result.

## Where this beats Prefect's Aug-13 demo

- **Cross-boundary lineage.** `maintainer_investigation_report` is an asset
  key. Point a dbt model at it. Add a `duckdb_table_writer` downstream.
  Wire it into your Insights dashboard. Prefect's plan is a Flow-scoped
  artifact — it doesn't have an asset key that other things depend on.
- **Deterministic + agent nodes in the same graph.** Nothing stops you
  from adding a `snowflake_workspace` task or a `dbt_project` component
  next to these — the graph doesn't care that some nodes are LLM calls
  and others are `EXECUTE TASK`. Prefect explicitly said this ("agent
  vs deterministic node") is planned, not shipped.
- **In-agent observability for free.** Each specialist emits an
  `AssetObservation` per LLM call with token counts + model + duration.
  Launchpad shows the tool call sequence. Insights turns it into a
  metric. Prefect's Loom explicitly called out that inside-agent
  observability is not there yet.
- **Human sign-off — shipped, not "actively working on."** The gate +
  sensor + auto-progression pattern already runs to `RUN_SUCCESS`
  today. Prefect: "human input node — actively working on."
- **Report retrieval.** The `report` asset materializes with structured
  metadata + a markdown side file. Both are queryable in the UI right
  now. Prefect explicitly said "fetch the report from the UI — not
  shipped."

## Where Prefect has an edge — and how to close it here

- **"Coding agent authors the plan via MCP" opener.** That's the demo
  beat that lands. In Dagster's story, you'd start by running:
  ```bash
  uvx --from dagster-community-components-cli dagster-component init
  ```
  in a bare directory. That writes `CLAUDE.md` / `.cursorrules` /
  `.github/copilot-instructions.md`. Then you tell your coding assistant
  (component-agnostic version — let it discover them):

  > *Build a Dagster project that takes a public GitHub issue number and
  > does an AI-driven maintainer triage on it: fetch the issue + comments
  > via the GitHub MCP server (`npx -y @modelcontextprotocol/server-github`),
  > fan out 4 LLM specialists (repo/code, docs, reproduction, prior
  > history), synthesize a triage decision, have a skeptic critique it,
  > draft a final maintainer report, gate it on a human sign-off via a
  > JSON token file, and add a filesystem sensor that auto-launches the
  > gate when a token drops. Use `dagster-component search` +
  > `dagster-component schema <id>` to pick real components. 100%
  > components + YAML, no Python. Target issue: #30000 in
  > dagster-io/dagster.*

## Zero-to-something — we ran this test

We handed the prompt above to a fresh Claude Code session in an empty
project with only `CLAUDE.md` installed (no session history, no priming).
It ran ~55 tool calls in 8 minutes:

1. Read `CLAUDE.md` → understood the `dagster-component search / info /
   schema / add` workflow.
2. Searched the registry for each capability (GitHub MCP, agentic
   pipeline, human approval, filesystem sensor, asset job).
3. Called `dagster-component schema <id>` on each candidate to verify
   field shapes.
4. Composed 5 defs.yaml files: `openai_agent`, `agentic_pipeline`,
   `human_approval_gate`, `filesystem_monitor`, `asset_job`.
5. `uv run dagster definitions validate` → **passed**.

Notable divergences from the "target" defs.yaml in this walkthrough:

- It picked `OpenAIAgentComponent` (native OpenAI agent with MCP support)
  instead of `MCPToolPickerComponent` (a planner/synthesizer pair). Both
  land the GitHub MCP fetch correctly. `OpenAIAgentComponent` is more
  idiomatic for a single-vendor OpenAI shop; `MCPToolPickerComponent`
  demonstrates the tool-plan-then-synthesize pattern explicitly.
- It folded the skeptic + final-report steps into one `critique_loop`
  with `iterations: 1` (drafter = report writer, critic = skeptic). One
  step shorter, arguably cleaner.
- It exposed all 6 pipeline step outputs as assets, not just the last 3.

Real friction points it surfaced (fix candidates for CLAUDE.md +
component docs):

- `AgenticPipelineComponent`'s JSON schema is generic (`steps: array of
  object`) — the op shapes aren't reflected in the schema, so the
  assistant had to open `component.py` to nail down `synthesize` vs.
  `critique_loop` field keys.
- MCP `env: {...}` on stdio server config doesn't say "literal dict,
  no `${VAR}` interpolation" — assistant had to infer.
- `HumanApprovalGateComponent.upstream_asset_key` field docs don't
  clarify string vs. `AssetKey` shape.

None of these are blockers — the assistant landed a validating project
in one pass — but they're the cheapest UX wins for making
zero-to-something even sharper next round.

- **Live graph view mid-run.** Dagster's asset graph + run timeline show
  the same information Prefect's execution plan tab shows — nodes
  lighting up in order, per-node duration. Same picture, different
  chrome.

## When to pick `mcp_call` vs. `MCPToolPickerComponent`

The primary demo above uses the `mcp_call` op inline (one YAML, one
component). The other pattern — a peer `MCPToolPickerComponent` upstream
of an `AgenticPipelineComponent` via `upstream_asset` — still ships and is
still supported:

- **`mcp_call` op** — one YAML, deterministic tool call, `{text}` (source) +
  `{port_name}` (typed inputs) substitution in `tool_args`. Best when you
  know exactly which MCP tool to invoke.
- **`MCPToolPickerComponent`** — planner + synthesizer pair with a
  bounded tool list. Best when the planner LLM picks from N candidate
  tools per run (adaptive lookups, RAG-style tool selection). Emits
  per-tool assets so the trajectory is inspectable.

Both wire into the same downstream `HumanApprovalGate` +
`FilesystemMonitorSensor` shape.

## Extending

- **Snowflake / dbt downstream.** Point a
  [`snowflake_workspace`](https://dagster-component-ui.vercel.app/examples/snowflake_workspace)
  task or a `dbt_project` model at `maintainer_investigation_report`.
  The lineage graph extends automatically. That's the story Prefect
  structurally cannot tell.
- **Private repos.** Swap `MCPToolPickerComponent`'s server config from
  `stdio` (public MCP) to `http` with `headers_env: {Authorization:
  GITHUB_MCP_TOKEN}` for the GitHub Copilot MCP or an internal
  GitHub-Enterprise MCP. The rest of the graph is unchanged.
- **Slack sign-off.** Replace the token-file approval with a Slack
  modal that writes the approval token via webhook. Add
  [`slack_notification_sink`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/slack_notification_sink)
  as a peer of `report_approval` — the report + the approver's
  rationale get posted to `#maintainer-triage` on ship.
- **Warehouse persistence.** Add a `duckdb_table_writer` downstream
  of a small dict→DataFrame adapter, or drop the approval-side JSON
  files into an object store via `s3_sink` for a data-lake-friendly
  audit trail.
