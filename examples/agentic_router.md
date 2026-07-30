# Agentic Router — LLM Picks the Next Action, Human Gates the Exit

The **router-loop** pattern from Prefect's "agentic orchestration" pitch and the BPMN airline-baggage-loss diagrams — but every planner decision, every tool call, every human approval is a first-class Dagster asset.

The agent doesn't run a fixed pipeline. It **picks** the next tool from a bounded set based on prior tool outputs, may call several, and declares `done` when it's satisfied. Downstream, an LLM classifier routes each case to auto-approval or human review, and a compensation branch fires only for cases that need it.

## The asset graph

```
                        baggage_reports (CSV source)
                                    │
                              one row per case_id
                                    │
                        ┌───────────┴───────────┐
                        ▼           ▼           ▼
                       c1          c2          c3   (each is a partition)

  Per partition ────────────────────────────────────────────────────────────
  │
  ▼  ITERATIVE SUPERVISOR AGENT (loop, bounded tool set)
  ├── agent_step_1  ──► planner picks: query_baggage_db  ─► tool output
  ├── agent_step_2  ──► planner picks: organize_delivery ─► tool output
  ├── agent_step_3  ──► planner picks: inform_passenger  ─► tool output
  ├── agent_step_4  ──► planner: done (short-circuit)
  └── agent_step_5  ──► short-circuit — no-op
  │
  ▼  agent_final_answer  (synthesizer, reads all step trajectories)
  │      → "OUTCOME: resolved" or "OUTCOME: needs_human_review"
  │
  ▼  resolution  (LLM classifier)
  │      → status=<auto|human>; compensate=<t|f>; amount_usd=<n>; reason=<...>
  │
  ▼  human_review  (human_approval_gate)
  │      • setup script auto-writes token when status=auto_resolved
  │      • otherwise waits for a real human (or the sensor)
  │
  ├──►  notification_sent   (dataframe_to_csv → simulated CRM push)
  ├──►  compensation_paid   (dataframe_to_csv → simulated AP)
  └──►  case_audit          (duckdb append → "legacy warehouse")

  Plus: approval_watcher (filesystem_monitor, partition_mode=static_partition)
        → watches approvals/, on new *.json fires approve_and_process_job
        with partition_key from the filename.
```

## The bounded tool set

