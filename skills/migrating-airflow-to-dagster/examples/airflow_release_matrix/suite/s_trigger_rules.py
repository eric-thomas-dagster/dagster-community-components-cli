from compat_lib import *
with dag("s_trigger_rules") as d:
    a = BashOperator(task_id="a", bash_command="exit 1")
    c = BashOperator(task_id="cleanup", bash_command="true", trigger_rule="all_done")
    al = BashOperator(task_id="alert", bash_command="true", trigger_rule="one_failed")
    a >> [c, al]
