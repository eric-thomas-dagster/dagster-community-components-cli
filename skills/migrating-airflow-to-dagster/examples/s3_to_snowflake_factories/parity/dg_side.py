import faulthandler; faulthandler.dump_traceback_later(120, exit=True)
"""Run the converted Dagster definitions with fake S3/Snowflake resources and record what they do."""
import json, sys, contextlib, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")
import dagster as dg
from dagster_aws.s3 import S3Resource
from dagster_snowflake import SnowflakeResource
from reusable_trial.components.s3_to_snowflake_load import S3ToSnowflakeLoad, logical_date_ds, build_load_manifest
import reusable_trial.definitions as D

LIST_CALLS, SQL_CALLS = [], []
class _Pager:
    def paginate(self, Bucket, Prefix):
        LIST_CALLS.append({"bucket": Bucket, "prefix": Prefix})
        return [{"Contents": [{"Key": Prefix}, {"Key": Prefix + "b.csv"}, {"Key": Prefix + "a.csv"}]}]
class _Client:
    def get_paginator(self, name): assert name == "list_objects_v2"; return _Pager()
class FakeS3(S3Resource):
    def get_client(self): return _Client()
class _Cursor:
    def __init__(self, ctx): self.ctx = ctx
    def execute(self, sql): SQL_CALLS.append({"sql": sql, **self.ctx})
class _Conn:
    def __init__(self, ctx): self.ctx = ctx
    def cursor(self): return _Cursor(self.ctx)
class FakeSnowflake(SnowflakeResource):
    def get_object_to_set_on_execution_context(self):       # the real one attaches a SnowflakeConnection; this fake attaches itself
        return self
    @contextlib.contextmanager
    def get_connection(self, *a, **k):
        yield _Conn({})

import pathlib, yaml
ROOT = pathlib.Path("src/reusable_trial/defs")
parts = []
for f in sorted(ROOT.glob("*/defs.yaml")):
    attrs = yaml.safe_load(f.read_text())["attributes"]
    attrs["retries"] = 0                                   # surface failures now instead of after a 300 s retry delay
    attrs.pop("post_load_sql_file", None)
    parts.append(S3ToSnowflakeLoad(**attrs).build_defs(None))   # context.path is only used for post_load_sql_file, removed above
res = {"aws_default": FakeS3(), "snowflake_default": FakeSnowflake(account="x", user="x", password="x")}
defs = dg.Definitions.merge(*parts, dg.Definitions(resources=res))
# hourly orders (example_02) is not deployed (duplicate producer) but its tick semantics are checked here
hourly = S3ToSnowflakeLoad(name="example_02_s3_to_snowflake_generated_orders", schedule="0 * * * *", partitions="hourly", timezone="America/Chicago",
                           stage="RAW_ORDERS_STAGE", prefix="orders/{{ ds_nodash }}/", table="ORDERS_RAW", file_format="CSV_FILE_FORMAT")
hdefs = dg.Definitions.merge(hourly.build_defs(None), dg.Definitions(resources=res))
assets = [a for a in defs.assets if isinstance(a, dg.AssetsDefinition) and a.is_executable]
out = {"runs": {}, "ticks": {}}
for a in assets:
    key = a.key.to_user_string()
    if key.endswith("post_load_transform"): continue
    LIST_CALLS.clear(); SQL_CALLS.clear()
    r = dg.materialize([a], partition_key="2026-07-01", resources=res)
    ev = r.get_asset_materialization_events()[0].step_materialization_data.materialization.metadata
    out["runs"][key] = {"success": r.success, "list_calls": list(LIST_CALLS), "sql_calls": list(SQL_CALLS),
                        "manifest_files": ev["files"].value, "file_count": ev["file_count"].value}
# schedule ticks at the exact UTC times Airflow's timetable produced
af = json.load(open("../../af_side.json"))
sched = {s.name: s for s in [*defs.schedules, *hdefs.schedules]}
for dag_id, rec in af.items():
    s = sched.get(f"{dag_id}_schedule")
    if s is None: continue
    ticks = []
    for t in rec["ticks"]:
        when = datetime.fromisoformat(t["run_after_utc"])
        ctx = dg.build_schedule_context(scheduled_execution_time=when, repository_def=(hdefs if 'orders' in dag_id and '02' in dag_id else defs).get_repository_def())
        rr = s.evaluate_tick(ctx).run_requests[0]
        ticks.append({"run_after_utc": t["run_after_utc"], "partition_key": rr.partition_key, "dagster_ds": logical_date_ds(rr.partition_key, "America/Chicago" if "05" not in dag_id and "06" not in dag_id else "UTC")})
    out["ticks"][dag_id] = {"cron": s.cron_schedule, "tz": s.execution_timezone, "ticks": ticks}
# hourly check: Airflow's ds is the UTC date of the logical date; try a tick near the end of the local day
out["hourly_ds_rule"] = {"2026-07-01-23:00@Chicago": logical_date_ds("2026-07-01-23:00", "America/Chicago"),
                         "2026-07-01-18:00@Chicago": logical_date_ds("2026-07-01-18:00", "America/Chicago")}
out["manifest_fn"] = build_load_manifest(["orders/20260701/", "orders/20260701/b.csv", "orders/20260701/a.csv"])
json.dump(out, open("../../dg_side.json", "w"), indent=1, default=str)
print("dagster side ran:", sorted(out["runs"]))
