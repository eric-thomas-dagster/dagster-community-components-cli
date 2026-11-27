# Rewrite patterns: verified Dagster code for Airflow control flow

The principle: **native Dagster forms first, plain Python inside the asset as the stated fallback.** For branching, short circuits, skips, rejoins and runtime fan-out, Dagster has native forms (optional outputs,
`graph_asset` fan-in, `DynamicOut`/`.map()`); use them. Plain Python inside one asset (`if/else`, early `return`, `for`, `try/finally`, call order, local variables for XCom) is the FALLBACK, for when (a) the branches
produce the same output and differ only in logic, (b) the native form is not verified to work for the case, or (c) it failed a gate; the decision record then says `fallback to plain Python: <reason>`. The
product-granularity principle stays (one asset per data product, not one per task: tasks that only prepare a single output fold into its asset), but it is not a reason to fold a real branch into plain Python.

Dagster fixes the shape of a run before it starts, so a runtime decision is an optional output (the step runs, the output is or is not yielded) or code inside an asset, never a different set of assets chosen at
run time. `can_subset` is launch-time selection, not a runtime decision; verified unnecessary and harmless here, so do not add it.

Each entry here has a `.md` (when it applies, the exact header sentences, what is lost; native form first, fallback last) and a `.py` of working code. **Every `.py` is executed by `scripts/tests/test_rewrite_patterns.py` on the
installed Dagster (1.13.31 when written) and its skip/state behaviour is asserted.** Where a claim is only checked against the docs or source it says so.

## What Dagster can and cannot express, per Airflow state and scenario

**Choice rule (native first, plain Python as the fallback):**
- ShortCircuit -> an **optional-output asset or op** (a generator that returns before yielding; `short_circuit.py`);
- branch to different downstream assets, ops, retries, resources or checks -> **`@multi_asset` with `AssetOut(is_required=False)`** or an op with `Out(is_required=False)` (`branching.py`, (a));
- branch then rejoin into one product -> **`@graph_asset` with an op using `Out(is_required=False)` per branch and a fan-in `list` input** (`branching.py`, (b));
- a list known only at run time -> **`DynamicOut` + `.map()`** (`mapped_tasks.py`);
- **fallback, plain Python in the asset** (`branching_plain_fallback.py`, an early `return`, a `for` loop): only when (a) the branches produce the same output and differ only in logic, (b) the native form is not
  verified to work for this case, or (c) it failed a gate. State it: `- skip/branch: fallback to plain Python: <reason>` in the decision record.
Do not add `can_subset=True`: launch-time selection, verified unnecessary and harmless here, not a runtime decision.

