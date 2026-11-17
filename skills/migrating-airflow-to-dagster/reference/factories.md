# DAG factories

Factories are where a static scan is weakest, because the DAGs only exist once the code runs. Handle them with the scan, a runtime reconcile, and a Dagster **component**.

## The patterns, and what the scanner reports

| Airflow pattern | Scanner unit | Dagster answer |
|---|---|---|
| Whole-DAG Python function (`build_x_dag(config)` returns a DAG) | the DAG inside it is a `dag` unit with `factory` set, class REDESIGN; each call is a `factory_call` unit with its literal configuration | one component class, one YAML instance per call |
| One DAG per config entry (`for name, cfg in CONFIGS.items(): ... globals()[dag.dag_id] = dag`) | `factory_call` units, one per dictionary entry, with the merged configuration and the evaluated `dag_id`; `dynamic_dag_generation` signal | one instance per entry |
| Reusable TaskGroup builder (`def x_group(config): with TaskGroup(...)`) | `taskgroup_template` | the same component, or a shared function that emits the same assets |
| Blueprint (`class X(Blueprint[Config])`, `*.dag.yaml` with `steps`) | `blueprint_template` (REDESIGN) and `blueprint_dag` | a Blueprint is already a config-driven template: migrate it as one component, each step as an instance |
| dag-factory (`load_yaml_dags`, YAML with `tasks` as a mapping or a list) | `dag_factory` | component instances when the shape repeats; otherwise assets |

Factory output is only fully visible at runtime. Run `airflow_truth.py` and `reconcile.py` (see SKILL.md Phase 1): in the worked example the static scan listed 6 DAGs while Airflow produced 9.

## Designing the component

- The factory function's parameters (often a dataclass) become the component's fields (`dg.Model` + `dg.Resolvable`). Scaffold with `dg scaffold component`, then `dg scaffold defs <component> <name>` per instance.
- Model **data products, not steps**. Steps that only list, log or wait become part of one asset's body, with what they produce (file counts, file names) recorded as materialization metadata.
- Airflow connection ids become Dagster resources under the same key, configured from environment variables.
- Refuse what you cannot map. The worked example's `render_template` accepts only `{{ ds }}` and `{{ ds_nodash }}` and raises at load time for anything else.

## Rules the worked example proved

1. **Many definitions may depend on one asset. Only one may produce it.** Four of six example DAGs loaded the same table. Do not invent a per-DAG key to make them load: that makes the lineage claim four tables exist. Share the upstream (a source asset defined once), keep one producer, and defer the others with the reason and the evidence that they are redundant (identical generated SQL).
2. **What `context.resources` gives you is not the resource.** `dagster-aws`'s `S3Resource` attaches the boto client; `dagster-snowflake`'s `SnowflakeResource` attaches a `SnowflakeConnection`. Calling `.get_client()` or `.model_copy()` on those fails in production. Test with fakes that attach the same derived objects. Airflow's operator-level warehouse/database/role/schema overrides become `USE` statements in the session.
3. **Schedule semantics depend on the Airflow version.** Airflow 3 turns a cron string into a trigger timetable with a zero-width data interval: a run at tick T has logical date T. Airflow 2's default was a data-interval timetable: a run at T covers the interval that ended at T. A faithful port needs the right one (`airflow_semantics` in the example). Check with the timetable API (`next_dagrun_info_v2` on the scheduler-side `CronTriggerTimetable`), not by assumption.
4. `{{ ds }}` was reproduced as the UTC date of the logical date. Verified against 27 consecutive hourly ticks of the timetable output; Airflow's own template rendering of `ds` was not run independently.
5. A `RetryPolicy` with a delay makes a failing test look hung: it is waiting out the delay. Build harness instances with retries off.
6. Quote dates in YAML (`start_date: "2026-07-01"`): an unquoted date is parsed as a date object by some loaders.
7. Asset keys and job names are global across the deployment; check for collisions before loading everything.

## Parity for factory-built DAGs

Compare against the **real Airflow DAG objects**, not a re-reading of the source: load the project with Airflow's DagBag, pull each operator's actual arguments, generate the operator's SQL with its hook mocked (nothing executes), and compare with what the Dagster asset does under fake resources. `examples/s3_to_snowflake_factories/parity/` has the harness (`af_side.py`, `dg_side.py`, `compare_parity.py`).

## dag-factory specifics (verified against dag-factory 1.1.0 in Airflow 3.4)

- **Task id.** When `tasks` is a mapping, the task id is the mapping key, not the inner `task_id` field. When it is a list, it is `task_id`.
- **`default` is not a DAG.** DAG-level values in the `default` block (schedule, catchup, tags, default_args) are inherited by every DAG in the file. `default.tasks` is **not** merged into a DAG's tasks by id: a task with no `operator` or `decorator` makes the whole file fail to build.
- **`defaults.yml` and `defaults.yaml` are never DAG definitions.**
- **Failures are logged, not raised.** A YAML file that fails to build is skipped with an error in the log; Airflow shows no import error and no DAG. The static scan flags tasks with no operator, `airflow_truth.py` records the logged failures, and `reconcile.py` lists static DAGs Airflow did not produce.
- **Loaders.** `load_yaml_dags(globals(), dags_folder=...)` loads every non-defaults YAML in the folder; paths must be absolute. A YAML file no loader reaches defines no DAGs: the scanner records `dagfactory_loader` units so you can check.
- **Families.** DAG blocks that have the same structure and differ only in parameter values (the AWS blog's seven countries) are reported as a `dag_family`. Convert the family to one asset partitioned by the varying value: `examples/dagfactory_family/` checks that the partitions equal the `op_args` of the DAGs dag-factory actually generated, the callable runs once per partition with that value, and one schedule at the shared cron requests every partition.
