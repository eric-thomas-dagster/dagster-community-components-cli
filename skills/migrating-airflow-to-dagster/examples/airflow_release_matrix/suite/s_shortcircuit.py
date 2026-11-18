from compat_lib import *
def cond():
    return True
with dag("s_shortcircuit") as d:
    sc = ShortCircuitOperator(task_id="sc", python_callable=cond)
    sc >> BashOperator(task_id="after", bash_command="true")
