# Trigger rules

Between separate assets Dagster applies `all_success` only. Between tasks folded into one asset, the rule is code (the fallback form).

| Airflow rule | Folded into one asset (default) | Separate assets |
|---|---|---|
| `all_success` | call order; an exception stops the rest | automatic |
| `none_failed`, `none_failed_min_one_success`, `one_success` | the join runs after the branches (plain Python has no skip state to confuse it) | same run: a `graph_asset` fan-in `list` join over optional outputs (`branching.py: rejoined`, verified); a regular asset with two inputs is all_success and is skipped if either is. Later run: `AutomationCondition.any_deps_updated()`. A consumer of a SKIPPED sentinel: `skip.py` |
| `all_done` | NATIVE: `@dg.success_hook` + `@dg.failure_hook` on the asset or graph_asset (`hooked_asset`; the asset keeps its true state, the cleanup runs after it either way). FALLBACK: `try/finally` around the upstream work (`cleanup_after`) | closest: one asset with `try/finally`; the step is failed after the cleanup if the work failed, Airflow marks the all_done task success. Say so. |
| `one_failed`, `all_failed` | `except` around the work, then the handler | a run-failure sensor launching a handler job (`trigger_rules.py: on_failure`). It runs after the run, not in it, and the sensor must be on. Tested: the definitions load, the handler job runs, and the sensor function evaluated against a FAILED run record in an ephemeral instance (`build_run_status_sensor_context`) returns the `RunRequest`. That is not a real firing: a daemon with the sensor RUNNING (or `default_status=RUNNING`) polling the event log was not run. |
| setup / teardown tasks | `try/finally` | the same |

Header: the task is `folded into `<asset>`` with the rule named, e.g. `<task>: folded into `<asset>`: trigger_rule all_done becomes try/finally`. Decision record (trigger rules): `preserved`
(in-run code) or `not preserved: <why>` for the failure-sensor form.

## all_done after an UPSTREAM FAILURE: what can and cannot be expressed in the same run (verified)

- **A separate asset or step below a failed one never runs in the same run**: its step is not executed ("Dependencies ... failed") and the run fails. A fan-in join below a failed op in the same graph_asset is not executed either. There is no input form that runs a step after a failed upstream.
- **Can be expressed:** `@dg.success_hook` + `@dg.failure_hook` on the asset or graph_asset (the cleanup runs after the step whatever happened, the asset keeps its true state); `try/finally` (or `except`) around the upstream work inside ONE asset (the cleanup runs, then the step fails if the work did).
- **Cannot be expressed:** an all_done task that is its own step with its own `success` state after the failure, in the same run. The closest approximation is a run-failure sensor launching a handler job after the run (a later run, sensor must be running).
- **Skipped upstream, not failed:** see `branching.md` (join below a skipped upstream): the graph_asset fan-in, the marker output and the sentinel forms all run the join.
