from compat_lib import *
try:
    from airflow.operators.subdag import SubDagOperator       # removed in 3.0
except ImportError:
    SubDagOperator = None
if SubDagOperator is not None:
    with dag("s_subdag_parent") as p:
        def sub(parent_id, child_id):
            s = dag(f"{parent_id}.{child_id}")
            EmptyOperator(task_id="inner", dag=s)
            return s
        SubDagOperator(task_id="sub", subdag=sub("s_subdag_parent", "sub"))
