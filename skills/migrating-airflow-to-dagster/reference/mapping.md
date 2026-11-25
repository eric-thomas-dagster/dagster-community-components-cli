# Airflow to Dagster mapping

Class: MECH = translation is mechanical; JUDG = needs a judgment call or confirmation; REDESIGN = needs a design decision. "Verify" means check the current Dagster API with `dagster-expert` or `dg` before relying on the exact name.

## Structure

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| DAG | A job over the assets it produces, plus a schedule; or just assets with an automation condition | JUDG | Granularity is the decision: one asset per data product, not per task |
| Task producing data | Asset | MECH | The unit of migration |
| Task with no data output (notify, cleanup, wait) | Op in a job, a sensor, an alert, or folded into an asset | JUDG | |
| `EmptyOperator` / `DummyOperator` | Nothing; ordering is kept through asset deps | MECH | |
| TaskGroup | Asset `group_name` | MECH | |
| Task dependencies (`>>`) | Asset `deps` | MECH | Edges between non-data tasks collapse |
| Dataset / Asset outlets and inlets | Asset keys and deps | MECH | URIs become key paths; Airflow Asset URIs map naturally to Dagster keys |
| Several DAGs writing the same table | One producing asset; the others depend on it or are deferred | JUDG | Many definitions may depend on an asset; only one may produce it. See `reference/factories.md` rule 1 |
| Dataset-triggered schedule (`schedule=[...]`) | Asset deps with an automation condition such as `AutomationCondition.eager()` | JUDG | Verify; semantics differ from "all datasets updated since last run" |

## Scheduling

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| `schedule` cron or preset (`@daily` = `0 0 * * *`, `@hourly`, `@weekly`, `@monthly`, `@yearly`) | `ScheduleDefinition(cron_schedule=...)` | MECH if no date templates; JUDG if the tasks use `ds` or the logical date | Set `execution_timezone`. The cron is the same; what a tick means is not. Airflow 3: trigger timetable, logical date = the tick, no data interval. Airflow 2: data-interval timetable, the run covers the interval that just ended. Date-templated tasks need a partition choice that matches the version: see `reference/factories.md` rule 3 |
| `@once` | Manual job, or a one-off | JUDG | |
| `schedule=None` | Job with no schedule | MECH | |
| `timedelta` schedule | Cron approximation or a custom schedule | JUDG | A fixed interval from the start date is not a cron |
| Custom timetable | Custom schedule function or sensor | REDESIGN | |
| `catchup=True` | Partitions (for example daily) plus a backfill | JUDG | Airflow's data interval versus Dagster's partition key: state the mapping in the equivalence row |
| `max_active_runs` | Run concurrency limits or tags | JUDG | Verify |
| `depends_on_past` | Partition dependencies or a custom condition | JUDG | |

