"""Cosmos fixture DAGs: DbtDag, DbtTaskGroup next to ordinary tasks, and the per-task Cosmos operators.

The dbt project is `../dbt_shop` (DuckDB). The profile is a `profiles.yml` that reads the database path from the environment, so no
credential is in the repo. Set DBT_EXECUTABLE to a dbt that has dbt-duckdb when extracting (Cosmos runs `dbt ls` to render the tasks).
"""
import os
from datetime import datetime
from pathlib import Path

from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from cosmos import DbtDag, DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.constants import InvocationMode, LoadMode, TestBehavior
from cosmos.operators.local import (DbtBuildLocalOperator, DbtRunLocalOperator, DbtSeedLocalOperator, DbtTestLocalOperator)

PROJECT = Path(__file__).resolve().parent.parent / "dbt_shop"
DBT = os.environ.get("DBT_EXECUTABLE", "dbt")

project_config = ProjectConfig(dbt_project_path=PROJECT, dbt_vars={"min_amount": 5}, env_vars={"SHOP_DUCKDB_PATH": "/tmp/shop.duckdb"})
profile_config = ProfileConfig(profile_name="shop", target_name="dev", profiles_yml_filepath=PROJECT / "profiles.yml")
execution_config = ExecutionConfig(dbt_executable_path=DBT, invocation_mode=InvocationMode.SUBPROCESS)

# 1. A whole project as a DAG: one task per model (run, then its tests) and one per seed.
shop_dbt_dag = DbtDag(
    dag_id="shop_dbt_dag",
    project_config=project_config, profile_config=profile_config, execution_config=execution_config,
    render_config=RenderConfig(load_method=LoadMode.DBT_LS, dbt_executable_path=DBT, invocation_mode=InvocationMode.SUBPROCESS, select=["path:models"]),
    operator_args={"install_deps": False, "retries": 2},
    schedule="0 6 * * *", start_date=datetime(2024, 1, 1), catchup=False, tags=["cosmos"],
)


def _extract():
    return "extracted"


# 2. A task group next to ordinary tasks: dependencies go in and out of the dbt group.
with DAG("shop_mixed", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False, tags=["cosmos"]) as shop_mixed:
    extract = PythonOperator(task_id="extract", python_callable=_extract)
    transform = DbtTaskGroup(
        group_id="transform",
        project_config=project_config, profile_config=profile_config, execution_config=execution_config,
        render_config=RenderConfig(load_method=LoadMode.DBT_LS, dbt_executable_path=DBT, invocation_mode=InvocationMode.SUBPROCESS, select=["tag:staging", "customer_totals"],
                                   exclude=["stg_customers"], test_behavior=TestBehavior.AFTER_ALL),
        operator_args={"install_deps": False},
    )
    publish = BashOperator(task_id="publish", bash_command="echo published")
    extract >> transform >> publish

# 3. The per-task operators
with DAG("shop_operators", schedule=None, start_date=datetime(2024, 1, 1), catchup=False, tags=["cosmos"]) as shop_operators:
    common = dict(project_dir=str(PROJECT), profile_config=profile_config, dbt_executable_path=DBT, install_deps=False)
    seed = DbtSeedLocalOperator(task_id="seed", **common)
    run = DbtRunLocalOperator(task_id="run_staging", select="tag:staging", vars={"min_amount": 1}, full_refresh=True, **common)
    build = DbtBuildLocalOperator(task_id="build_totals", select="customer_totals", exclude="tag:slow", **common)
    test = DbtTestLocalOperator(task_id="test_all", **common)
    seed >> run >> build >> test
