# Dagster targets: what each RunMyJobs construct lands on

What Dagster offers for the constructs in `mapping.md`, read from the Dagster documentation and the dagster-expert references. Dagster's side only: what a RunMyJobs construct does is in `mapping.md`.

## Queue: concurrency pool

- Assign with `@dg.asset(pool="<queue>")`; the converter does this for every process definition with a queue.
- The limit caps the steps (assets or ops) executing at the same time **across all runs**. The default granularity is `op`, which is the right reading of a queue that limits parallel processes. `granularity: 'run'` limits runs that contain pooled ops instead (set it only if the queue is meant to cap runs).
- A pool has no limit until you set one:
  - CLI: `dagster instance concurrency set <pool> <n>`
  - UI: Deployments, Concurrency, "Add pool limit"
  - Open source: `dagster.yaml`, `concurrency.pools` (`default_limit: <n>` for all pools)
  - Dagster+: the `concurrency` block in deployment settings (UI or `dagster-cloud` CLI)
- A slot held by a cancelled or failed run is **not** released by default, which can deadlock later runs. Enable run monitoring with `run_monitoring.enabled: true` and `free_slots_after_run_end_seconds: <n>`. Dagster recommends this whenever pools are used.
- Not documented: whether work over the limit queues or fails. Test it before promising queue behavior to a customer.
- The generated `MIGRATION.md` lists each pool with the exact `concurrency set` command and the run-monitoring snippet.

## Submit frame: schedule

- `ScheduleDefinition(job=..., cron_schedule=..., execution_timezone="<IANA zone>")`. Daylight saving transitions are handled by Dagster. The default zone is UTC.
- A time window that limits hours maps to a cron hour range, for example `0 9-17 * * 1-5` for hourly, 9 to 17, weekdays. Minute-level windows are rounded to hours.
- Dagster's own `ScheduleDefinition` has no end date (not in the references read). The community `CronScheduleComponent` does: `end_date` (ISO date or datetime; ticks after it are skipped). Use it for a submit frame's end date; the python output only lists the end date as a caveat.
- Multiple elements of one frame are a union of cycles: one schedule per element.

## Events, status handlers, chain-to-chain dependencies

The community registry's `EventAutomationComponent` (35 triggers, 27 actions) covers most of the rows below as YAML; the Dagster primitives are listed for when a component does not fit.

| RunMyJobs | Dagster | Notes |
|---|---|---|
| File event (poll interval, minimum size, minimum age) | basic `@dg.sensor` | The sensor polls; keep a small cursor (a string, stored in the database) for state. `minimum_interval_seconds` is a floor, not an exact interval. Minimum size and age are checked inside the sensor. |
| Raised event | sensor on whatever raises it, or a `run_status_sensor` when another chain raises it | Depends on what the event signals. |
| Status handler: restart on error | `RetryPolicy` on the step's assets | Already converted. |
| Status handler: alert or other action on failure | `@dg.run_failure_sensor` (`context.failure_event`, `get_step_failure_events()`) | Use `monitored_jobs` to limit it to the chains in question; `monitor_all_code_locations=True` for deployment-wide. |
| Chain B runs after chain A succeeds | `@dg.run_status_sensor(run_status=SUCCESS, monitored_jobs=[A], request_job=B)` | Or declarative automation if the dependency is really between the assets. |
| Precondition that depends on upstream state | declarative automation (`eager()`, `on_cron()` with `.allow()` / `.ignore()` to filter dependencies) | Evaluated about every 30 seconds; the `default_automation_condition_sensor` must be switched on in the UI. Assets only, not ops or graphs. |

Choosing: schedules for fixed times, sensors for outside events and custom polling, declarative automation for dependency-aware conditions, run-status sensors for reacting to run outcomes.

## Deadlines and expected cadence: freshness policy

RunMyJobs deadlines (a chain that must finish by a time of day, or that must run at least every N hours) have no confirmed counterpart object in the notes here; if the customer has them, they map to Dagster freshness policies:

```python
from datetime import timedelta
import dagster as dg

# must materialize at least every 24 hours, warn at 12
dg.FreshnessPolicy.time_window(fail_window=timedelta(hours=24), warn_window=timedelta(hours=12))

# must have materialized inside the hour before 06:00 each day
dg.FreshnessPolicy.cron(deadline_cron="0 6 * * *", lower_bound_delta=timedelta(hours=1), timezone="America/New_York")
```

- Set with `@dg.asset(freshness_policy=...)` or on an `AssetSpec`; apply to many assets with `defs.map_asset_specs(func=lambda spec: dg.apply_freshness_policy(spec, policy, overwrite_existing=False))`.
- Windows cannot be shorter than 60 seconds; `warn_window` must be shorter than `fail_window`.
- In open source the feature is off until `dagster.yaml` has `freshness: {enabled: True}`. The docs call it under active development, so the API may change. Dagster+ can alert when an asset fails its policy. Not supported on source observable assets.

## Sources

- https://dagster.io/docs/guides/operate/managing-concurrency/concurrency-pools
- https://dagster.io/docs/guides/operate/managing-concurrency
- https://dagster.io/docs/guides/labs/freshness
- dagster-expert references: schedules, basic sensors, run-status sensors, declarative automation
