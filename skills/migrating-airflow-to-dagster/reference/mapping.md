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
| Cosmos `DbtDag` / `DbtTaskGroup` | `dagster-dbt` assets or the dbt project component | JUDG | Per-model assets replace the Cosmos tasks |
| `KubernetesPodOperator` | `dagster-k8s` Pipes client | JUDG | |
| SQL and provider operators (Snowflake, BigQuery, S3, Spark, Databricks, ...) | Official `dagster-<vendor>` integration or a registry component | JUDG | Search `dagster-component search "<vendor> <action>" --json` first |
| Sensors (`S3KeySensor`, `ExternalTaskSensor`, ...) | Dagster sensor, or declarative automation (`eager`, `on_cron`) | JUDG | Poke interval and timeout semantics differ |
| `TriggerDagRunOperator` | Asset dependency or automation condition | JUDG | |
| `BranchPythonOperator`, `@task.branch`, `ShortCircuit` | Conditional logic in an asset, asset checks, or dynamic outputs | REDESIGN | |
| Dynamic task mapping (`.expand`) | Static partitions for a known list; dynamic partitions for items found at run time | REDESIGN | One asset partitioned by the item, not N assets. See `reference/dynamic-mapping.md` and its worked example |
| `SubDagOperator` | Flatten into the parent graph | REDESIGN | |
| `trigger_rule` other than `all_success` | No direct equivalent; checks, run-status hooks or automation conditions | JUDG | |

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
| `on_failure_callback` and friends | Run-failure sensor or alert policy | JUDG | |
| SLAs / `sla` | Freshness policy or alert | JUDG | |
| `default_args` | Applied per asset (retries, owners) | MECH | |

## Generated pipelines

| Source | Handling |
|---|---|
| dag-factory YAML | The scanner reads the YAML; migrate the generated DAGs' tasks as above. Consider a Dagster component when the same shape repeats many times. |
| Cosmos | See the dbt rows above. |
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
