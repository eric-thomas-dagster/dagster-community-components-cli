from compat_lib import *
with dag("s_templates", params={"who": "world"}) as d:
    a = BashOperator(task_id="a", bash_command="echo {{ ds }} {{ params.who }}")
    def f(ds=None, **ctx):
        return ds
    b = PythonOperator(task_id="b", python_callable=f)
    a >> b
