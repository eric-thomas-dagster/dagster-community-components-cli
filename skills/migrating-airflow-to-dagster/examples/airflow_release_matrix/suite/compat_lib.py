"""Imports that work on Airflow 2.2 through 3.x, so one suite can be loaded by every version."""
import datetime, inspect
import airflow

try:
    from airflow.sdk import DAG                                  # 3.x
except ImportError:
    from airflow import DAG                                      # 2.x
try:
    from airflow.providers.standard.operators.bash import BashOperator
    from airflow.providers.standard.operators.python import PythonOperator, BranchPythonOperator, ShortCircuitOperator
except ImportError:
    from airflow.operators.bash import BashOperator
    from airflow.operators.python import PythonOperator, BranchPythonOperator, ShortCircuitOperator
try:
    from airflow.providers.standard.operators.empty import EmptyOperator
except ImportError:
    try:
        from airflow.operators.empty import EmptyOperator
    except ImportError:
        from airflow.operators.dummy import DummyOperator as EmptyOperator
try:
    from airflow.sdk import task, TaskGroup
except ImportError:
    from airflow.decorators import task
    from airflow.utils.task_group import TaskGroup

VERSION = tuple(int(x) for x in airflow.__version__.split(".")[:2])
START = datetime.datetime(2024, 1, 1)


def dag(dag_id, schedule="@daily", **kw):
    """A DAG with the schedule keyword this version understands."""
    params = inspect.signature(DAG.__init__).parameters
    key = "schedule" if "schedule" in params else "schedule_interval"
    kw.setdefault("catchup", False)
    return DAG(dag_id, start_date=START, **{key: schedule}, **kw)


def say(msg="hi"):
    print(msg)
    return msg
