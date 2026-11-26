"""A DAG whose only task fails for an environmental reason (an unreachable service): the harness must call the baseline `baseline-failed`, not blame the route."""
import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def call():
    raise ConnectionRefusedError("[Errno 111] Connection refused")


with DAG("fixture_env", schedule=None, start_date=datetime.datetime(2024, 1, 1), catchup=False) as dag:
    PythonOperator(task_id="call", python_callable=call)