| Airflow state / scenario | Form that reproduces it | Same-run behaviour? |
|---|---|---|
| `success` | the asset materializes | exact |
| `failed` | the step fails; the run fails | exact |
| `up_for_retry` | `RetryPolicy` on the asset or op | policy exact; no per-task retry history |
| `upstream_failed` | a downstream step of a failed upstream asset is not run | exact for separate assets; folded tasks fail with their step |
| branch not taken, its own asset (`multi_asset` optional outs, graph_asset ops): the NATIVE form | output not yielded: asset not materialized, every step below it SKIPPED, run succeeds | **same run, skipped state expressed** (verified) |
| branch not taken, folded into the branching asset (plain Python): the FALLBACK | not executed, no state of its own | **state not expressible**; the effects are exact. Header: `folded into `<asset>`: branch not taken`; record: `fallback to plain Python: <reason>` |
| join after branches, `all_success` | a regular downstream asset with inputs from several optional outputs | exact: skipped if ANY input is skipped |
| join after branches, `none_failed_min_one_success` / `one_success`, same run | `graph_asset` with a fan-in `list` input over the branch ops (`rejoined`) | **same run, runs with whichever branch ran** (verified); with none taken it is skipped, as in Airflow |
| same join as separate top-level assets | `AutomationCondition.any_deps_updated()` on the join | approximation: requested in a LATER run after any one branch ran, not the same run (verified with `evaluate_automation_conditions`) |
| join as a SEPARATE top-level asset (input or `deps`) below a top-level optional output, a multi_asset output or a skipped graph_asset | skipped when ANY upstream is skipped (verified for every one of these shapes; `Nothing`-typed inputs and `deps` do not help) | mismatch with none_failed / all_done: use a join `graph_asset` that takes the branches as inputs with pass-through ops and a fan-in `list` (`join_graph`, verified to run with the branches that yielded), or the marker output, or the sentinel form |
| join in the same `graph_asset` as the branch | runs with the branches that yielded (verified) | same run |
| all_done step after an upstream FAILURE, its own step | never runs in the same run (verified: dependents of a failed step, a fan-in join included, are not executed) | not expressible; hooks / try-finally / run-failure sensor approximate it |
| ShortCircuit task itself | success: the condition part runs | exact |
| ShortCircuit downstream skipped (plain Python), the FALLBACK | early `return`; guarded tasks folded | effects exact, state not expressed |
| ShortCircuit downstream skipped (optional output), NATIVE | generator that returns before yielding: not materialized, everything below skipped | **same run, skipped expressed** (verified). A bare `return` with no `yield` does NOT skip (the None counts as the output): claim checked and refuted |
| a task that skips itself and is its own asset, no optional output wanted | metadata `airflow/state: skipped` and the SKIPPED sentinel (`skip.py`) | closest: shows as materialized, the metadata carries the state |
| mapped task with 0 instances (Airflow: skipped, downstream skipped) | `DynamicOut(is_required=False)`: zero items = output not yielded, steps below `.map()`/`.collect()` skipped, run succeeds, asset not materialized (verified). The default `DynamicOut()` collects `[]` and the step below runs; a plain loop also returns `[]` | **same run, skipped reproduced** with the optional form |
| `all_done` after a failure | `try/finally` in one asset | closest: cleanup runs, then the step is failed (Airflow: all_done task succeeds) |
| `one_failed` / `all_failed` | run-failure sensor launching a handler job | approximation: after the run |
| `deferred`, `queued`, `scheduled`, `running`, `removed` | none | not expressible |
| sensor `reschedule` | a Dagster sensor that requests the run | closest |
| `depends_on_past` waiting | the asset fails when its previous step failed, or (partitioned) when the previous partition is not in `instance.get_materialized_partitions` (`depends_on_past.py`, both verified). A self-dependency with a -1 mapping does NOT block a run, and `eager()` / `on_missing()` on it requested nothing in evaluation (no daemon run) | closest (waits vs fails) |
| `all_done` cleanup, native | `@dg.success_hook` + `@dg.failure_hook` on the asset or graph_asset (verified to fire on both): the asset keeps its true state, the cleanup runs after it either way | closest: the cleanup is not a task with its own `success` state |
| mapped task, one failed instance | `DynamicOut` + `.map()`: that step fails, the run fails, the other items ran, the step after `.collect()` is not run (verified) | same as Airflow (instance failed, others success, downstream upstream_failed) |
| mapped items as tracked units | `DynamicPartitionsDefinition` (keys added by a sensor or code), one partitioned asset | closest: a partition status per item, not a task instance |
| pool `pool="x"` | first-class `pool="x"` on asset/op (not graph_asset): an op attribute, step level; limit via `dagster instance concurrency set x <n>` (instance config). `dagster/concurrency_key` is a different, older op tag; run-level limits are the QueuedRunCoordinator's `tag_concurrency_limits` on run tags (docs only) | slot count is not in the spec; per-run limits and `pool_slots` > 1 not expressible |
| long wait / sensor `reschedule` / async job | a `@sensor` emitting `RunRequest` (`condition_sensor`, `job_done_sensor`), or Pipes | closest: the wait is outside every run, no timeout state |
| short wait, same run needed | `poll_until` in the asset (fallback) | holds the worker like `mode='poke'` |
| TriggerDagRun / ExternalTaskSensor / asset trigger | asset dependency + `AutomationCondition.eager()` / `on_cron`; with conf: `run_status_sensor` yielding `RunRequest(run_config=...)` (verified eager requests the downstream) | separate run: `wait_for_completion` not reproduced |
| `PythonVirtualenvOperator`, `@task.virtualenv` (pip install at run time) | NO equivalent: run in the project's environment and add the `requirements` to the project's dependencies; a conflict means a separate code location (`venv_pipes.py`, in-process form, run) | the per-task isolation is not reproduced; say so in the record |
| `ExternalPythonOperator`, `@task.external_python` | `PipesSubprocessClient` with extras in, logs and metadata streamed back, value out, interpreter from `<TASK>_PYTHON` (`venv_pipes.py`, run with a separate interpreter) | arguments and return value must be JSON; no task context in the script |
| `@task.docker`, `@task.kubernetes` | `PipesDockerClient` / `PipesK8sClient` / `PipesECSClient`, or executor step tags with an image (`venv_pipes.md`; **UNVERIFIED**: no daemon or cluster here) | needs an image with Python and `dagster-pipes` |
| XCom | return value as an asset argument; observability values as `add_output_metadata` | exact for small values |
| TaskGroup | `group_name` + `key_prefix` (consumers name the prefixed key via `AssetIn(key=...)`); a factory for a template group | exact except the UI grouping |

