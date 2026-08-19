# Maintainer Investigation Room v3 — every new AI-agentic primitive, one pipeline

The **comprehensive** MIR — same "triage a real GitHub issue" pattern as [`maintainer_investigation_room.md`](maintainer_investigation_room.md), rebuilt to exercise every AI-agentic primitive shipped in v0.10.69–v0.10.73:

- **`per_step_ops: true`** — one op per step, visible in the Runs page + native per-op retry (no more "restart the whole partition to retry step 6")
- **`fastmcp` transport** — GitHub MCP via FastMCP v2 client
- **`tool_use_loop` op** — open-ended agent tool-use loop (LangGraph-shape) as ONE Dagster asset with the full tool-call trace in metadata
- **`handoff` op** — bring your existing LangGraph state-machine code as ONE step of the pipeline; adjacent steps stay Dagster-native
- **`debate` op** — 3 skeptic proposers + arbitrator
- **`critique_loop` op** — drafter + critic iterative refinement
- **`SlackApprovalGateComponent`** — Slack quorum HITL (falls back to file-drop when unset)
- Existing: `PartitionedAssetLauncherJobComponent`, `HumanApprovalGateComponent`, `FilesystemMonitorSensorComponent`, `AssetJobComponent`

**Setup script:** [`setup_maintainer_investigation_room_v3_demo.sh`](./setup_maintainer_investigation_room_v3_demo.sh).
**LangGraph target:** [`mir_v3_langgraph_reproducer.py`](./mir_v3_langgraph_reproducer.py) — user's own project code, invoked via `handoff` op.
**Cost:** ~$0.05 per full triage (tool_use_loop makes several MCP calls; LangGraph handoff does 4 LLM calls; critique_loop iterates twice).

## What this demo proves

The Prefect co-worker who ran the earlier MIR demo asked two things:
1. *"Is each node in the graph a single LLM call?"* — accurate for the earlier demo, but a limitation.
2. *"Only one op shows in the run view — what about per-step retry?"* — accurate; the earlier demo was one `@multi_asset`.

MIR-v3 answers both directly:

1. **No — steps can be arbitrarily complex.** `tool_use_loop` runs 5-15 internal LLM+tool calls; `handoff` invokes a whole LangGraph state machine; `debate` runs N+1 calls; `critique_loop` runs 2N+1. Each still becomes ONE Dagster asset with the internal cost/latency/trajectory rolled up into metadata.
2. **Per-step ops with `per_step_ops: true`.** 11 step_keys in the Runs page, each with independent retry via Dagster's native re-execution. dbt-style resume, one YAML.

## Architecture (11 assets from one YAML)

```
launch_mir_triage_v3 (job — config-driven entry, dynamic partition register)
      │
      ▼  {owner}/{repo}#{issue_number}
┌──────────────────────────────────────────────────────────────────────┐
│ AgenticPipelineComponent (per_step_ops: true → graph_multi_asset)     │
│                                                                       │
│  ┌───────────────────────────────┐                                    │
│  │ mir_intake (mcp_call)          │ fastmcp → GitHub MCP → get_issue  │
│  └────────────┬──────────────────┘                                    │
│               │                                                       │
│    ┌──────────┼──────────┬─────────────────┐                          │
│    ▼          ▼          ▼                 ▼                          │
│  ┌────┐   ┌───────┐  ┌───────┐         ┌───────┐                      │
│  │repo│   │reprod-│  │docs_  │         │history│                      │
│  │_ev │   │uction │  │ev     │         │_ev    │                      │
│  │(⭐  │   │(⭐    │  │(llm_  │         │(llm_  │                      │
│  │tool│   │handoff│  │call)  │         │call)  │                      │
│  │_use│   │→Lang- │  │       │         │       │                      │
│  │_loo│   │Graph) │  │       │         │       │                      │
│  │p)  │   │       │  │       │         │       │                      │
│  └──┬─┘   └───┬───┘  └───┬───┘         └───┬───┘                      │
│     │        │          │                 │                          │
│     └────────┴────┬─────┴─────────────────┘                          │
│                   ▼                                                   │
│  ┌─────────────────────────────────────┐                              │
│  │ mir_preliminary (synthesize)         │ 5 typed named inputs joined │
│  └────────────┬─────────────────────────┘                             │
│               ▼                                                       │
│  ┌─────────────────────────────────────┐                              │
│  │ mir_skeptic_debate (⭐ debate)        │ 3 skeptics + arbitrator     │
│  └────────────┬─────────────────────────┘                             │
│               ▼                                                       │
│  ┌─────────────────────────────────────┐                              │
│  │ mir_decision (synthesize)            │ 4 typed named inputs        │
│  └────────────┬─────────────────────────┘                             │
│               ▼                                                       │
│  ┌─────────────────────────────────────┐                              │
│  │ mir_report (⭐ critique_loop)         │ drafter + critic, ≤2 iters, │
│  │                                       │ early-stop @ score ≥ 85     │
│  └────────────┬─────────────────────────┘                             │
└───────────────┼───────────────────────────────────────────────────────┘
                │
                ▼
      ┌───────────────────────────────────────┐
      │ mir_report_approval_slack             │  ← ⭐ SlackApprovalGate
      │ (posts + polls, writes token on       │      (optional — falls back
      │  Slack quorum)                        │       to file-drop when
      └────────────┬──────────────────────────┘       env vars unset)
                   │
                   ▼
      ┌───────────────────────────────────────┐
      │ mir_report_approval                   │  ← HumanApprovalGate
      │ (asset check `approved` gates ship)   │
      └────────────┬──────────────────────────┘
                   │
                   ▼
      ┌───────────────────────────────────────┐
      │ ship_mir_report_v3_job                │  ← FilesystemMonitorSensor
      │ (fires when JSON token drops)         │      auto-fires this
      └───────────────────────────────────────┘
```

