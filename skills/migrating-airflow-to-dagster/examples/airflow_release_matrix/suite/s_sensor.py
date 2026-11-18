from compat_lib import *
try:
    from airflow.providers.standard.sensors.time_delta import TimeDeltaSensor
except ImportError:
    from airflow.sensors.time_delta import TimeDeltaSensor
with dag("s_sensor") as d:
    s = TimeDeltaSensor(task_id="wait", delta=datetime.timedelta(seconds=1), poke_interval=5, timeout=60)
    s >> BashOperator(task_id="go", bash_command="true")
