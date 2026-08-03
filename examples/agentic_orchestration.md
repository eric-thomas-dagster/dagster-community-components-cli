# Agentic Orchestration — Agents + Humans + Legacy Systems

The full-stack pattern: **multiple autonomous AI agents, a human-in-the-loop gate, and post-back to legacy systems, all in one graph**. Every step is an asset. Every asset is partitioned per case. Every decision — agent classification, agent draft, human approve/reject — is a replayable materialization with metadata.

This is the shape that Prefect describes as "agentic orchestration." Dagster's take: **treat each agent's output and each human decision as first-class state**. No black-box flow, no lost audit trail, no "why did the agent do X six weeks ago?"

## Architecture

```
                              tickets (CSV source)
                                    │
                              one row per ticket_id
                                    │
                                    ▼
                          triage_agent[ticket_id]     ← LLM #1
                          (llm_prompt_executor)         classify:
                                    │                   category + urgency
                                    ▼
                          draft_response[ticket_id]   ← LLM #2
                          (llm_prompt_executor)         write customer-facing draft
                                    │
                                    ▼
                          response_approved[ticket_id]  ← HUMAN GATE
                          (human_approval_gate)           reads approvals/<id>.json
                                    │                     3 outcomes:
                    ┌───────────────┼────────────────┐    - missing → approval_pending
                    ▼                                ▼    - {approved: false} → rejected
        response_sent[ticket_id]        audit_log[ticket_id]  - {approved: true} → pass
        (dataframe_to_csv)              (duckdb_table_writer)
        → ticketing system              → legacy warehouse

                    ┌── approval_watcher (filesystem_monitor)
                    │   watches approvals/, on new *.json:
                    │     partition_key = file stem (t3.json → "t3")
                    │     RunRequest(partition_key=t3, job=approve_and_send_job)
                    └── job materializes response_approved[t3] +
                        response_sent[t3] + audit_log[t3] in one run.
```

## What each component is doing

- **`tickets`** — [`dataframe_from_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_csv). Unpartitioned. One row per ticket with `ticket_id`, `customer`, `subject`, `body`.
- **`triage_agent[ticket_id]`** — [`llm_prompt_executor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/llm_prompt_executor) partitioned by `ticket_id`. Templated prompt over the row's `customer`/`subject`/`body`. Classifies each ticket into `category=<technical|billing|feature_request|other>; urgency=<low|medium|high>`.
- **`draft_response[ticket_id]`** — another `llm_prompt_executor` with a specialist system prompt. Reads both the ticket body AND the triage output, drafts a 3-5 sentence reply. Never commits to specific ETAs or dollar amounts — flags those for human review.
- **`response_approved[ticket_id]`** — [`human_approval_gate`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/human_approval_gate). On materialize, reads `approvals/<ticket_id>.json`:
  - **missing** → `Failure(status=approval_pending)`. Downstream blocked.
  - **`{"approved": false, ...}`** → `Failure(status=approval_rejected)`. Downstream blocked.
  - **`{"approved": true, ...}`** → passes upstream payload through, records `approver`/`reason`/`approved_at` in materialization metadata. Downstream unblocked.
