from compat_lib import *
with dag("s_basic") as d:
    a = BashOperator(task_id="a", bash_command="echo hello")
    b = PythonOperator(task_id="b", python_callable=say, op_kwargs={"msg": "x"}, retries=2)
    a >> b
