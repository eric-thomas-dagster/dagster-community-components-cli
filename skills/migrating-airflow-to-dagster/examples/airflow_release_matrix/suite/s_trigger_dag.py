from compat_lib import *
try:
    from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
except ImportError:
    from airflow.operators.trigger_dagrun import TriggerDagRunOperator
with dag("s_trigger_child") as c:
    EmptyOperator(task_id="x")
with dag("s_trigger_parent") as p:
    TriggerDagRunOperator(task_id="go", trigger_dag_id="s_trigger_child", wait_for_completion=True, poke_interval=5)
