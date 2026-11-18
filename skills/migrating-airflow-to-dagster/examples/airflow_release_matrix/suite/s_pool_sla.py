from compat_lib import *
import datetime as dt
kw = {"sla": dt.timedelta(hours=1)} if VERSION < (3, 0) else {}
with dag("s_pool_sla") as d:
    BashOperator(task_id="a", bash_command="true", pool="default_pool", pool_slots=1, execution_timeout=dt.timedelta(minutes=5), **kw)
