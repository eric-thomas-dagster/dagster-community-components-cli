from datetime import datetime
from pathlib import Path

from airflow.operators.empty import EmptyOperator
from airflow.sdk import DAG, TaskGroup, asset, chain, task, task_group


@task
def say(x):
    return x


@task_group
def grp():
    @task
    def a():
        return 1

    @task
    def b():
        return 2

    a() >> b()


@asset(schedule=None)
def my_asset():
    pass


with DAG(dag_id=Path(__file__).stem, schedule=None, start_date=datetime(2024, 1, 1)) as dag:
    with TaskGroup("section") as s:
        t1 = EmptyOperator(task_id="t1")
        t2 = EmptyOperator(task_id="t2")
        t1 >> t2
    say(1)
    say(2)
    grp()
    grp()
    flags = [EmptyOperator(task_id=f"flag_{i}") for i in [1, 2]]
    chain(t2, *flags)
    for i in range(3):
        EmptyOperator(task_id="in_loop")
    say.override(task_id="renamed")(3)

with DAG("second", schedule=None, start_date=datetime(2024, 1, 1)) as dag:   # same variable name as above
    EmptyOperator(task_id="only")
