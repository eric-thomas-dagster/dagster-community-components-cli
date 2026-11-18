from compat_lib import *
with dag("s_branch") as d:
    def pick():
        return "left"
    br = BranchPythonOperator(task_id="br", python_callable=pick)
    left, right = EmptyOperator(task_id="left"), EmptyOperator(task_id="right")
    join = EmptyOperator(task_id="join", trigger_rule="none_failed_min_one_success")
    br >> [left, right] >> join
