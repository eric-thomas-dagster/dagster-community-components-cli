from compat_lib import *
if VERSION < (3, 0):                      # execution_date is gone in 3.0
    with dag("s_execution_date") as d:
        BashOperator(task_id="a", bash_command="echo {{ execution_date }} {{ next_ds }}")
