# Compatibility across Airflow releases

Most teams that migrate will be on Airflow 2.x, so the converter was checked against every minor release from 2.2 to 3.4, not only the current one. The method, the results, what each release changes, and what was not tested are below.

## What was tested

- **Extraction:** the IR extractor, run with each release installed (`airflow_ir.py` imports Airflow, so it must run in an environment with the release the DAGs were written for). Releases: 2.2.5, 2.3.4, 2.4.3, 2.5.3, 2.6.3, 2.7.3, 2.8.4, 2.9.3, 2.10.5, 2.11.2, 3.0.6, 3.1.8, 3.2.2, 3.3.2 and 3.4.0, each installed with Airflow's own constraint file.
- **Conversion and load:** the converter over each IR, and the generated project loaded in Dagster.
- **Behavior:** Airflow's own `dag.test()` against Dagster for every converted DAG that can run offline, from 2.5 on (`dag.test()` does not exist before 2.5): the overall outcome, each task's outcome and the returned values.
- **The suite:** 18 small DAG files in `examples/airflow_release_matrix/suite/`, one construct each, so a difference between releases points at a construct. A file defines nothing on a release that does not have its feature.

## Results

| Airflow | Python | DAGs extracted | Converted | Outcomes identical to Airflow |
|---|---|---|---|---|
| 2.2.5 | 3.9 | 20 | 16 | not run (no `dag.test()`) |
| 2.3.4 | 3.9 | 21 | 17 | not run |
| 2.4.3 | 3.10 | 23 | 19 | not run |
| 2.5.3 | 3.10 | 23 | 19 | 14 of 14 |
| 2.6.3 | 3.10 | 23 | 19 | 15 of 16 |
| 2.7.3 to 2.11.2 | 3.11 to 3.12 | 24 | 20 | 17 of 17 on each |
| 3.0.6 to 3.4.0 | 3.12 | 20 | 20 | 17 of 17 on each |

The DAGs that are not converted are the same few on every 2.x release, and each has a stated reason:
- `sla` on a task (removed in Airflow 3): the converter does not map it.
- A SubDAG and its parent's `SubDagOperator` (removed in Airflow 3): use a task group or a separate job.
- A DAG with `catchup` true, which is the default in Airflow 2 when the DAG does not say: convert with `--catchup ignore` to run from now on instead of replaying every missed interval.

The one 2.6.3 difference is not a conversion difference: Airflow 2.6's `dag.test()` infers a different data interval for a manual run, so a `TimeDeltaSensor` waits for tomorrow in Airflow and not in Dagster. The sensor itself is unchanged since 2.3.

## What changes by release, and how it is handled

| Construct | In releases | Handling |
|---|---|---|
| `schedule_interval` and `timedelta` schedules | 2.x | A `timedelta` becomes a cron when the interval divides the day (or is a day or a week) and the start date is aligned to it; otherwise it is deferred. Cron strings and presets convert; the interval semantics (logical date is the start of the interval that ended) are rebuilt for 2.x |
| Template macros `execution_date`, `next_ds`, `prev_ds`, `tomorrow_ds`, `yesterday_ds` and their `_nodash` forms | 2.x (most removed in 3.0) | Rebuilt from the schedule. Before 2.8 Airflow prints datetimes in templates with a `T` (`2026-07-02T05:00:00+00:00`), from 2.8 with a space; the runtime follows the release the IR came from |
| Datasets | 2.4 to 2.9 (3.0 renames them assets) | Convert as asset dependencies. The extractor reads the list from the DAG on 2.4 to 2.8, a condition object on 2.9 and 2.10, and the asset condition from 3.0 |
| Dynamic task mapping | 2.3 and later | Convert. Airflow 2.3 keeps the expanded arguments in a different place (`mapped_kwargs`, `mapped_op_kwargs`) and names the reference class `XComArg`; both are read |
| Setup and teardown | 2.7 and later | Convert (graph form) |
| Branching, short-circuit, trigger rules | all | Convert (graph form) |
| `SLA` | 2.x | Not converted |
| SubDAGs | 2.x | Not converted; the child DAG is deferred so it is not converted by itself |
| Partitioned timetables | 3.2 and later (3.3 for most mappers) | See `reference/partitions.md` |
| `execution_date` as a context name in callables | 2.x | Rebuilt |
| Pre-2.0 import paths (`airflow.operators.python_operator`, `provide_context=True`) | accepted by 2.x with warnings | Convert: the IR reads the loaded operators, not the import path |

## Problems this check found and fixed

- **The extractor failed on every release before 2.10** if a `~/airflow` database from a newer release existed (an alembic revision error), because Airflow was imported before the sandbox `AIRFLOW_HOME` was set. The sandbox is now set before any import.
- **Dataset-triggered DAGs lost their datasets on 2.4 to 2.9** (the extractor read an attribute that only exists from 2.10).
- **A DAG's `timedelta` schedule was not read** on any release (the extractor looked for the wrong attribute).
- **Airflow 3's "not set" marker for `logical_date` on `TriggerDagRunOperator` was read as a value**, which deferred it.
- **Mapped tasks on Airflow 2.3 were not recognized.**
- **The 2.x macros `next_ds`, `prev_ds`, `tomorrow_ds`, `yesterday_ds` were unsupported**, which deferred any 2.x DAG that used them.
- **`execution_date` printed with a space on releases that print it with a `T`.**
- **A DAG run's outcome was decided by every task**; Airflow decides it by the leaf tasks. A failed task whose cleanup and alert tasks succeeded made Dagster fail the run where Airflow succeeds. Teardown tasks follow Airflow's own rule for which tasks count.
- **The comparison harness** did not run on Airflow 2.5, 2.6, 3.0 or 3.1 (different import paths and `dag.test()` return values); it does now.

## What was not tested

- **Airflow 1.10, 2.0 and 2.1.** Not tried. DAGs written for them mostly use the pre-2.0 import paths, which the suite covers on 2.x, but the releases themselves were not installed.
- **Provider drift.** The suite uses core operators. Operators from provider packages (Amazon, Snowflake, Google, Postgres and so on) change names and arguments between provider versions, and only the S3 and Snowflake operators the converter knows are converted; the rest are reported by name.
- **Real DAG populations.** The suite is synthetic and the public example repos lean to teaching examples. Run the converter on a sample of your own DAGs, with the Airflow release you run, before planning from the conversion rate.
- **Scheduled behavior over time.** Schedules are checked structurally and by unit tests, not by waiting for ticks.
- **Parity for failing DAGs on every release.** The failure paths (a failed task, a failed setup or teardown) are covered by unit tests and by one suite DAG, not a larger comparison against Airflow.

## For a team on Airflow 2.x

1. Install the Airflow release you run in a sandbox and run `airflow_ir.py` there; the extractor reads live objects, so the DAG files must import in that environment (the providers they use must be installed).
2. Use `--catchup ignore` if most of your DAGs rely on the default `catchup=True` and you do not want history replayed.
3. Expect SLAs and SubDAGs to need hand work, and `execution_date`-style templates to convert.
