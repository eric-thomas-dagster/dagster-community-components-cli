from compat_lib import *
if VERSION >= (2, 7):
    with dag("s_setup_teardown") as d:
        s = BashOperator(task_id="setup", bash_command="true").as_setup()
        w = BashOperator(task_id="work", bash_command="true")
        t = BashOperator(task_id="teardown", bash_command="true").as_teardown(setups=s)
        s >> w >> t
