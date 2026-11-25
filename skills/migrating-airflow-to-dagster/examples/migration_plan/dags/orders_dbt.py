"""Cosmos DbtDag: the whole analytics project (the plan treats it as dbt-centric)."""
from datetime import datetime
from cosmos import DbtDag, ProfileConfig, ProjectConfig

orders_dbt = DbtDag(
    dag_id="orders_dbt",
    project_config=ProjectConfig("/opt/airflow/dbt/analytics"),
    profile_config=ProfileConfig(profile_name="analytics", target_name="prod", profiles_yml_filepath="/opt/airflow/dbt/analytics/profiles.yml"),
    schedule="0 5 * * *", start_date=datetime(2024, 1, 1), catchup=False,
)
