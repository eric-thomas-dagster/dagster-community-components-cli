"""Ground truth from the REAL Airflow DAG objects: operator arguments, generated COPY SQL, schedules, logical dates."""
import json, os, sys, tempfile, warnings
from unittest import mock
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(); os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
root = os.path.abspath("reusable_patched"); sys.path.insert(0, root)
from airflow.dag_processing.dagbag import DagBag
import pendulum
bag = DagBag(dag_folder=os.path.join(root, "dags"), safe_mode=False)
print("import errors:", {k[-60:]: v.strip().splitlines()[-1][:120] for k, v in bag.import_errors.items()})
WANT = [d for d in bag.dags if d.startswith(("example_01", "example_02", "example_03", "example_05", "example_06"))]
out = {}
for dag_id in sorted(WANT):
    dag = bag.dags[dag_id]
    copy = next(t for t in dag.tasks if t.task_type == "CopyFromExternalStageToSnowflakeOperator")
    lst = next(t for t in dag.tasks if t.task_type == "S3ListOperator")
    rec = {"tasks": sorted(dag.task_dict), "timezone": str(dag.timezone), "tags": list(dag.tags or []),
           "retries": copy.retries, "retry_delay_s": copy.retry_delay.total_seconds(),
           "schedule_summary": getattr(dag.timetable, "summary", str(dag.timetable)), "cron": getattr(dag.timetable, "_expression", None) or str(dag.timetable),
           "list_bucket": lst.bucket, "list_prefix_template": lst.prefix, "copy_prefix_template": copy.prefix}
    ctx = {"ds": "2026-07-01", "ds_nodash": "20260701"}
    # SQL exactly as the operator builds it, with the hook mocked so nothing executes
    import jinja2
    env = jinja2.Environment()
    sql_by_ds = {}
    for ds in ("2026-07-01", "2026-07-02"):
        c = {"ds": ds, "ds_nodash": ds.replace("-", "")}
        copy.prefix_rendered = env.from_string(copy.prefix).render(**c) if copy.prefix else copy.prefix
        orig = copy.prefix; copy.prefix = copy.prefix_rendered
        with mock.patch("airflow.providers.snowflake.transfers.copy_into_snowflake.SnowflakeHook") as H:
            copy.execute({})
            sql_by_ds[ds] = H.return_value.run.call_args.kwargs["sql"]
            rec["hook_overrides"] = {k: v for k, v in H.call_args.kwargs.items() if k in ("warehouse", "database", "role", "schema")}
        copy.prefix = orig
    rec["copy_sql"] = sql_by_ds
    rec["list_prefix_rendered"] = {ds: env.from_string(lst.prefix).render(ds=ds, ds_nodash=ds.replace("-", "")) for ds in ("2026-07-01", "2026-07-02")}
    # manifest callable
    mt = next((t for t in dag.tasks if t.task_id.endswith("build_load_manifest")), None)
    if mt is not None:
        fn = getattr(mt, "python_callable", None)
        keys = ["orders/20260701/", "orders/20260701/b.csv", "orders/20260701/a.csv"]
        rec["manifest_sample"] = fn(keys) if fn else None
    # logical-date behaviour for the first three ticks (Airflow 3 timetable API)
    from airflow.timetables.base import TimeRestriction
    from airflow.timetables.trigger import CronTriggerTimetable as SchedTT
    import datetime as _dt
    tt0 = dag.timetable
    expr = getattr(tt0, "expression", None) or getattr(tt0, "_expression")
    tz = dag.timezone; ticks = []; last = None
    sched_tt = SchedTT(expr, timezone=tz, interval=_dt.timedelta(0), run_immediately=False)
    rec["cron"] = expr
    try:
        for _ in range(27 if expr.startswith('0 *') else 3):
            nxt = sched_tt.next_dagrun_info_v2(last_dagrun_info=last, restriction=TimeRestriction(
                earliest=pendulum.datetime(2026, 7, 1, tz=tz), latest=None, catchup=True))
            ticks.append({"run_after_utc": str(nxt.run_after.in_timezone("UTC")), "logical_date_utc": str(nxt.logical_date.in_timezone("UTC")),
                          "ds": nxt.logical_date.in_timezone("UTC").strftime("%Y-%m-%d"),
                          "interval_width_s": (nxt.data_interval.end - nxt.data_interval.start).total_seconds()})
            last = nxt
    except Exception as e:
        ticks = [f"error: {type(e).__name__}: {e}"]
    rec["ticks"] = ticks
    out[dag_id] = rec
json.dump(out, open("af_side.json", "w"), indent=1, default=str)
print("airflow side:", len(out), "DAGs:", ", ".join(sorted(out)))
