# Design: RunMyJobs to Dagster migration tool

## Goal

Take a RunMyJobs installation's definitions and produce a loadable Dagster project plus a report that says exactly what needs a human. Output should be asset-oriented where the source says what data a job produces, not just a task-for-task port.

## Pipeline

```
extractor (CAR/XML | REST API | hand-written YAML)
   -> intermediate model (model.py)          # the only thing the rest of the tool sees
   -> plan per call (convert.py), cron.py    # deterministic; one plan, two emitters
   -> emitter: registry components (--output components) or generated Python (--output python)
   -> Dagster project + MIGRATION.md
   -> optional LLM pass for RedwoodScript/SQL (not built)
```

Keeping an intermediate model means a customer can run the extractor themselves and share only the model (or nothing), and new input formats need no changes to the mapper.

## Object mapping

| RunMyJobs object | Dagster | Status |
|---|---|---|
| Process definition, OS type | `ShellCommandAssetComponent` (or asset + subprocess) | built |
| Process definition, SQL type | `SqlAssetComponent` (resource named after the connection) or asset + SQLAlchemy (env var) | built; the shell path was run, the SQL path only loaded and dry-run |
| Process definition, RedwoodScript | loud stub (`RMJ-CAVEAT`), source kept as comments | built (flagged) |
| Process definition, SAP_AbapRun / SAP_BW_ProcessChain | components output: `SapJobTriggerAssetComponent` (needs review, untested against SAP); python output: loud stub that keeps the parameters | built |
| Process definition, System_Mail_Send | components output: `EmailSendAssetComponent` when the recipient is fixed, else a generated SMTP step with run config; attachments not sent | built |
| Process definition, other connector types | loud stub, flagged unmapped | built |
| Chain (steps, calls) | assets with deps between steps; parallel within a step; one job per chain | built |
| Submit frame (elements, time zone, end date) | one `CronScheduleComponent` (or `ScheduleDefinition`) per element; `end_date` enforced by the component | built for interval, daily, weekly, monthly, cron |
| Time window on a submit frame | hour range in cron (hour granularity) | built, flagged |
| Queue | pool on each asset | built; limits must be configured in Dagster |
| Maximum Starts, restart status handlers | `RetryPolicy` | built (interpretation flagged) |
| Step or chain precondition (time window, REL expression) | none yet; flagged | todo: skip logic or sensor |
| Other status handlers | flagged | todo |
| Nested chain calls | loud stub in place, order kept | built; todo: flatten |
| Events (file, raised) | sensors | todo |
| Named calendars, nested time windows | schedule filter | todo |
| Process outputs (tables/files) | `metadata.produces` | todo: real lineage / external assets |
| Locks, resources, process servers | n/a | out of scope for now |

## What is confirmed vs not

From public Redwood and RunMyJobs documentation (read via search summaries; the docs sites refused direct fetches):

- A chain is a series of steps; steps run in order and the calls inside a step run concurrently.
- Preconditions (time window and/or REL expression) are evaluated each time a step or chain runs, including restarts.
- Restart behavior is checked at the end of a step; status handlers can restart a step or the chain.
- Submit frames have one or more elements; multiple elements form a union of cycles; elements can be restricted by time windows, and frames can have an end date and a time zone.
- Time windows can include or exclude other windows.
- Objects export as XML inside CAR archives (export rule sets, "export with related objects"). A real `JobDefinition` sample is now in `tests/fixtures/car/` and the extractor is built against it. The type name for chains appears to be `JobChain`; not yet seen.
- A community report says exports do not carry schedules, so those must be recreated or extracted separately.
- The built-in REST interface uses HTTP Basic; the Inbound REST Extension uses Bearer or API key.

**Not confirmed** (do not trust until checked against a real export or the vendor reference):

- XML for chains, queues, time windows, events and submit frames (only `JobDefinition` has been seen).
- The exact REST endpoints for listing definitions.
- Exact semantics of "Maximum Starts".
- RedwoodScript's API surface (`jcsSession` and friends appear in vendor examples).

The intermediate YAML model is therefore our own shape, informed by the documented concepts, not a copy of Redwood's schema.

## Next steps

1. **Extend the extractor.** `JobDefinition` is done. Needed samples: a chain definition, a queue, a time window, a submit frame/schedule, an event, and a Shell-type and SQL-type job definition. The same for the REST API.
1a. **Run config for unresolved parameters (python output).** A chain parameter with no value in the export (for example `P_REGION`, or an email `To`) becomes a `dg.Config` field on each asset that needs it, default blank; a non-empty launch value overrides the exported default. Components output does not do this yet (the registry components take no run config); it keeps the note, and the value can be supplied as an environment variable. Partitions are not generated: they need the business key per chain from the customer.
2. **Preconditions and calendars.** Time-window preconditions become schedule windows or a skip check; named calendars become partition or schedule filters.
3. **Events to sensors.**
4. **Asset lineage.** Use declared process outputs to emit external source assets and real table-level deps.
5. **LLM pass** for RedwoodScript and complex SQL, import-time only, each result scored, with the threshold behavior used in `alteryx-to-dagster`.
6. **Execution checks.** Done: gate 4 dry-runs every asset (both outputs). The components output gets the same dry run through generated `Rmj*` subclasses of the registry shell, SQL and task-chain components (`components.py`), which add `dry_run` / `RMJ_DRY_RUN`. Subclasses must derive from the real classes (`_load_real_class`), because the package's exported names are lazy placeholders.
7. **Parallel-run harness.** Compare run results and timing between RunMyJobs and Dagster before cutover (the existing `runmyjobs_integration` component's reconciliation job is the starting point).
8. **Corpus.** Build a set of synthetic and public definitions to test against, as `alteryx-to-dagster` does with its 138 workflows.

## RunMyJobs MCP service (from Redwood's documentation, as supplied by the user)

RunMyJobs offers a regionally hosted MCP service (for example `https://mcp.use2.redwood.cloud` for Ohio) that LLM clients connect to with an API key and an `X-RMJ-Instance-ID: <customer>/<environment>` header. Read tools relevant to extraction: `list_job_definitions`, `get_job_definition`, `list_queues`, `list_process_servers`, `list_event_definitions`. No tool for listing chains, schedules or time windows appears in the catalog (unconfirmed whether `get_job_definition` returns chain structure).

Implications:

- **Live extraction path.** An agent connected to this MCP server could build the intermediate model from a live tenant with no export step, complementing the CAR extractor. It needs credentials, which we do not have.
- **Safety.** The same catalog includes destructive tools (`kill_job`, `delete_job`, `delete_event_definition`, `create_credential`, `stop_process_server`, and others). A migration agent should be restricted to the list/get tools and never be allowed to submit, cancel or delete.
- **Parity checks.** `list_jobs` and `get_job` expose run history, which is what a side-by-side comparison needs.
- **Gaps.** Schedules (submit frames), chains and time windows are not in the listed tools, so the CAR export, the built-in REST `fetch`, and the parity data still matter.

## No-sample strategy

If a customer will not share any export, extraction runs through their own MCP server with read-only tools (`reference/mcp-extraction.md`). The agent writes the intermediate model, `validate-model` checks it, and raw responses are saved. That first run supplies the real response formats; they then become fixtures for a deterministic extractor, exactly as the CAR sample did for `extract_car.py`.
