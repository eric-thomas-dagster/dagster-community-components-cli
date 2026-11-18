# Airflow release matrix

A small suite of DAGs (one file per construct) that loads on every Airflow release from 2.2 to 3.4, and the scripts to run the converter over it release by release.

| File | What it does |
|---|---|
| `suite/` | 18 DAG files covering basic and TaskFlow DAGs, templates and the 2.x macros, task groups, branching, short-circuit, trigger rules, dynamic mapping (2.3+), setup/teardown (2.7+), datasets and assets, sensors, callbacks, `TriggerDagRunOperator`, schedule forms, SubDAGs (2.x), pools and SLAs, and the pre-2.0 import paths. A file defines nothing on a release that lacks its feature |
| `build_env.sh <airflow version> <python minor>` | a virtualenv with that release, installed with Airflow's own constraint file (Airflow 2.2.5 needs `pyyaml==6.0.1` instead of the pinned 5.4.1 to build) |
| `run_matrix.sh` | for each `env_<version>`: extract the IR with that release, convert, and load the result in Dagster |
| `report.py [version]` | the DAG by release table, and the deferral reasons for one release |

To compare run outcomes with Airflow itself, use `scripts/parity_incremental.py` (`dag.test()` needs Airflow 2.5 or later; on 2.5 and 2.6 `dag.test()` returns nothing or infers a manual run's data interval differently, which the harness works around but which shows up in sensor timing).

Results are in `reference/compatibility.md`.
