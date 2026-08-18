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

The AI-agentic-orchestration pattern, Dagster-honest: **every specialist
is an asset, every decision is a materialization, the report is a
warehouse-ready artifact, and the whole graph lives next to your dbt
models.** No sidecar workflow tab.

## Architecture — 9-node "execution plan" as ONE YAML

The whole pipeline is a single `AgenticPipelineComponent` using the new
`mcp_call` op + typed named `inputs:` field. Every node has explicit
input port names (execution-plan graph shape); every node is a
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
      │  decision  (synthesize — 4 typed inputs, final        │
      │            routing decision node)                     │
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
`system_prompt`. Same execution-plan graph shape any typed-IO workflow
tool would draw — except on Dagster's substrate, every port output is
a queryable asset key.

## What each component does

- **`maintainer_investigation`** — [`AgenticPipelineComponent`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/assets/ai/agentic_pipeline). One YAML declares the whole 9-node execution plan. Uses **two v2 features**:
  - **`mcp_call` op** — the `intake` step calls the GitHub MCP directly (no planner LLM, just a deterministic MCP tool invocation). Config shape: `server: {type: stdio, command: [npx, -y, "@modelcontextprotocol/server-github"]}` (token from parent process env, no YAML inlining). For http-transport MCP with deferred secrets, use `headers_env: {Authorization: GITHUB_MCP_TOKEN}` — the env-var NAME lives in YAML, the value never does.
  - **`inputs:` field** — every node with more than one upstream declares `inputs: {port_name: {from: step_id} | {literal: value}}` for typed named multi-input joins. `preliminary` reads 6 named inputs, `skeptic` reads 2, `decision` reads 4, `report` reads 1. Each port becomes a `{port_name}` placeholder in the step's `prompt_template` + `system_prompt`.

  Every one of the 9 step outputs is a first-class Dagster asset key. `mir_intake` / `mir_repo_evidence` / `mir_preliminary` / `mir_decision` / `mir_report` (etc.) can all be depended on by downstream dbt models, warehouse writes, notification sinks, whatever. The plan is IN the org's data graph — not in a sidecar workflow tab.

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
for you — no manual re-materialize. Human sign-off happens via file
drop / Slack modal / Retool form / GitHub Action — anything that can
write a JSON token to the approval directory.

## What Dagster gives you that a plain workflow tool doesn't

- **Cross-boundary lineage.** `maintainer_investigation_report` is an
  asset key. Point a dbt model at it. Add a `duckdb_table_writer`
  downstream. Wire it into an Insights dashboard. The plan connects
  into the rest of the org's data graph — no seam between "agent
  workflow" and "data pipeline."
- **Deterministic + agent nodes in the same graph.** Nothing stops
  you from adding a `snowflake_workspace` task or a `dbt_project`
  component next to these — the graph doesn't care that some nodes
  are LLM calls and others are `EXECUTE TASK`. All materializations,
  all queryable the same way.
- **In-agent observability for free.** Each specialist emits an
  `AssetObservation` per LLM call with token counts, model, duration.
  Launchpad shows the tool-call sequence. Insights turns any numeric
  field into a queryable metric with alerts. No custom instrumentation.
- **Human sign-off as a first-class primitive.** `HumanApprovalGate` +
  filesystem sensor + auto-progression ships today. The gate's
  `approved` asset check surfaces in the UI, in Insights, and in any
  alerting pipeline — no bespoke wiring per approval.
- **Report retrieval + audit trail.** The `report` asset materializes
  with structured metadata + a markdown side file. Every past
  materialization is browsable in the UI — click any partition, see the
  exact triage decision from that run, the arbitrator reasoning, the
  cost. Full audit trail with zero custom code.

## Authoring the plan — coding-agent authored, in one prompt

The whole plan can be authored by Claude Code (or Cursor / Copilot) via
the community components CLI:

```bash
uvx --from dagster-community-components-cli dagster-component init
```

in a bare directory. That writes `CLAUDE.md` / `.cursorrules` /
`.github/copilot-instructions.md`. Then you tell your coding assistant
(component-agnostic — let it discover the right pieces):

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
  The lineage graph extends automatically — the agent output is just
  another asset in the org's data graph.
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