The router picks the next tool by **name** from this set (it can't invent tools or write code):

| Tool | Args | Purpose |
|---|---|---|
| `query_baggage_db` | `baggage_id` | Look up tracking status |
| `inquire_with_airport` | `airport_code` | Send a lookup to the last-known airport |
| `request_info_from_passenger` | `question` | Ask the passenger for more info |
| `send_care_voucher` | `passenger_id, amount_usd` | Issue a voucher |
| `organize_delivery` | `baggage_id, address` | Book a courier |
| `inform_passenger` | `passenger_id, message` | Post-back to the passenger |

Each tool is an LLM invocation with its own system_message — the "database" and "airport API" are pre-seeded with the 3 cases' ground-truth data. In production, swap the tool's `system_message` for a real API call by wrapping the tool's asset (or use `openai_agent` with real MCP servers).

## The three cases

```
c1: passenger=Alice,  flight=UA100 ORD→LHR, baggage_id=BAG-001,
    description="Checked bag never arrived at LHR arrivals."
    → DB says: in_transit_to_LHR, ETA 4h, delivery_address on file
    → Agent trajectory: query_db → organize_delivery → inform_passenger → done
    → OUTCOME: resolved

c2: passenger=Bob,    flight=AF200 JFK→CDG, baggage_id=BAG-002,
    description="Bag missing 3 days now — no updates from airline."
    → DB says: not_found, last scanned CDG 72h ago
    → Agent trajectory: query_db → inquire_with_airport → send_voucher → request_info → done
    → OUTCOME: needs_human_review

c3: passenger=Carol,  flight=DL300 SFO→JFK, baggage_id=BAG-003,
    description="Airline confirms bag is at JFK; need it delivered."
    → DB says: at JFK, address on file
    → Agent trajectory: query_db → organize_delivery → inform_passenger → done
    → OUTCOME: resolved
```

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_router_demo.sh \
  -o setup_agentic_router_demo.sh
bash setup_agentic_router_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY`. ~2 min first run.

The script walks the flow: source → router (5 step assets × 3 partitions + synthesizer) → resolution classifier → auto-writes approval tokens for `auto_resolved` cases → materializes gate + sinks. c1 and c3 auto-cascade end-to-end; c2 pends at the gate for a human.

## The "stateless gate + auto-approver" pattern

The `human_approval_gate` component doesn't know or care who wrote the token. In this demo, the setup script inspects each case's `resolution` output and writes the approval token itself for `auto_resolved` cases:

```python
# From the setup script:
if "status=auto_resolved" in routing:
    (approvals_dir / f"{case_id}.json").write_text(json.dumps({
        "approved": True,
        "approver": "auto_resolver (llm-driven, high-confidence)",
        "reason": routing,
    }))
```

The gate can't tell an LLM auto-approver from a human. That's the point: **anything that can write a JSON file can approve or reject**. Slack bot, Retool form, ServiceNow webhook, another Dagster asset, a compliance-officer signing off from their phone — same interface.

For cases where the LLM says `needs_human_review`, the script leaves the token unwritten. The gate materializes as `Failure(approval_pending)` and downstream is blocked until a real human (or the `approval_watcher` sensor picking up a manual drop) resolves it.

## Escape hatches: confidence + timeout

The BPMN example has two boundary events off the AI-Agent subprocess:

- **Low confidence** → route to Manual Processing
- **48h timeout** → same

Both fall out of Dagster's normal primitives here:

- **Low-confidence** is exactly the `resolution` LLM classifier writing `status=needs_human_review`. The setup script skips writing an auto-approval token; the gate pends; a human takes over via the sensor.
- **48h timeout** — add a `FreshnessPolicy(maximum_lag_minutes=2880)` to the `resolution` or `human_review` asset. When Dagster sees the case has been in the gate-pending state past the deadline, the freshness sensor fires an alert (in Dagster+) or triggers a job that writes a "timeout → manual" token that a downstream operator picks up.

## Watch the sensor cascade c2

While `dg dev` is running:

```bash
echo '{"approved": true, "approver": "you", "reason": "voucher sent, will re-open on passenger reply"}' \
  > /tmp/agentic-router-demo/approvals/c2.json
```

Within ~5s, `approval_watcher` (a [`filesystem_monitor`](../c/filesystem_monitor) with `partition_mode: static_partition`) fires. Filename `c2.json` maps to `partition_key=c2` via the `{file_stem}` template. It launches `approve_and_process_job` for that partition. `human_review[c2]` + `notification_sent[c2]` + `compensation_paid[c2]` + `case_audit[c2]` all materialize on their own. No manual click.

Reject instead:

```bash
echo '{"approved": false, "approver": "you", "reason": "escalate to airport ops"}' \
  > /tmp/agentic-router-demo/approvals/c2.json
```

Sensor fires → gate fails with `approval_rejected` → downstream stays untouched → the rejecter + reason are permanent metadata on `human_review[c2]`.

## Why this is different from a Python script

**Every planner decision is an asset.** `agent_step_2[c2]` is a named, addressable materialization. It carries `{iteration, done, tool, args, reasoning, tool_output}`. Six weeks later, "why did the agent send Bob a voucher instead of escalating?" opens as a UI click — you see the planner's exact reasoning at that step.

**Every tool call is a materialization.** No opaque "the agent did stuff." The trajectory is 5 assets per case; whichever step declared `done` short-circuits the rest as no-ops (still materialized, tagged as skipped).

**Bounded tool set is safety.** The planner picks by name. It can't invent a `delete_all_customer_data` tool. The YAML lists what's callable; anything else is a validation error at plan time.

**The gate is stateless.** LLM auto-approves the high-confidence cases by writing tokens; humans handle the rest. Same primitive, different writer. This scales — the router handles the routine 80%, the humans handle the interesting 20%, and neither knows about the other.

**Partitions are cases.** Add a new baggage report → add its `case_id` to `partition_values` → materialize. Same graph, new instance.

## Components used

| Layer | Component | Notes |
|---|---|---|
| Source | [`dataframe_from_csv`](../c/dataframe_from_csv) | unpartitioned; 3 rows = 3 cases |
| Router (planner loop + 6 tools) | [`iterative_supervisor_agent`](../c/iterative_supervisor_agent) | 5 step assets + synthesizer, partitioned by case_id |
| Classifier | [`llm_prompt_executor`](../c/llm_prompt_executor) | outputs `status; compensate; amount_usd; reason` |
| Human gate | [`human_approval_gate`](../c/human_approval_gate) | reads `<approval_dir>/<case_id>.json`; auto-approve from an assessor asset OR from a human — gate doesn't care |
| Sink: CRM push | [`dataframe_to_csv`](../c/dataframe_to_csv) | one file per case |
| Sink: AP compensation | [`dataframe_to_csv`](../c/dataframe_to_csv) | one file per case |
| Sink: legacy warehouse audit | [`duckdb_table_writer`](../c/duckdb_table_writer) | append; queryable by compliance |
| Auto-progression | [`filesystem_monitor`](../c/filesystem_monitor) | `partition_mode: static_partition`, `partition_key_template: {file_stem}` |
| Sensor job target | [`asset_job`](../c/asset_job) | named job over [gate, notification, compensation, audit] |

## Swap parts without touching the graph shape

- **More/fewer tools.** Add or remove entries in the `tools:` list — no code, YAML only. Planner picks from whatever's there.
- **A real database.** Swap the `query_baggage_db` tool's `system_message` for an actual SQL query, or replace with an `openai_agent` component using an MCP server for your DB.
- **A different LLM per tool.** Each tool spec accepts a `model:` override — search-capable model for retrieval tools, cheap model for math.
- **A different notification channel.** Swap `dataframe_to_csv` (notification) for `mongodb_writer` / `rest_api_writer` / `dataframe_to_kafka` / `dataframe_to_sfmc` — pick the real ticketing system.
- **A different legacy sink.** Swap `duckdb_table_writer` for `dataframe_to_snowflake` / `dataframe_to_mssql` / `dataframe_to_bigquery` / `dataframe_to_iceberg_table`. Same fields, different backend.

## Related walkthroughs

- **[agentic_orchestration.md](agentic_orchestration.md)** — the simpler linear "triage → draft → gate" flavor with two LLMs and a human gate. Read first if you're new to the pattern.
- **[rag_supervisor.md](rag_supervisor.md)** — planner + specialist agents, no human gate. Pure multi-agent story.
- **[rag_pipeline_dynamic.md](rag_pipeline_dynamic.md)** — one-component RAG with per-partition queries.
- **[rag_complete.md](rag_complete.md)** — end-to-end RAG (state-tracking + decomposed pipeline).
