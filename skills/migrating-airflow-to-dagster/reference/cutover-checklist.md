# Cutover checklist

Cutover is moving one DAG's scheduling and execution from Airflow to Dagster. It is the last step of Phase 6 in `SKILL.md`, after the side-by-side run, and it is done by a person, one DAG or one domain at a time, in a change window. `scripts/cutover.py` writes the checklist for each converted DAG; this page says why each section exists.

```bash
python3 scripts/cutover.py manifest.json report.json --ir ir.json --parity-state parity_state/ --out-dir cutover
```

It writes `cutover/cutover-<dag_id>.md` for every DAG the converter converted, plus `cutover-index.md` (deferred DAGs have no checklist, and the index says why). **It never pauses, starts, stops or deletes anything**: it reads three or four JSON files, writes markdown, and prints the commands for a person to run (a test asserts that the module starts no process and removes nothing). With a hosted Airflow, pass `--airflow-cmd` for the service's own CLI route (MWAA, Composer and Astro each differ: [hosted-airflow.md](hosted-airflow.md)); add your workspace or deployment flags to the `dagster` commands.

`--ir` is optional but recommended: without it the checklist cannot know `catchup`, `start_date`, the timezone Airflow used, owners, callbacks, SLAs or the Variables read inside callables, and it says so where that matters. `--parity-state` is the state directory of `parity_incremental.py`.

| Section | What it settles | Where the content comes from |
|---|---|---|
| Blockers | A deferred or still-pending manifest unit, or a loud stub (`AIRFLOW-CAVEAT`), means the DAG is not converted: do not cut over. | manifest, report `caveats` |
| 1. Pause plan | The order of the window: confirm nothing is running, Dagster schedule off and code deployed, pause the Airflow DAG, start the Dagster schedule, watch the first run. Airflow stays authoritative until the last step. | `schedule`, the job and schedule names |
| 2. Catchup and backfill | What Airflow replayed and what Dagster will not. `catchup=True` is converted to a schedule from now on (`--catchup ignore`, or a degraded caveat): history is not replayed, and a person decides whether to backfill. `catchup=False`: check for a gap at the switch. | IR `catchup`, `start_date`; report notes and caveats |
| 3. Secrets and connections | Names only: each Connection and Variable the DAG uses and the environment variable or resource it becomes in Dagster. No value is read or written; values are set by their owner in the deployment, and rotated if they were pasted anywhere. | report `connections`, `resources`, `snowflake_resources`, `dbt` environment; Variable names from templates and callables |
| 4. Schedule and timezone | Cron, timezone and data-interval versus trigger semantics side by side; the next Airflow run against the next Dagster tick; daylight-saving behaviour. | IR timetable; report schedule and timezone |
| 5. Retries, pools, concurrency | The limits the converter wrote to `concurrency_limits.yaml` take effect only when applied (`dagster instance concurrency set`, the run-queue tag limits, run monitoring for `dagrun_timeout`). | report `pools`, `max_active_runs`, `max_active_tasks`, `run_timeout_s`; IR retries |
| 6. NOT converted | Alerts, callbacks, SLAs and email-on-failure defaults, plus every `degraded` caveat. An Airflow `sla` becomes an observation that alerts nobody; a callback that could not be attached sends nothing. Each needs a manual replacement (alert policy, run failure sensor, freshness check) before cutover. | report `caveats`; IR callbacks, `sla_s`, `default_args` |
| 7. Freshness | What each consumer expects, and the alert that enforces it. The cadence is read from the cron as a guess. | schedule |
| 8. Parity evidence | Whether the DAG was in a parity run and its outcome (the same comparison as `parity/compare.py`: overall outcome, each task's outcome, returned values). A DAG outside a parity run needs a side-by-side run on the same logical window. | parity state |
| 9. On-call handoff | Owner, escalation path, alert route, runbook, and that the on-call person has run the job once. | IR owners and tags; blanks to fill |
| 10. Rollback | Stop the Dagster schedule, unpause the Airflow DAG (mind `catchup=True` replaying on unpause), set `proxied: false` if Airlift proxying was used ([airlift.md](airlift.md)), and check for a double run. Possible until the decommission step. | |
| 11. Decommission | After an agreed number of clean runs: keep what must be kept from Airflow's run history (it does not migrate), remove the DAG file in a reviewed change (deleting in the UI removes only metadata), remove Connections and Variables nothing else uses, remove the Airflow alert route, advance the manifest unit with evidence. | |

Rules that apply to every cutover:

- **One DAG or one domain at a time**, never the whole estate in one window.
- **No DAG is cut over with an open blocker or an unreplaced alert.** The point of the stubs being loud is that a stubbed task fails visibly; a cutover that depends on one is a cutover that fails.
- **Rollback stays possible** until decommission: keep the Airflow DAG file and its Connections until the clean-run count is reached.
- **The checklist is not a verdict.** Ticking every box does not prove the cutover is safe; unticked boxes show what is not yet known.