## The Runs page view (per_step_ops payoff)

Under the default `per_step_ops: false` shape, materializing this pipeline shows **one op** (`mir_pipeline`) in the Runs page. Every internal step is a log line inside that op, and retry-from-a-failed-step means restarting the whole pipeline.

Under `per_step_ops: true`, the same triage shows **11 step_keys**:

```
mir_pipeline.mir_ingest             ← source + ctx build
mir_pipeline.mir_intake             ← GitHub MCP call
mir_pipeline.mir_repo_evidence      ← tool_use_loop (5-15 internal calls)
mir_pipeline.mir_reproduction       ← handoff → LangGraph (4 nodes)
mir_pipeline.mir_docs_evidence      ← simple llm_call
mir_pipeline.mir_history_evidence   ← simple llm_call
mir_pipeline.mir_preliminary        ← 5-input synthesize
mir_pipeline.mir_skeptic_debate     ← 3 proposers + arbitrator (4 calls)
mir_pipeline.mir_decision           ← 4-input synthesize
mir_pipeline.mir_report             ← critique_loop (5 calls: draft + 2×(critique + revise))
mir_pipeline.mir_extract            ← yield the 9 asset outputs from final state
```

Dagster's normal per-op retry works. `mir_report` failed at iteration 2 of the critique_loop? Re-execute from that op — the upstream state is on the IO manager, everything before it doesn't re-run. dbt-style resume without splitting into 9 separate `@asset` decorators.

Trade-off: state dict serializes between ops via the IO manager (~50-200KB per hop for text-heavy pipelines). Real but not painful.

## Try it

```bash
# Required
export OPENAI_API_KEY=sk-...
export GITHUB_PERSONAL_ACCESS_TOKEN=$(gh auth token)  # or a classic PAT

# Optional (falls back to file-drop HITL when unset)
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_APPROVER_USER_IDS="U1234ALICE,U5678BOB"
export SLACK_CHANNEL="#dagster-triage"
export REQUIRED_APPROVERS=1

# Optional — target a different issue (default 30000)
export DAGSTER_ISSUE_NUM=25000

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_maintainer_investigation_room_v3_demo.sh \
  -o setup_maintainer_investigation_room_v3_demo.sh
bash setup_maintainer_investigation_room_v3_demo.sh
```

Requirements: `uv`, `npx` (Node — GitHub MCP), `OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`. ~90-120s first run. ~$0.05 in tokens.

## What each new primitive demonstrates

### `tool_use_loop` on `mir_repo_evidence`

Instead of one LLM call ("summarize what code is likely relevant"), the specialist gets GitHub MCP tools and **iteratively explores**. Typical trajectory:

```
iter 1: search_code(query="io_manager NoneType") → 3 hits
iter 2: get_file_contents(path="python_modules/dagster/dagster/_core/storage/io_manager.py")
iter 3: search_code(query="load_input None") → 1 hit
iter 4: get_file_contents(path=...) → confirms
iter 5: finalize(answer="io_manager.load_input returns None when...")
```

