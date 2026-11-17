# Worked example: six DAG factories to one Dagster component

Source: https://github.com/astronomer/reusable_dag_examples (Astro Runtime 3.2, Airflow 3.x). It implements one S3-to-Snowflake load six ways: whole-DAG Python factory, dynamic DAG generation, TaskGroup builder, dynamic task mapping, Astronomer's Blueprint library, and the dag-factory library. Verified with Airflow 3.4.0, Dagster 1.13.30, dagster-aws and dagster-snowflake.

## Result

`manifest.json`: 61 units, 27 complete, 34 deferred with reasons, 0 pending (`status.py summary` exits 0). `dg check defs` loads 7 assets, 3 jobs, 3 schedules.

```
s3/example-landing-bucket/{orders,customers,products}   shared upstream source assets (defined once)
        |                    |                    |
analytics/raw/orders_raw   customers_raw       products_raw     one producing asset each
        |
example_01_.../post_load_transform
```

One component class (`dagster/components/s3_to_snowflake_load.py`) and one YAML instance per table. The factory function, TaskGroup builder, Blueprint and its DAG-args class, and the dag-factory YAML all collapse into that class.

## Dispositions

| Airflow | Status | Why |
|---|---|---|
| factory function, TaskGroup builders, Blueprint, dag-factory template | complete | replaced by the component |
| example_01 (orders, daily, post-load SQL) | complete | instance; canonical producer of `orders_raw` |
| example_02 customers, products | complete | instances |
| example_02 orders (hourly) | deferred | same table as example_01 at hourly cadence; one asset cannot have two partitionings or two producers |
| example_03, 05, 06 | deferred | duplicate producers of `orders_raw`; their COPY SQL is byte-identical to example_01's. Differences for the owner: wrapper steps (03), fixed -05:00 timezone and no post-load (05), UTC and no post-load (06) |
| example_04 | deferred | per-file dynamic task mapping (copy, archive, delete): redesign |
| example_astronauts | deferred | out of scope |

## Findings

1. **Four DAGs produce one table.** Dagster refused to load until each asset had one producer. See `reference/factories.md` rule 1.
2. **`context.resources` hands you derived objects**, not the resource. My first version called `.get_client()` and `.model_copy()` and would have failed in production; a fake resource that mimics the real one exposed it.
3. **Airflow 3 schedules have no data interval.** A run at Chicago midnight on July 2 has `ds=2026-07-02`, so the DAGs load the new day's prefix at the moment the day starts. A faithful port keeps that; it is a question for the DAG owner.
4. The static scan listed 6 DAGs; Airflow produced 9. Factories need the runtime reconcile.
5. The retry delay (Airflow's default 300 s) made a failing harness look hung.

## Equivalence rows

| Airflow | Dagster | Delta |
|---|---|---|
| `schedule="@daily"` (Airflow 3 trigger timetable), `start_date` in America/Chicago | `ScheduleDefinition` with an `execution_fn` returning the partition starting at the tick, daily partitions with `end_offset=1`, timezone America/Chicago | none measured: cron and `ds` match at 3 ticks per DAG |
| hourly `0 * * * *` with a date prefix | hourly partitions; `ds` = UTC date of the partition start | matches at 27 consecutive ticks (ds flips at 19:00 Chicago). Not deployed (deferred) |
| `retries=2`, default `retry_delay` 300 s | `RetryPolicy(max_retries=2, delay=300)` | Dagster retries the whole asset, including the S3 listing |
| four tasks (list, manifest, log, copy) with independent retries | one asset | a COPY failure now repeats the listing; the manifest is metadata on the materialization |
| XCom between list and manifest tasks | none (inside one asset) | the manifest is no longer a separate artifact |
| operator-level warehouse/database/role/schema | `USE` statements at session start | same effect; the connection's own values are not changed |
| connections `aws_default`, `snowflake_default` | resources with the same keys, credentials from environment variables | credentials must be provisioned in each deployment |

## Parity (against the real Airflow DAG objects)

`parity/af_side.py` loads the DAGs with Airflow's DagBag, takes each real operator's arguments, and generates the COPY statement with the hook mocked. `parity/dg_side.py` materializes the converted assets with fake S3 and Snowflake resources. `parity/compare_parity.py` compares: 19 of 19 checks match (COPY SQL, S3 prefix, session overrides, manifest, cron, `ds` at three ticks, for the three deployed tables). `parity/finalize.py` drives the manifest through `validate_defs.py` and `status.py`.

## What this does not prove

Nothing ran against real S3 or Snowflake. The fakes mirror what the real resource classes attach; they are not the real services. Airflow's own rendering of `{{ ds }}` was not run independently (the rule was checked against the timetable's logical dates). Operator-level behavior outside the SQL and prefix (timeouts, connection pooling, `autocommit`) was not compared. The deferred DAGs were not converted.
