# the pre-2.0 import paths that 2.x still accepted (with warnings)
import datetime
try:
    from airflow.operators.python_operator import PythonOperator
    from airflow.operators.bash_operator import BashOperator
    from airflow import DAG
except ImportError:
    PythonOperator = None
if PythonOperator is not None:
    with DAG("s_legacy_imports", start_date=datetime.datetime(2024, 1, 1), schedule_interval="@daily") as d:
        b = BashOperator(task_id="a", bash_command="true")
        p = PythonOperator(task_id="b", python_callable=lambda **c: 1, provide_context=True)
        b >> p