## What folding loses (state it in the decision record)

Per-step retries (one `RetryPolicy` for the whole asset), per-step visibility in the UI (one run step, not one per task), partial restart (a failed asset restarts from its start). If any of these
matters, the alternative is a graph asset (`@dg.graph_asset`) with one op per step and a `RetryPolicy` on each op; say so in the record.

## Index

| Airflow construct | File | Detected from the spec by |
|---|---|---|
| BranchPythonOperator, `@task.branch`, BranchDateTimeOperator | `branching.md`, `branching.py` (native, optional outputs), `branching_plain_fallback.py` (fallback); runtime lists: `mapped_tasks.py` | an operator name containing `Branch` |
| ShortCircuitOperator, `@task.short_circuit` | `short_circuit.md`, `short_circuit.py` | an operator name containing `ShortCircuit` |
| AirflowSkipException, exit code 99, `skip_on_exit_code` | `skip.md`, `skip.py` | `AirflowSkipException` or `skip_on_exit_code`/`99` in a callable or bash command |
| trigger rules other than `all_success` | `trigger_rules.md`, `trigger_rules.py` | a task `trigger_rule` that is not `all_success`; setup/teardown tasks |
| `expand`, `expand_kwargs`, `map` | `mapped_tasks.md`, `mapped_tasks.py` | `is_mapped` / `expand` on a task |
| sensors with `timeout`, `soft_fail`, `mode` | `sensors.md`, `sensors.py` | a task with `sensor` facts or an operator name containing `Sensor` |
| `depends_on_past`, `wait_for_downstream` | `depends_on_past.md`, `depends_on_past.py` | `depends_on_past` on a task |
| TriggerDagRunOperator, ExternalTaskSensor, asset-triggered schedule | `cross_dag.md`, `cross_dag.py` | an operator name with `TriggerDagRun` / `ExternalTask`, or `schedule.assets` |
| pools, `max_active_tis_per_dag` | `pools.md`, `pools.py` | a task `pool` other than `default_pool` |
| XCom between tasks | `xcom.md`, `xcom.py` | a task that `reads_value_from` another |
| TaskGroup | `task_groups.md`, `task_groups.py` | a task with a `group` |
| virtualenv, external_python, docker, kubernetes tasks | `venv_pipes.md`, `venv_pipes.py` (`_venv_pipes_task.py`: the script that runs in the other interpreter) | an operator name with `Virtualenv`, `ExternalPython` / `PythonExternal`, `DockerOperator` / `_DockerDecorated`, `KubernetesPod` / `_KubernetesDecorated` |