## Tasks and operators

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| `BashOperator`, `@task.bash` | Asset running the command (Pipes subprocess or a registry component) | MECH | |
| `PythonOperator`, `@task` | Asset or op running the same function | MECH | JUDG if the function reads XCom, context or `ti` |
| Granularity (task or product) | `convert_ir.py --granularity {task,product}`: one asset per task (default), or steps folded into the asset they feed; the rules, the restart behavior and what is not implemented are in `reference/conversion.md`, "Choosing granularity" | MECH | a folded step restarts with its consumer's asset; asset keys change |
| Cosmos `DbtDag` / `DbtTaskGroup` | `dagster_dbt.DbtProjectComponent` (`defs.yaml`); `convert_ir.py` writes it (see `reference/conversion.md`, "Astronomer Cosmos") | MECH in every execution mode (container modes convert like local, with one informational note); a container that runs something other than dbt is a loud stub | Per-model assets and asset checks replace the Cosmos tasks; `test_behavior=after_all` runs tests per model (stated) |
| Cosmos `DbtRunLocalOperator`, `DbtBuildLocalOperator`, `DbtSeedLocalOperator`, `DbtTestLocalOperator`, `DbtSnapshotLocalOperator` used on their own | one asset per operator running the same dbt command (`DbtCliResource`) | MECH | no model-level lineage (the operator only holds a `select`) |
| `KubernetesPodOperator` | `convert_ir.py` calls `dagster-k8s` `execute_k8s_job` for the supported arguments (see `reference/conversion.md`). `PipesK8sClient` is the choice only for a container that speaks the Pipes protocol | MECH for the supported arguments, JUDG otherwise | Unverified against a live cluster; a succeeded Job is not deleted |
| SQL and provider operators (Snowflake, BigQuery, S3, Spark, Databricks, ...) | Official `dagster-<vendor>` integration or a registry component. `convert_ir.py` converts HTTP, SQL on other databases, email, the Slack webhook, dbt Cloud, Airbyte, Fivetran and Databricks run operators (see `reference/conversion.md`) | MECH for those, JUDG otherwise | Search `dagster-component search "<vendor> <action>" --json` first for the rest |
| Sensors (`S3KeySensor`, `ExternalTaskSensor`, ...) | A sensor that holds the pipeline step open converts to a polling step (`convert_ir.py`: Airflow's default `poke` behavior, check, give up after `timeout` or skip with `soft_fail`, sleep `poke_interval`) for `PythonSensor`, `@task.sensor`, `BashSensor`, `FileSensor`, `TimeSensor` and `TimeDeltaSensor`. A Dagster sensor (or declarative automation) instead starts the run when the condition holds, so it changes the pipeline shape. `DateTimeSensor`, `exponential_backoff`, `silent_fail` and `never_fail` are deferred. `ExternalTaskSensor` is a different case: see its own row | MECH for the polling step, JUDG otherwise | A polling step holds a run open for as long as Airflow's sensor held a task slot |
| `TriggerDagRunOperator` | Asset dependency or automation condition | JUDG | |
| `BranchPythonOperator`, `@task.branch`, `ShortCircuitOperator`, `@task.short_circuit` | `convert_ir.py` converts the whole DAG into one graph asset in which each op decides whether to run (a small gate): a branch result names the tasks to follow, a short-circuit stops everything after it (or only the next task when `ignore_downstream_trigger_rules=False`), and a join runs under `none_failed`, `none_failed_min_one_success`, `none_skipped`, `one_success` or `all_skipped` as Airflow would. A skipped task still appears as a materialization with metadata `airflow/state: skipped`. Dagster's own skipped steps cannot do this: a join after optional outputs is skipped if any input was skipped. | MECH for those operators, REDESIGN otherwise | Rules that depend on failures (`all_done`, `one_failed`, `all_failed`, `always`) are deferred: a failed step stops its downstream steps in Dagster. `BranchDateTimeOperator`, `BranchDayOfWeekOperator`, external-Python and virtualenv branch operators are deferred |
| Dynamic task mapping (`.expand`) | Static partitions for a known list; dynamic partitions for items found at run time | REDESIGN | One asset partitioned by the item, not N assets. See `reference/dynamic-mapping.md` and its worked example |
| `SubDagOperator` | Flatten into the parent graph | REDESIGN | |
| `trigger_rule` other than `all_success` | In the graph form `convert_ir.py` applies Airflow's own rule table (from its `TriggerRuleDep`) to the states of the steps before: success, skipped, failed and upstream-failed. A step catches its failure after its retries and passes a marker on, so `all_done`, `one_failed`, `all_failed`, `one_success`, `none_skipped` and the rest behave as in Airflow; a final step fails the run if any task failed | MECH | A DAG that uses these rules becomes one graph asset (the same form as dynamic mapping and branching) |
| Airflow assets and datasets in callables: `Asset(...)`, `Dataset(...)`, `AssetAlias`, `yield Metadata(asset, extra)`, `outlet_events[asset].extra = {...}`, `inlet_events[asset]` | The runtime provides stand-in classes that name the Dagster asset key the converter gave that asset. A yielded `Metadata` or an `outlet_events` write becomes metadata on that asset's materialization (the task's own asset, or a logged materialization for another); a generator's return value is the asset's value; `inlet_events[asset]` is the list of that asset's materializations, oldest first, each with the metadata it carried as `.extra`. An alias that names its asset at run time logs a materialization for the asset it resolves to | MECH | `triggering_asset_events`, `Asset.ref(...)` and partitioned events are not provided |
| `dagrun_timeout` | A `dagster/max_runtime` tag on the job (seconds). Dagster enforces it only when run monitoring is enabled in the deployment (`run_monitoring` with `max_runtime_seconds`; the tag overrides the default) | MECH | A run is terminated, not marked failed by a task, as in Airflow |
| `LatestOnlyOperator`, `ExternalTaskMarker` | Dropped, with a note. Latest-only skips runs that are not the latest, and a Dagster schedule does not catch up on missed ticks; the marker only declares what to clear in another DAG | MECH | A backfill you launch runs everything |
| Lambda callables | Read from the DAG file's source and rewritten as a function | MECH | |
| A user-defined operator (a `BaseOperator` subclass with `execute`, defined in the project) | The class is copied into the converted module with a small `BaseOperator` stand-in (arguments, `self.log`, no Airflow import), created with the operator's own constructor arguments, its template fields rendered, and `execute(context)` run with the rebuilt task context; the return value is the asset's value | MECH when the class and what it imports are plain Python | Deferred: a class that uses the Airflow API directly (hooks, `DagBag`), a base class other than `BaseOperator` (sensors with `poke`, deferrable operators), a constructor argument that is not a plain value (a callable), and mapped custom operators |
| `ExternalTaskSensor` | When it is the first step of its DAG, waits for the same logical date with the default states, and the awaited DAG and task are in the same conversion: a dependency of the DAG's first steps on the awaited task's asset (the whole DAG's last tasks when no task is named), with an automation condition, so the DAG's assets update when the awaited task does. The DAG's own schedule no longer starts it | MECH for that shape, JUDG otherwise | Not converted: a sensor in the middle of a DAG, an `execution_delta` or `execution_date_fn` (a run at another logical date), custom `allowed_states` or `failed_states`, a task group, or an awaited DAG outside the conversion. A Dagster `@run_status_sensor` (or the registry's `event_automation` run-status trigger) is the alternative when only job-level coordination is wanted: it fires on any run of the job, not the same logical date, and cannot wait for one task |
| `AirflowSkipException`, `AirflowFailException`, `PokeReturnValue` inside a callable | The runtime provides classes with these names, so the callable keeps working without an Airflow import. In the graph form a raised `AirflowSkipException` skips the step (and what depends on it under its trigger rule); `AirflowFailException` fails the step without retrying; a sensor callable can return a `PokeReturnValue` | MECH | A DAG whose callable raises a skip is converted in the graph form |
| Setup and teardown tasks | In the graph form: a teardown runs under `all_done_setup_success` (skipped if every setup was skipped, upstream-failed if no setup succeeded), so it runs after the scope's work fails and not after a failed setup. A failed teardown does not fail the run unless `on_failure_fail_dagrun` is set | MECH | The registry's `scoped_setup_teardown` component is the alternative when the scope is a resource |

