from datetime import datetime, timedelta

from airflow import DAG
from airflow.models import Variable
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import PythonOperator

default_args = {"owner": "data", "retries": 2, "retry_delay": timedelta(minutes=5)}


def transform(**context):
    return Variable.get("threshold")


with DAG(
    dag_id="nightly_etl",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["etl"],
) as dag:
    start = EmptyOperator(task_id="start")
    extract = BashOperator(task_id="extract", bash_command="python extract.py {{ ds }}", pool="batch")
    xform = PythonOperator(task_id="transform", python_callable=transform)
    load = BashOperator(task_id="load", bash_command="python load.py", trigger_rule="all_done")
    start >> extract >> xform >> load
