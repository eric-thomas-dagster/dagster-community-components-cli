from compat_lib import *
def notify(context):
    print("failed", context["task_instance"].task_id)
with dag("s_callbacks") as d:
    BashOperator(task_id="a", bash_command="exit 1", on_failure_callback=notify, retries=1)
