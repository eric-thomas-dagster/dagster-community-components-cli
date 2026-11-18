from compat_lib import *
for name, sched in (("cron", "30 6 * * *"), ("preset", "@hourly"), ("delta", datetime.timedelta(hours=6)), ("none", None)):
    globals()[f"d_{name}"] = d = dag(f"s_sched_{name}", schedule=sched)     # a module global: Airflow before 2.4 only finds DAGs that way
    EmptyOperator(task_id="x", dag=d)
