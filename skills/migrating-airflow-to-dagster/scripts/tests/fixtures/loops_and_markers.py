from datetime import datetime

from airflow.sdk import DAG, task
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchExternalPythonOperator
from airflow.sensors.external_task import ExternalTaskMarker


@task
def produce():
    return 1


@task
def consume(x):
    return x


with DAG("loopy", schedule=None, start_date=datetime(2024, 1, 1)) as dag:
    marker = ExternalTaskMarker(task_id="marker", external_dag_id="x", external_task_id="y")
    pick = BranchExternalPythonOperator(task_id="pick", python_callable=lambda: "a", python="/usr/bin/python3")
    done = EmptyOperator(task_id="done")
    for i in range(3):
        step = EmptyOperator(task_id=f"step_{i}")
        pick >> step >> done
    consume(produce())
