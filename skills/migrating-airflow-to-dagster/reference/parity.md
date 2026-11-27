# Parity checks

A unit that loads and matches its plan (`validate_defs.py`) is structurally right. Parity checks are what justify advancing a judgment unit. Two checks were run and verified in `examples/three_dags/` (Dagster 1.13.30):

## 1. Function-level parity (TaskFlow and PythonOperator tasks)

Run the original task bodies without Airflow and compare return values with the materialized Dagster assets.

- Stub `airflow` / `airflow.decorators` so `@task` is a pass-through that records each return value (key: module plus function name; two DAGs can both define `process_data`), and `@dag(...)` returns the function unchanged. Load the original files with `importlib`.
- Materialize the Dagster assets in-process with `dg.materialize([...])` and read each output with `result.asset_value(asset.key)`.
- Compare for equality.

This checks logic and data passing. It does not check Airflow's scheduling, XCom serialization, retries or triggers. See `examples/three_dags/parity.py`.

## 2. Trigger parity (dataset schedules, sensors via automation)

`dg.evaluate_automation_conditions(defs=..., instance=..., cursor=...)` evaluates declarative automation in-process; `result.get_num_requested(asset_key)` says what would be launched. Drive it by materializing upstream assets between evaluations and pass the previous `result.cursor`. Check at least: nothing runs when nothing has happened; nothing runs when upstream ran but the dataset was not updated; the consumer is requested when the dataset updates.

Caveat: the evaluation requests runs but does not launch them, so later ticks in a script that never executes those runs are not a faithful simulation. Interpret only the ticks up to the first request. In production the automation-condition sensor must be enabled. See `examples/three_dags/automation_check.py`.

## 3. Running the DAGs on both sides, incrementally

`scripts/parity_incremental.py CONFIG.json` runs each converted DAG with Airflow's own `dag.test()` and with `dg.materialize`, in separate interpreters, and compares the overall outcome, each task's outcome, and each returned value (`scripts/parity/compare.py`). It records a hash of each DAG's generated module and, on the next run, runs only the DAGs whose hash changed, so a parity pass after a small change takes seconds, not the full corpus time. Results from earlier runs stay in the state directory, so the comparison always covers every DAG that has been run. Pass `--all` after changing something the DAGs share (the generated `runtime.py`); use `--seed` once to record the current conversion as already checked, and `--select` to see what would run. The config format is at the top of the script.

Choose the DAGs to compare with care: converted, no external systems, no long waits (those need a mocked system, as in `examples/runtime_conversion/component_parity/`). Values that are random or time-based will differ and are not defects; for a mapped task Airflow records one instance and Dagster the collected list, so the check passes if Airflow's value appears in the list.

## 4. Three-way behaviour comparison (original, converter, rewrite)

`scripts/eval_behavior.py` runs the original DAG in real Airflow twice (the repeat marks nondeterministic fields) and each route's Dagster output, in identical sandboxes (scratch working directory, SQLite, HTTP and SMTP recorders, moto for S3), and compares outcomes, each task's outcome, XCom against asset values, files, rows, requests sent, mail, S3 objects, failure behaviour, schedule, retries, pools, dependencies and job structure, each dimension on its own, with a roll-up verdict. See [rewrite-route-evaluation-runbook.md](rewrite-route-evaluation-runbook.md). It is the successor to section 3 for the rewrite route.

How the comparison reads what it cannot compare directly (pass 6; the tests are in `scripts/tests/test_converter_stage2a_v9.py`):

- **Partitioned and dynamic-partition routes.** `run_dagster.py` runs a partitioned route with its last key; a dynamic partition set is empty on a fresh instance (and listing it needs one), so the harness registers the key `harness_partition` first (a multi-partition set gets one for its dynamic dimension). `dag.test()` has no partition key, so `dag_run.partition_key` prints `None` in Airflow and the key in the route. Decision: the key is read as `None` in the route's printed lines before masking (`printed_lines(text, partition_key)`), so the rest of every line is still compared and only the key itself is excused; the result records `partition_key` and the evidence says `partition_key_read_as_None`. (Running the route unpartitioned is not possible: a partitioned asset needs a key.)
- **Task-group loops.** Airflow's inner task ids (`group.task`, `group.<gate>`) have no step of their own; the converter report lists them under `loop_members` and in `task_keys`, and the harness treats them as folded into the loop's step.
- **A folded chain whose leaf fails.** The asset fails as one step. The folded steps keep the existing accepted-representation rule, and the chain's final task, which Airflow never started (`upstream_failed`), is translated the same way when a folded step of that asset failed in Airflow. It is not translated when nothing folded failed (a real difference stays one).
- **Wall-clock strings.** A value is also nondeterministic when it differs from an earlier recorded baseline run of the same DAG (two runs in the same second agree, an earlier one does not), and a value pair that is equal once its wall-clock timestamps (ISO, or the `date` command's format) are masked is not compared when the baseline's timestamp is within 36 hours of the Airflow run (the log's modification time). A fixed business timestamp far from the run time stays compared.
- **`dag.test()` limits are `baseline-failed`, not the route's fault.** A deferrable task (`deferrable=True` in the spec; `dag.test()` has no triggerer, does not give `execute_complete` its kwargs and never runs a `start_from_trigger` operator's `execute`), an `@asset` DAG whose inlet asset no other DAG registered (`ASSET_NOT_FOUND`), and an async task's `aget` / `aset` (`dag.test()`'s in-process supervisor has no `asend`). These DAGs leave the shares; they are not counted as successes.

