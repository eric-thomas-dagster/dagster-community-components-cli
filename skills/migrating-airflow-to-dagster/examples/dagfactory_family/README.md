# dag-factory family as one partitioned asset

The AWS blog's pattern (N DAG blocks that differ only in a country argument) converted to one asset partitioned by country.

- `family_af.py`: run in a Python environment with Airflow 3 and dag-factory, against the fixture in `scripts/tests/fixtures_dagfactory/` (set `AIRFLOW_HOME` to a temp dir; the fixture's `python_callable_file` path must point at `process_s3_data.py`). It loads the DAGs with Airflow's DagBag and records each DAG's `op_args`, callable, cron and retries into `family_af.json`.
- `country_assets.py`: the Dagster conversion (static partitions, one asset, one schedule requesting every partition).
- `check.py`: materializes each partition and compares: partitions equal the Airflow `op_args`, the callable ran once per country with that country, one schedule at the shared cron requests every partition.

Result when run: all three checks true (4 countries, cron `*/10 * * * *`). Paths in the scripts follow the layout of the session that produced them; adjust them to your checkout.
