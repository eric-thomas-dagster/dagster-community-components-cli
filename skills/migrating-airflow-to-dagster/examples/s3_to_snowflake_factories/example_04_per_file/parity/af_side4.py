"""Ground truth for example 4 from the REAL Airflow DAG: its own task functions, mapped-operator arguments, and
the calls its operators make (hooks mocked, nothing executes)."""
import inspect, json, os, sys, tempfile, warnings
from unittest import mock
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(); os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
root = os.path.abspath("reusable_patched"); sys.path.insert(0, root)
from airflow.dag_processing.dagbag import DagBag
import jinja2, pendulum
bag = DagBag(dag_folder=os.path.join(root, "dags"), safe_mode=False)
dag = bag.dags["example_04_s3_to_snowflake_dynamic_task_mapping"]
T = dag.task_dict
out = {"tasks": sorted(T), "types": {k: type(v).__name__ for k, v in T.items()}}
lst = T["list_landing_files"]
out["list_prefix_template"] = lst.prefix; out["bucket"] = lst.bucket
KEYS = ["orders/20260701/", "orders/20260701/b.csv", "orders/20260701/a.csv", "orders/20260701/sub dir/c d.csv"]
out["keys_in"] = KEYS
batches = T["copy_file_batches"].python_callable(KEYS)
arch = T["archive_kwargs"].python_callable(KEYS)
dele = T["delete_kwargs"].python_callable(KEYS)
out["batches"], out["archive_kwargs"], out["delete_kwargs"] = batches, arch, dele

from airflow.providers.snowflake.transfers.copy_into_snowflake import CopyFromExternalStageToSnowflakeOperator as Copy
from airflow.providers.amazon.aws.operators.s3 import S3CopyObjectOperator as S3Copy, S3DeleteObjectsOperator as S3Del
pk = lambda op: dict(op.partial_kwargs)
sql = []
for b in batches:
    allow = ("snowflake_conn_id", "stage", "table", "schema", "database", "warehouse", "role", "file_format", "copy_options", "prefix", "pattern")
    op = Copy(task_id="t", files=b, **{k: v for k, v in pk(T["copy_file_into_snowflake"]).items() if k in allow})
    with mock.patch("airflow.providers.snowflake.transfers.copy_into_snowflake.SnowflakeHook") as H:
        op.execute({}); sql.append({"files": b, "sql": H.return_value.run.call_args.kwargs["sql"],
                                    "overrides": {k: v for k, v in H.call_args.kwargs.items() if k in ("warehouse", "database", "role", "schema")}})
out["copy_sql"] = sql
copies, deletes = [], []
for kw in arch:
    op = S3Copy(task_id="c", aws_conn_id=pk(T["archive_landing_file"]).get("aws_conn_id"), **kw)
    with mock.patch.object(S3Copy, "hook", new_callable=mock.PropertyMock) as H:
        op.execute({}); c = H.return_value.copy_object.call_args
        copies.append({"args": [a for a in c.args], "kwargs": c.kwargs})
for kw in dele:
    op = S3Del(task_id="d", aws_conn_id=pk(T["delete_landing_file"]).get("aws_conn_id"), **kw)
    with mock.patch.object(S3Del, "hook", new_callable=mock.PropertyMock) as H:
        op.execute({}); c = H.return_value.delete_objects.call_args
        deletes.append({"args": list(c.args), "kwargs": c.kwargs})
out["copy_object_calls"], out["delete_objects_calls"] = copies, deletes
out["deps"] = {t: {"upstream": sorted(T[t].upstream_task_ids), "trigger_rule": str(getattr(T[t], "trigger_rule", ""))}
               for t in ("copy_file_into_snowflake", "archive_landing_file", "delete_landing_file")}
out["retries"] = pk(T["copy_file_into_snowflake"]).get("retries"); out["timezone"] = str(dag.timezone)
# schedule ticks
from airflow.timetables.base import TimeRestriction
from airflow.timetables.trigger import CronTriggerTimetable as SchedTT
import datetime as _dt
tt0 = dag.timetable; expr = getattr(tt0, "expression", None) or getattr(tt0, "_expression")
st = SchedTT(expr, timezone=dag.timezone, interval=_dt.timedelta(0), run_immediately=False)
ticks, last = [], None
for _ in range(3):
    n = st.next_dagrun_info_v2(last_dagrun_info=last, restriction=TimeRestriction(earliest=pendulum.datetime(2026, 7, 1, tz=dag.timezone), latest=None, catchup=True))
    ds = n.logical_date.in_timezone("UTC").strftime("%Y-%m-%d")
    ticks.append({"run_after_utc": str(n.run_after.in_timezone("UTC")), "ds": ds,
                  "list_prefix": jinja2.Environment().from_string(lst.prefix).render(ds_nodash=ds.replace("-", ""), ds=ds)})
    last = n
out["cron"] = expr; out["ticks"] = ticks
json.dump(out, open("af_side4.json", "w"), indent=1, default=str)
print("airflow side 4 done:", out["types"]["copy_file_into_snowflake"], "| batches", len(batches))
