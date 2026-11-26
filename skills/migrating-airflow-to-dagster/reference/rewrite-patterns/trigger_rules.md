# Trigger rules

Between separate assets Dagster applies `all_success` only. Between tasks folded into one asset, the rule is code (the fallback form).

| Airflow rule | Folded into one asset (default) | Separate assets |
|---|---|---|
| `all_success` | call order; an exception stops the rest | automatic |
| `none_failed`, `none_failed_min_one_success`, `one_success` | the join runs after the branches (plain Python has no skip state to confuse it) | same run: a `graph_asset` fan-in `list` join over optional outputs (`branching.py: rejoined`, verified); a regular asset with two inputs is all_success and is skipped if either is. Later run: `AutomationCondition.any_deps_updated()`. A consumer of a SKIPPED sentinel: `skip.py` |
| `all_done` | NATIVE: `@dg.success_hook` + `@dg.failure_hook` on the asset or graph_asset (`hooked_asset`; the asset keeps its true state, the cleanup runs after it either way). FALLBACK: `try/finally` around the upstream work (`cleanup_after`) | closest: one asset with `try/finally`; the step is failed after the cleanup if the work failed, Airflow marks the all_done task success. Say so. |
| `one_failed`, `all_failed` | `except` around the work, then the handler | a run-failure sensor launching a handler job (`trigger_rules.py: on_failure`). It runs after the run, not in it, and the sensor must be on. Only its wiring is tested here (the definitions load and the handler job runs); the sensor was not fired against a live failed run. |
| setup / teardown tasks | `try/finally` | the same |

Header: the task is `folded into `<asset>`` with the rule named, e.g. `<task>: folded into `<asset>`: trigger_rule all_done becomes try/finally`. Decision record (trigger rules): `preserved`
(in-run code) or `not preserved: <why>` for the failure-sensor form.
