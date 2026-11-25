"""Branching on a value: needs a design decision."""
from datetime import datetime
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator

with DAG("route_by_region", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    choose = BranchPythonOperator(task_id="choose", python_callable=lambda: "emea")
    emea = PythonOperator(task_id="emea", python_callable=lambda: 1)
    amer = PythonOperator(task_id="amer", python_callable=lambda: 2)
    join = EmptyOperator(task_id="join", trigger_rule="none_failed_min_one_success")
    choose >> [emea, amer] >> join
