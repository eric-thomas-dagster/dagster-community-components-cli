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
| Dataset-triggered schedule (`schedule=[...]`) | Asset deps with an automation condition such as `AutomationCondition.eager()` | JUDG | Verify; semantics differ from "all datasets updated since last run" |

## Scheduling

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| `schedule` cron or preset (`@daily` = `0 0 * * *`, `@hourly`, `@weekly`, `@monthly`, `@yearly`) | `ScheduleDefinition(cron_schedule=...)` | MECH | Set `execution_timezone`; Airflow cron is evaluated in the DAG timezone |
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
| Dynamic task mapping (`.expand`) | Dynamic partitions, or a multi-asset / graph with dynamic outputs | REDESIGN | |
| `SubDagOperator` | Flatten into the parent graph | REDESIGN | |
| `trigger_rule` other than `all_success` | No direct equivalent; checks, run-status hooks or automation conditions | JUDG | |

## Behavior

| Airflow | Dagster | Class | Notes |
|---|---|---|---|
| `retries`, `retry_delay` | `RetryPolicy` | MECH | `retry_exponential_backoff` maps to a backoff setting; verify |
| `pool` | Dagster pool (`pool=` on the asset or op) | MECH | Limits are configured in Dagster (Dagster+ or the instance), not in code; list each pool and limit in the report |
| `execution_timeout` | Op/asset timeout or an inner timeout | JUDG | |
| XCom (`xcom_push/pull`, `.output`, and TaskFlow return values passed to another task, which are implicit XComs) | Return values with an IO manager, explicit storage, or fusing steps | JUDG | Decide per edge; do not assume an XCom backend exists |
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

Measured by comparing the scan with Airflow 3.4.0's own parse of the example DAGs in `apache/airflow` (main): all 118 DAGs the parser produced were found; of 427 task ids in matched DAGs, 75% matched exactly, 25% sit in DAGs where the scan holds a unit flagged `dynamic_id`, `in_loop` or `mapped` (the real ids are only known at parse time), and none were unaccounted for. The scan also reported 4 DAGs Airflow did not produce, all from files Airflow failed to import (missing optional providers or a source newer than the installed provider). An exact match was only reached after fixing real gaps this comparison exposed (TaskGroup id prefixes, `__1` call suffixes, operators in comprehensions, `@setup`/`@teardown`/`@task_group`/`@asset`, reused DAG variable names, `Path(__file__).stem` ids), so re-run the comparison on your own repo rather than assuming the number carries over.

What the static scan models: DAGs by `with DAG`, `DAG(...)`, `@dag` and `@asset`; tasks at their call sites with group prefixes and Airflow's duplicate-call suffixes; `.override`, `.partial`, `.expand`, `.loop`; `>>`, `<<`, `chain`, `set_downstream`, TaskFlow argument passing; dag-factory YAML.

What it does not: DAGs or tasks produced by factories in other files, loops with unknowable counts, constructs added after it was written (it flags `.loop` groups but cannot name synthetic tasks such as `__loop_gate`), or code behind conditional imports. It reports unparsable files as `unparsed`, keeps computed task ids as `dynamic_id` and loop-created tasks as `in_loop` (one source task may be many), and `reconcile.py` closes the remaining gap against a real Airflow parse. When the manifest's DAG count disagrees with the instance's, trust the instance.
