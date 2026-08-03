# Agentic Router — One Asset per Case, Steps as Ops, Multiple Downstream Branches

The **router-loop** pattern from BPMN "agentic orchestration" diagrams, done the Dagster-honest way:

- **The agent is one asset per case**, static-partitioned by case_id.
- **The ReAct loop iterations are ops** — visible in the run view, not the asset graph.
- **Each branch is its own asset** with its own `DynamicPartitionsDefinition`. When the router picks a branch for a case, it registers that case_id on that branch's dynamic partition set. So each branch's UI shows ONLY the case keys the router actually picked for it — no sparse-empty slots, no red failures for "wrong branch" partitions.
- **Downstream sinks share the branch's dynamic partition set** — same story: only the cases that flowed through.

The alternative shape — `iterative_supervisor_agent` — declares max_iterations assets per case. That works when you want per-step re-runs and per-step lineage, but for a router demo it clutters the graph and misrepresents assets (loop iterations are compute, not state). This walkthrough uses the cleaner shape.

## Architecture

```
baggage_reports  (CSV source, unpartitioned — wired into lineage)
       │
       │  filtered by case_id per partition
       ▼
baggage_triage_agent[c1,c2,c3]        ← ONE graph-backed asset per case
       │  static partitions (case_id)   (llm_multi_path_router)
       │
       │  Inside the run view (not the asset graph):
       │    build_task → plan_step_1 → plan_step_2 → … → classify_and_register
       │    (each step is an op with its own logs; final op registers case_id
       │     on each picked branch's DynamicPartitionsDefinition)
       │
       ├──►  delivery_request      ─►  courier_booked        (dataframe_to_csv)
       │       DynamicPartitionsDef("delivery_request_cases")
       │       shows ONLY the case_ids the router picked delivery for
       │
       ├──►  voucher_issued        ─►  compensation_paid     (dataframe_to_csv)
       │       DynamicPartitionsDef("voucher_issued_cases")
       │
       └──►  escalation            ─►  human_review          (human_approval_gate)
               DynamicPartitionsDef("escalation_cases")            ▼
                                                      escalation_audited (duckdb append)

Plus: approval_watcher (filesystem_monitor, partition_mode=dynamic_partition)
      → watches approvals/, on new *.json launches approve_and_process_job
      with partition_key=<file stem>. Auto-progresses stuck escalations.
```

**The key move**: each branch has its own `DynamicPartitionsDefinition`. The router asset, at the end of its ReAct loop, calls `context.instance.add_dynamic_partitions("<branch>_cases", [case_id])` for each picked branch. So a branch's UI shows only the case_ids the router actually registered on it — no sparse empty slots, no red failures for "wrong branch" partitions.

Downstream sinks reference the SAME dynamic partition set as their upstream branch, so they inherit the same clean per-partition view.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_demo.sh \
  -o setup_agentic_router_demo.sh
bash setup_agentic_router_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY`. ~2 min first run.

## What each ReAct step looks like in the UI

Open `http://localhost:3000` → click any partition of `baggage_triage_agent` → "View run" → the timeline shows:

```
build_task
  → plan_step_1  (planner: "call query_baggage_db(BAG-001)" + tool output)
  → plan_step_2  (planner: "call organize_delivery(...)" + tool output)
  → plan_step_3  (planner: "call inform_passenger(...)" + tool output)
  → plan_step_4  (short-circuit — prior step declared done)
  → plan_step_5  (short-circuit)
  → classify_and_emit  (LLM picks branches from {delivery_request, voucher_issued, escalation})
```

Each op has its own logs: planner reasoning, tool called, tool output. The full trajectory also lands in every emitted asset's materialization metadata (`trajectory` markdown, `n_iterations`, `summary`).

## The three cases

```
c1: Alice, UA100 ORD→LHR, BAG-001 — DB says: in_transit_to_LHR, address known
    → agent: query_db → organize_delivery → inform_passenger → done
    → emits: delivery_request

c2: Bob, AF200 JFK→CDG, BAG-002 — DB says: not_found (72h since scan)
    → agent: query_db → inquire_with_airport → send_voucher → request_info → done
    → emits: voucher_issued + escalation (both apply)

c3: Carol, DL300 SFO→JFK, BAG-003 — DB says: awaiting_delivery at JFK
    → agent: query_db → organize_delivery → inform_passenger → done
    → emits: delivery_request (sometimes + escalation if classifier judges monitoring needed)
```

The classifier decides per case which branches apply. c2 legitimately fires TWO branches — voucher was sent AND the case still needs human review. Multi-output branching handles that naturally; you couldn't express it with a single-output chain.

## The stateless gate pattern

The `human_review` asset (a [`human_approval_gate`](../c/human_approval_gate)) reads `approvals/<case_id>.json`. In the setup script, an "auto-assessor" step inspects each escalation's trajectory and auto-writes a token when the outcome is clear (`n_iterations >= 3` = agent used the tools well). Otherwise the token is unwritten and the gate pends.

