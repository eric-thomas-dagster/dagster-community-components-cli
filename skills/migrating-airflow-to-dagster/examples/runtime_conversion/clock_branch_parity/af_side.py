import json, os, tempfile, warnings
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp()
import pendulum
from airflow.sdk import DAG
from airflow.providers.standard.operators.datetime import BranchDateTimeOperator
from airflow.providers.standard.operators.weekday import BranchDayOfWeekOperator
import cases as C
from airflow.providers.standard.utils.weekday import WeekDay
out = {}
with DAG("d", start_date=pendulum.datetime(2020, 1, 1, tz=C.TZ), schedule=None, timezone=C.TZ) if False else DAG("d", start_date=pendulum.datetime(2020, 1, 1, tz=C.TZ), schedule=None) as dag:
    for name, (lo, hi) in C.DATETIME_CASES.items():
        op = BranchDateTimeOperator(task_id="b" + str(len(out)), follow_task_ids_if_true="Y", follow_task_ids_if_false="N", target_lower=lo, target_upper=hi, use_task_logical_date=True)
        out[name] = {n.isoformat(): op.choose_branch({"logical_date": pendulum.instance(n)}) for n in C.NOWS}
    for name, wd in C.WEEKDAY_CASES.items():
        op = BranchDayOfWeekOperator(task_id="w" + str(len(out)), follow_task_ids_if_true="Y", follow_task_ids_if_false="N", week_day=({WeekDay(i) for i in wd} if wd and isinstance(wd[0], int) else wd), use_task_logical_date=True)
        out[name] = {n.isoformat(): op.choose_branch({"logical_date": pendulum.instance(n)}) for n in C.NOWS}
json.dump(out, open("af_out.json", "w"), indent=1); print("airflow cases:", len(out))