- **`response_sent[ticket_id]`** — [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-community-components/tree/main/assets/sinks/dataframe_to_csv). Writes to `sent_responses/sent_response_<ticket_id>.csv`. Stand-in for a REST POST to Zendesk/Freshdesk/ServiceNow.
- **`audit_log[ticket_id]`** — [`duckdb_table_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/duckdb_table_writer). Appends to `audit/audit.duckdb`. Stand-in for the legacy analytics warehouse where compliance queries the trail.
- **`approval_watcher`** — [`filesystem_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/filesystem_monitor) sensor. Polls the approvals dir every 5s. New `t3.json` → `partition_key=t3` → launches `approve_and_send_job` for that partition.
- **`approve_and_send_job`** — [`asset_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/schedules/asset_job). Named job over `[response_approved, response_sent, audit_log]` so the sensor has a discrete target to launch.

## Run

```bash
export OPENAI_API_KEY=sk-...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_agentic_orchestration_demo.sh \
  -o setup_agentic_orchestration_demo.sh
bash setup_agentic_orchestration_demo.sh
```

Requirements: `uv`, `OPENAI_API_KEY` (or set `llm_provider: anthropic` etc. in the yaml). ~1 min first run.

## The three-ticket demo

The setup script seeds three synthetic tickets:

| ticket_id | customer | subject |
|---|---|---|
| t1 | acme corp | Password reset stuck |
| t2 | widgets inc | Feature request: dark mode |
| t3 | globex | Invoice discrepancy on Feb bill |

It walks the flow:

1. **Both agents run** per ticket (`triage_agent[t1..t3]`, `draft_response[t1..t3]`).
2. **First attempt at the gate — all 3 fail** `approval_pending`. No tokens exist yet. This is the "waiting on a human" state; downstream doesn't run.
3. **Drops tokens for t1 (approved) and t2 (rejected).** Leaves t3 untouched.
4. **Re-materializes the gate.** t1 succeeds. t2 fails `approval_rejected`. t3 fails `approval_pending`.
5. **Materializes downstream for t1** — `response_sent[t1]` writes the CSV, `audit_log[t1]` appends to DuckDB.
6. Prints final state: 1 sent, 1 in audit, t2/t3 blocked.

Then it points at the sensor for the auto-progression story:

```bash
echo '{"approved": true, "approver": "eric", "reason": "credited $250"}' \
  > $PROJECT_ABS/approvals/t3.json
```

`approval_watcher` sees the new token within 5s → launches `approve_and_send_job` with `partition_key=t3` → `response_approved[t3]` + `response_sent[t3]` + `audit_log[t3]` all materialize in one run. No manual click.

## Why this is different from a Python script or a flow tool

**Every agent output is an asset.** `triage_agent[t2]` is a named, addressable materialization. Six weeks later when someone asks "why did we classify t2 as feature_request?", you open the asset in the UI and see the exact prompt, model, and response for that ticket.

**Every human decision is metadata.** `response_approved[t1]`'s materialization metadata carries `{"approver": "eric@dagsterlabs.com", "reason": "...", "approved_at": "..."}`. That's the audit trail — no Slack thread archaeology, no "who approved this?"

**Approval is stateless — the token file IS the state.** The `human_approval_gate` component doesn't care who wrote the token. A bash `echo > .json` works. A Slack bot works. A Retool form works. A ServiceNow webhook works. Another Dagster asset can even write the token (auto-approve on high-confidence). The gate can't tell the difference.

**Partitions are cases.** `--partition t3` is enough. No custom "for ticket in tickets: run_flow(ticket)" loop. Re-run one case, backfill all cases, retry only the rejected ones — all built in.

**Legacy is just another sink.** `audit_log` writes to DuckDB in the demo; swap for `dataframe_to_snowflake` / `dataframe_to_mssql` / `dataframe_to_bigquery` — the graph shape doesn't change, only the sink yaml does.

## Components used

| Layer | Component | Notes |
|---|---|---|
| Source | [`dataframe_from_csv`](../c/dataframe_from_csv) | unpartitioned; tickets.csv is the ticket set |
| Agent 1 (triage) | [`llm_prompt_executor`](../c/llm_prompt_executor) | partitioned by ticket_id |
| Agent 2 (draft) | [`llm_prompt_executor`](../c/llm_prompt_executor) | partitioned by ticket_id |
| Human gate | [`human_approval_gate`](../c/human_approval_gate) | **the new primitive** |
| Sink 1 (ticketing) | [`dataframe_to_csv`](../c/dataframe_to_csv) | stand-in for a REST POST |
| Sink 2 (legacy warehouse) | [`duckdb_table_writer`](../c/duckdb_table_writer) | stand-in for Snowflake/BigQuery |
| Auto-progression | [`filesystem_monitor`](../c/filesystem_monitor) | `partition_mode: static_partition` — polls approvals/, launches per-partition on token drop |
| Sensor job target | [`asset_job`](../c/asset_job) | one job over [gate, sent, audit] so the sensor has a discrete target |

## Swap parts without touching the graph shape

**Swap the LLM.** Change `llm_prompt_executor`'s `provider` from `openai` to `anthropic` / `gemini` / any OpenAI-compatible endpoint. Nothing else changes.

**Swap the audit sink.** Change `duckdb_table_writer` for `dataframe_to_snowflake` / `dataframe_to_mssql` / `dataframe_to_bigquery` / `dataframe_to_iceberg_table`. Same fields, different destination.

**Swap the ticketing sink.** `dataframe_to_csv` → `rest_api_writer` / `mongodb_writer` / `dataframe_to_kafka` — depends on where the ticketing system reads from.

**Swap the source.** `dataframe_from_csv` → `dataframe_from_table` (poll a DB) / `s3_monitor` / `sftp_ingestion` / `mailbox_ingestion`. The upstream shape stays "one row per case."

**Chain multiple gates.** Draft → `legal_approved` (gate) → `svp_approved` (gate) → send. Each gate has its own approval directory + its own sensor. Two-step approvals fall out of composing components.

**Auto-approve when confident.** Add an assessor asset upstream of the gate that writes the approval token itself when the model's confidence exceeds a threshold. The gate can't tell if the token came from an LLM auto-approver or from a human. Below the threshold, the token doesn't get written and it waits for a human.

## See also

- **[rag_supervisor.md](rag_supervisor.md)** — planner LLM + specialist agents, no human gate. The pure multi-agent story.
- **[rag_pipeline_dynamic.md](rag_pipeline_dynamic.md)** — one-component RAG with dynamic per-partition queries. The "queries as addressable state" story.
- **[rag_complete.md](rag_complete.md)** — end-to-end RAG with two parallel paths (state-tracking + decomposed pipeline). No agent + human loop, but similar partition-per-case discipline.