The gate can't tell a human writer from an LLM writer — that's the point. Anything that can write a JSON file can approve or reject. Slack bot, Retool form, ServiceNow webhook, another Dagster asset — same interface.

For the cases the auto-assessor didn't approve, the `approval_watcher` sensor waits for a real human to drop a token. When one lands, it launches the gate + audit for that specific partition.

## Watch the sensor cascade

While `dg dev` is running, drop a token for any escalated case that's still pending:

```bash
echo '{"approved": true, "approver": "you", "reason": "verified with airport ops"}' \
  > /tmp/agentic-router-demo/approvals/c3.json
```

Within ~5s, `approval_watcher` fires → launches `approve_and_process_job` with `partition_key=c3` → `human_review[c3]` + `escalation_audited[c3]` materialize on their own.

Reject:

```bash
echo '{"approved": false, "approver": "you", "reason": "not enough evidence — escalate to airport ops"}' \
  > /tmp/agentic-router-demo/approvals/c3.json
```

Sensor fires → gate fails `approval_rejected` → `escalation_audited[c3]` stays untouched. The rejecter + reason are permanent metadata on `human_review[c3]`.

## Why this shape beats the alternatives

**Vs 5 fixed step assets per case.** Cleaner asset graph (1 per case, not 5). Steps are visible where they belong — in the run view for that partition, not in the global asset catalog.

**Vs a monolithic single asset with all downstream branches wired.** Multi-output means each sink asset shows only the cases that flowed through it — the graph tells the truth per partition. Wiring all downstream from one output would force every sink to run for every case and either no-op or error.

**Vs a hand-written Python script.** The bounded tool set is safety — the planner picks BY NAME from a YAML-declared list. It can't invent a `delete_all_customer_data` tool. The task template + tool YAMLs are what an operator reviews before adding a new tool; a script would require a code review.

**Vs `iterative_supervisor_agent`.** Same primitive idea (ReAct loop, bounded tools), different asset semantics. Use `iterative_supervisor_agent` when you want each step as its own asset for per-step re-runs; use `llm_multi_path_router` (this walkthrough) when the agent is a single unit of work with multiple branches.

## Components used

| Layer | Component | Notes |
|---|---|---|
| Source | [`dataframe_from_csv`](../c/dataframe_from_csv) | Unpartitioned; one row per case; properly wired via `upstream_asset_key`. |
| Router | [`llm_multi_path_router`](../c/llm_multi_path_router) | **The new primitive.** One graph-backed asset per case; ReAct steps as ops; multi-output branches. |
| Sink: courier booking | [`dataframe_to_csv`](../c/dataframe_to_csv) | Downstream of `delivery_request` only. |
| Sink: compensation | [`dataframe_to_csv`](../c/dataframe_to_csv) | Downstream of `voucher_issued` only. |
| Human gate | [`human_approval_gate`](../c/human_approval_gate) | Downstream of `escalation` only. |
| Sink: legacy audit | [`duckdb_table_writer`](../c/duckdb_table_writer) | Downstream of the human gate; append. |
| Sensor | [`filesystem_monitor`](../c/filesystem_monitor) | `partition_mode: static_partition`; auto-progresses on token drop. |
| Sensor job target | [`asset_job`](../c/asset_job) | Named job over `[human_review, escalation_audited]`. |

## Swap parts without touching the graph shape

- **More tools.** Add YAML entries to `tools:`. Planner picks from whatever's there.
- **More branches.** Add YAML entries to `outputs:` (and downstream assets to consume them).
- **A different LLM per step.** The `model:` field is shared across planner + tools + classifier; per-tool overrides are on the `iterative_supervisor_agent` sibling (add same to this one if needed).
- **Real APIs instead of LLM-simulated tools.** Each tool's `system_message` currently pre-seeds ground-truth data (baggage DB rows, airport records) so the demo is self-contained. In production, wrap the tool's system_message around a call to your actual DB / API / service, or use [`openai_agent`](../c/openai_agent) with real MCP servers.
- **A different legacy audit sink.** Swap `duckdb_table_writer` for `dataframe_to_snowflake` / `dataframe_to_mssql` / `dataframe_to_bigquery` / `dataframe_to_iceberg_table`.

## See also

- **[agentic_orchestration.md](agentic_orchestration.md)** — simpler linear shape: `triage_agent → draft_response → human_approval_gate → sinks`. Start here if you're new to agent + human patterns.
- **[rag_supervisor.md](rag_supervisor.md)** — planner + parallel specialist agents (single retrieval isn't enough). No human gate.
- **[rag_pipeline_dynamic.md](rag_pipeline_dynamic.md)** — one-component RAG with per-partition dynamic queries.
- **[rag_complete.md](rag_complete.md)** — full RAG stack, state-tracking + decomposed pipeline.
