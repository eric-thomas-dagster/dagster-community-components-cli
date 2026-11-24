# Extracting the model through the RunMyJobs MCP service

Use this when there is no CAR export. The MCP server is the customer's own (regional URL, API key, `X-RMJ-Instance-ID: <customer>/<environment>`). The response formats were not available when this was written, so the procedure is built to work from whatever comes back and to record what it saw.

## Allowlist (read-only)

Allowed: `get_version`, `list_job_definitions`, `get_job_definition`, `list_queues`, `get_queue`, `get_queue_provider`, `list_process_servers`, `get_process_server`, `list_event_definitions`, `get_event_definition`, `list_jobs`, `get_job`, `get_job_definition_alert_source`.

Never call during a migration: `submit_job`, `runnow_job`, `cancel_job`, `cancel_or_kill_job`, `kill_job`, `hold_job`, `release_job`, `restart_job`, `resubmit_job`, `enable_job`, `disable_job`, `delete_job`, `update_job`, `unknown_job`, `create_job_definition`, any `create_*`/`update_*`/`delete_*`/`clear_*`/`raise_*` tool, credential tools, process-server start/stop/restart, alert or message replies. Ask the user to enforce the allowlist in the client's connector settings as well; an instruction is not a control.

Business keys are `PARTITION.NAME`. `get_job` takes one ID at a time; do not loop it over hundreds of jobs without asking.

## Procedure

1. `get_version`; record it in `extraction/NOTES.md`.
2. `list_job_definitions`. For each, `get_job_definition` and save the raw response to `extraction/raw/job_definition/<key>.json`. Raw files are the audit trail and the training material for a deterministic extractor later.
3. Map each definition to a `process_definitions` entry (fields in `src/runmyjobs_to_dagster/model.py`):
   - name (business key without partition, or keep the partition if names collide), application, description
   - type: RedwoodScript, Python, BASH/Shell map to `redwoodscript`, `python`, `os`; anything else keeps its lowercase name and will convert as unmapped
   - `command`: the script source
   - parameters: from the definition's parameters (`Name`, data type, default, direction, mandatory); the doc calls them `JobDefinitionParameters`
   - queue: the default queue
   - anything set that the model cannot hold (time windows, resources, restart settings, return-code mapping, hold on submit): put it in `extraction_notes`, never drop it silently
4. `list_queues`, then `get_queue` / `get_queue_provider` per queue. Hypothesis to confirm on real output: the queue-provider `executionSize` is the concurrency limit for that queue on that process server; if it is, it becomes `max_concurrency`. Record whatever you set as an extraction note so the reviewer can check it.
5. Chains: look at what `get_job_definition` returns for a definition that is a chain (workflow). If steps and calls are present, map them to `chains`. If they are not, say so in `NOTES.md` and ask the user for another source (CAR export of the chains, or a screenshot/description); do not infer chain order from job run history.
6. Schedules: the tool catalog has no schedule listing. Hypothesis to test: a recurring job instance returned by `list_jobs` / `get_job` carries its recurrence (the cancel tools take a `cancelRecurrence` parameter, which suggests instances know about recurrences). If recurrence details appear, map them to `schedules`; if not, record the gap.
7. Events: `list_event_definitions` and `get_event_definition`. There is no sensor mapping yet; record them as units to review.
8. Write `model.yaml`, then run `runmyjobs-to-dagster validate-model model.yaml`. Fix every reported error. Do not weaken the model to make validation pass.
9. Continue with the normal workflow (inventory, convert, validate).

## What to hand back

`extraction/raw/` (every response used), `extraction/NOTES.md` (version, hypotheses confirmed or refuted, gaps), and `model.yaml`. The first real extraction is the first time we see the true formats: after it, the confirmed formats should be turned into a deterministic extractor with fixtures taken from `extraction/raw/`.
