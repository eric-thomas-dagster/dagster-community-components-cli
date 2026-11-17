import os

from airflow import DAG
from airflow.operators.empty import EmptyOperator

dag_id = os.path.basename(__file__).replace(".py", "")
OWNER_DAG = "named_constant_dag"

with DAG(dag_id=dag_id, schedule=None) as dag:
    EmptyOperator(task_id="a")

with DAG(OWNER_DAG, schedule=None) as dag2:
    EmptyOperator(task_id="b")