## Behavior

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| `retries`, `retry_delay` | `RetryPolicy` | MECH | `retry_exponential_backoff` maps to a backoff setting; verify |
| `pool` | Dagster pool (`pool=` on the asset or op) | MECH | Limits are configured in Dagster (Dagster+ or the instance), not in code; list each pool and limit in the report |
| `execution_timeout` | Op/asset timeout or an inner timeout | JUDG | |
| XCom (`xcom_push/pull`, `.output`, and TaskFlow return values passed to another task, which are implicit XComs) | A return value is the asset's value (IO manager). A value pushed under a key is metadata on the asset's materialization; a keyed pull reads that metadata from the upstream asset's materialization in the same run (`convert_ir.py` does this) | JUDG | Decide per edge; do not assume an XCom backend exists. A pushed value must be JSON-serializable. Metadata is kept in the event log, so it suits small values |
| Variables | Environment variables or secrets | MECH | `dg plus create env` for Dagster+; never commit values |
| Connections (`conn_id`) | Resources configured from env vars | JUDG | |
| Jinja templates (`{{ ds }}`, `{{ data_interval_start }}`) | Partition key or run config | JUDG | |
| `on_failure_callback`, `on_success_callback`, `on_retry_callback` | The registry's `@on_hooks` decorator on the asset, with an adapter that rebuilds the callback's context dict (with `exception`). The failure callback runs after the last attempt only (the hooks fire on every attempt, so the adapter checks the retry count); `on_retry_callback` runs before each further attempt | MECH (asset form only) | Needs `dagster-community-components`. Other hooks (`pre_execute`, `post_execute`, `@task.run_if`) and callbacks in the graph form are deferred |
| SLAs / `sla` | Freshness policy or alert | JUDG | |
| `default_args` | Applied per asset (retries, owners) | MECH | |

