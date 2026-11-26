"""A DAG that fails by design: `boom` raises, `after` never runs (upstream_failed), `side` is independent and writes a file."""
import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def ok():
    return "fine"


def boom():
    raise ValueError("by design")


def after():
    with open("after.txt", "w") as f:
        f.write("should not exist")


def side():
    with open("side.txt", "w") as f:
        f.write("independent")


with DAG("fixture_fails", schedule=None, start_date=datetime.datetime(2024, 1, 1), catchup=False) as dag:
    t_ok = PythonOperator(task_id="ok", python_callable=ok)
    t_boom = PythonOperator(task_id="boom", python_callable=boom)
    t_after = PythonOperator(task_id="after", python_callable=after)
    t_side = PythonOperator(task_id="side", python_callable=side)
    t_ok >> t_boom >> t_after