5 real MCP tool calls, one Dagster asset materializes. The full trace lives in the asset's `tool_call_trace` metadata — inspectable in the Materializations tab. Cost + latency + tool-call count roll up.

### `handoff` on `mir_reproduction`

The reproduction analysis is a **LangGraph state machine** the customer wrote in their own project ([`mir_v3_langgraph_reproducer.py`](./mir_v3_langgraph_reproducer.py)):

```
plan → hypothesize → verify → refine → finalize
```

The `handoff` op imports the module, calls `run_reproduction_analysis(issue_facts=...)`, materializes the returned `final_answer` as the Dagster asset. Framework's internal 5-node trace lives in the asset's `framework_result.trajectory` metadata. Adjacent Dagster steps (docs specialist, history specialist, synthesize, debate) don't know or care that this step used LangGraph — they consume text via the standard `text` payload contract.

**Point:** you can bring existing LangGraph / AutoGen / CrewAI / DSPy code without rewrite. Dagster is the harness at the pipeline level; the framework is one node.

### `debate` on `mir_skeptic_debate`

Instead of ONE skeptic critiquing the preliminary triage, run **three** skeptics (Bayesian / classification / ownership angle) in parallel + an arbitrator picks the strongest. 4 LLM calls total; one asset materializes with the winner's text as `text`, all 3 proposals + arbitrator reasoning in metadata.

### `critique_loop` on `mir_report` (with `until_score_gte: 85`)

Drafter writes the maintainer-facing report; critic reviews for clarity / actionable-next-step / honest-uncertainty; drafter revises. Capped at 2 iterations = 5 LLM calls max, BUT stops early when the critic scores the draft ≥ 85/100 (skips the revise step for that iteration). Real-world triage drafts are often good on first pass → ~40% call-count savings on average, still capped at 2 iterations for hard cases. One asset with the FINAL revised text; full transcript + `final_score` + `stop_reason` in metadata.

### `SlackApprovalGateComponent`

Slack quorum HITL on the final report — see [`slack_approval_gate.md`](slack_approval_gate.md) for setup details. Falls back to file-drop when `SLACK_BOT_TOKEN` is unset so the demo still runs offline.

## Composition patterns illustrated

- **Config → partition → materialization** (launcher → dynamic partition → per-partition pipeline)
- **Fan-out with typed named inputs** (4 specialists reading `issue_facts: {from: intake}`)
- **Multi-input synthesize** (5-input preliminary, 4-input decision)
- **Adjacent framework hand-offs** (LangGraph inside a pipeline that has MCP + native ops around it)
- **Iterative refinement inside a pipeline step** (critique_loop)
- **Per-request debate + arbitration** (debate)
- **Slack-driven quorum** composing with existing asset-check gate

## Comparing MIR-v1 vs MIR-v3

|  | MIR-v1 ([`maintainer_investigation_room.md`](maintainer_investigation_room.md)) | MIR-v3 (this) |
|---|---|---|
| Ops per pipeline run | 1 (`mir_pipeline`) | 11 (per_step_ops) |
| Retry granularity | Whole partition | Per-op via Dagster's native re-execution |
| Total LLM calls | ~9 (one per specialist + join) | ~20-30 (tool_use_loop iterates, debate has 4 proposers+arbitrator, critique_loop iterates, LangGraph handoff has 4 nodes) |
| Framework composition | None | LangGraph via `handoff` on one step |
| Tool-use pattern | None (single MCP call for intake only) | Full agentic tool-use loop on repo_evidence |
| HITL | File-based approval only | Slack quorum + file-based fallback |
| Cost per triage | ~$0.02 | ~$0.05 |
| Setup complexity | Modest | Same YAML shape + langgraph pip dep + optional Slack setup |

Both are valid — MIR-v1 for a lean happy-path demo, MIR-v3 for showing the full agentic primitive palette.

## Related walkthroughs

- **[maintainer_investigation_room.md](maintainer_investigation_room.md)** — MIR-v1 (simpler, one op per pipeline, file-drop HITL)
- **[slack_approval_gate.md](slack_approval_gate.md)** — Slack HITL walkthrough on its own
- **[local_ai_ab.md](local_ai_ab.md)** — provider A/B pattern (can be added as a companion for evaluating each specialist's model choice)
- **[agentic_pipeline.md](agentic_pipeline.md)** — the base AgenticPipelineComponent (all 8 ops documented)