## Generated pipelines

| Source | Handling |
|---|---|
| dag-factory YAML | The scanner reads the YAML; migrate the generated DAGs' tasks as above. Consider a Dagster component when the same shape repeats many times. |
| Cosmos | See the Cosmos rows above and `reference/conversion.md`. `ProfileConfig` becomes an `env_var()` based `profiles.yml` (names only); a profiles file is never copied. |
| Jinja-heavy or programmatically generated DAGs | The static scanner may miss them; run the project's DAG parse once (`airflow dags list` in a sandbox) and compare counts with the manifest. |

## Scanner accuracy and limits

Measured by comparing the scan with Airflow's own parse (DagBag) of five public repos. "Exact" is task ids equal to Airflow's; "flagged" is tasks in DAGs where the scan holds a `dynamic_id`, `in_loop` or `mapped` unit because the real ids exist only at parse time; "unaccounted" is neither.

| Repo (Airflow used for the parse) | DAGs found / Airflow produced | Task ids exact | Flagged | Unaccounted |
|---|---|---|---|---|
| apache/airflow example DAGs, main (3.4.0) | 118 / 118 | 75% of 427 | 25% | 0 |
| astronomer/2-10-example-dags (2.10.5) | 25 / 25 | 100% of 55 | 0 | 0 |
| astronomer/2-9-example-dags (2.10.5) | 18 / 18 | 100% of 44 | 0 | 0 |
| matsudan/airflow-dag-examples (3.4.0) | 13 / 13 | 71% of 48 | 29% | 0 |
| astronomer/reusable_dag_examples, factories (3.4.0) | 6 static / 9 (reconcile closes the gap) | n/a | n/a | n/a |
| astronomer/2-4-example-dags (2.10.5) | 10 / 10 | 100% of 31 | 0 | 0 |
| astronomer/2-5-example-dags (2.10.5) | 9 / 9 | 96% of 25 | 4% | 0 |
| astronomer/2-6-example-dags (2.10.5) | 12 / 12 | 69% of 62 | 31% | 0 |
| astronomer/2-7-example-dags (2.10.5) | 14 / 14 | 100% of 139 | 0 | 0 |
| astronomer/2-8-example-dags (2.10.5) | 11 / 11 | 100% of 23 | 0 | 0 |
| astronomer/astro-example-dags (2.10.5) | 1 / 1 | 100% of 2 | 0 | 0 |
| astronomer/astro-ide-example-dags-airflow3 (3.4.0) | 22 / 22 | 55% of 176 | 45% | 0 |
| astronomer/airflow-example-dags (2.10.5) | 3 / 3 | 5% of 58 | 95% (loop-generated) | 0 |
| astronomer/ds-ml-example-dags | no ground truth: its ML dependencies did not import | n/a | n/a | n/a |

The scan also lists DAGs Airflow did not produce, all from files Airflow failed to import (missing providers or a source newer than the installed provider). The first runs of this comparison found real gaps that are now fixed and tested: TaskGroup id prefixes, `__1` call suffixes, operators in comprehensions, `@setup`/`@teardown`/`@task_group`/`@asset`, reused DAG variable names, ids computed from `Path(__file__)` or a module-level constant, and the factory patterns in `reference/factories.md`. Re-run the comparison on your own repo rather than assuming these numbers carry over.

What the static scan models: DAGs by `with DAG`, `DAG(...)`, `@dag` and `@asset`; tasks at their call sites with group prefixes and Airflow's duplicate-call suffixes; `.override`, `.partial`, `.expand`, `.loop`; `>>`, `<<`, `chain`, `set_downstream`, TaskFlow argument passing; dag-factory YAML.

What it does not: DAGs or tasks produced by factories in other files, loops with unknowable counts, constructs added after it was written (it flags `.loop` groups but cannot name synthetic tasks such as `__loop_gate`), or code behind conditional imports. It reports unparsable files as `unparsed`, keeps computed task ids as `dynamic_id` and loop-created tasks as `in_loop` (one source task may be many), and `reconcile.py` closes the remaining gap against a real Airflow parse. When the manifest's DAG count disagrees with the instance's, trust the instance.