### Harness burn-down: what the comparison now does (tests: `scripts/tests/test_eval_burndown.py`)

- **One file per dag id.** Two files of a repository can define the same dag id (an `exercises/` copy and its `solutions/` copy). The corpus keeps the first and records its `file`; the spec, the kit and the converter now follow that file (`eval_common.pick_ir_dag`, `dedupe_ir`): the IR handed to the converter keeps, for such an id, the file the manifest row names, converted into `<repo>.<airflow>.rad`. The DAG Airflow runs is the DAG that is converted.
- **Symlinked repositories.** A work directory's `repos/` is usually a symlink; `local_deps` resolved data files out from under the root and found none, so the sandbox's working directory was empty and `include/...` reads failed in Airflow (`baseline-failed`). The root is resolved first now.
- **Virtualenv and external-python tasks.** The baseline cannot build a `PythonVirtualenvOperator` environment offline (pip, a package index or wheelhouse, an interpreter that still supports a pinned old `numpy`); the route runs the task in-process. This is classified `baseline-failed`, reason `environmental (virtualenv-build)` naming the requirements. No stand-in venv is built: it would need installs (see `converter-triage.md`, "Burn-down, harness").
- **Printed output.** A record the logger printed with a short level prefix (`CRITI [airflow.task] ...`), chained-traceback text (`During handling of the above exception ...`, `Stack Trace:`, caret lines) and the same long line cut at a different point by each side (one a prefix of the other, at least 80 characters) are not differences.
- **An Airflow-side XCom read failure is `baseline-failed`.** `xcom read failed ... airflow.serialization` (for example `serializers.deltalake has no attribute 'stringify'`) means the baseline interpreter's serializer does not match its Airflow, so the baseline's values are unreadable; reason `environmental (xcom-serialization)`. A general rule (the error text, not a DAG name), checked before the success test, so such a DAG no longer flips to `different`.
- **When the baseline fails by design, `matches-baseline-failure` is the top result.** Failing where the original fails is the correct outcome, so in the regression check it outranks `equivalent-with-differences` and the rest for such a DAG (`verdict_snapshot.rank`); `stub-matches-failure` stays the lowest of the failure-matching verdicts. The committed `verdict-snapshot.json` is unchanged by this (a regenerate is not required).
- **A stub never matches a failure.** `matches-baseline-failure` is kept apart from the successes in the report (section "Failed by design"), with whether the two failures share an exception class (`rollup.cause.same`: true, false = coincidental agreement, null = not told). A route failure counts as a stub (`stub-matches-failure`) when its error is a loud `AIRFLOW-CAVEAT`, a `NotImplementedError` the converter runtime raises for something it does not mirror (unless the baseline fails with one too), or when the failing asset holds a task the converter report lists as stubbed.
- **What a strict `equivalent` did not observe.** Every `rollup` of a thin `equivalent` carries `unobserved` (values, files, sqlite, http, smtp, s3, stdout, failure that compared nothing). The report adds, per route, how many strict DAGs are thin, a count per missing dimension, and a table of each thin DAG with the reason. Verdicts are unchanged.
- **The route's environment.** A route that fails on `No module named 'x'` (and the baseline does not) gets `rollup.route_missing_modules`; the report lists them under "Route environment". These need an install, not a converter change.

**Retries and pools are attributed per task.** `parity/run_dagster.py` reads the retry policy and pool of the op that belongs to each asset key (a graph-backed asset holds one op per task, named `<dag>__<task>` and identified by the output's `airflow_task` metadata), not the union over the graph, so one pooled op no longer marks every asset in its graph. A converted `max_active_tis_per_dag` becomes a Dagster pool named `<dag>__<task>`; that is not an Airflow pool, so the `pools` dimension ignores a route pool of exactly that name on a task that has no Airflow pool (any other extra or missing pool is still a difference).

### Regression guard: `scripts/verdict_snapshot.py`

`reference/verdict-snapshot.json` holds one entry per measured DAG (converter route): verdict (`stub-matches-failure` separately), reason, `thin` or `substantive` (what was not observed when thin), baseline class and a digest of the converter report (stubbed and degraded tasks, caveat count, deferred). It was seeded from the burn-down data (work_v9 stage2a, work_fresh1c, work_fresh2b, work_stage1c): 242 DAGs, 177 strict (97 substantive, 80 thin).

```
cd scripts
python3 verdict_snapshot.py check WORKDIR [WORKDIR ...] --conv-dir CONVDIR [CONVDIR ...]     # after a measurement run: exit 1 on any regression
python3 verdict_snapshot.py save  WORKDIR [WORKDIR ...] --conv-dir CONVDIR [CONVDIR ...]     # re-seed after a deliberate change (freshest behavior.json per DAG wins)
```

A regression is a verdict that got worse (equivalent > equivalent-with-differences > matches-baseline-failure > stub-matches-failure > different = not-comparable > no-output; where the baseline fails by design, matches-baseline-failure ranks first), a strict `equivalent` that went from substantive to thin, or a new stub, degraded task or caveat in the converter report. Improvements, baseline changes (`baseline-failed` appearing or leaving: not ranked), DAGs not measured in the run and new DAGs are listed; only regressions fail the check. Run the full set before trusting a clean result: a partial run reports the rest as "not measured".

## What still needs a live comparison

Operator-based tasks, sensors, anything touching external systems, durations, failure behavior and data volume. For those, run both systems on the same logical window and compare outputs (row counts, checksums); with an Airflow instance available, `af runs list` and `af dags stats` give the baseline durations and failure rates.
