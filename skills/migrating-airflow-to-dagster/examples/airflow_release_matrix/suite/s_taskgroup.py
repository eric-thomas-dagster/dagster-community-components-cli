from compat_lib import *
with dag("s_taskgroup") as d:
    with TaskGroup("grp") as g:
        a = EmptyOperator(task_id="a")
        b = BashOperator(task_id="b", bash_command="true")
        a >> b
    EmptyOperator(task_id="after") << g
