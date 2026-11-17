import faulthandler; faulthandler.dump_traceback_later(150, exit=True)
"""Run the dynamic-partition conversion with fake S3/Snowflake and record what it does; compare to af_side4.json."""
import contextlib, json, pathlib, warnings, yaml
from datetime import datetime
warnings.filterwarnings("ignore")
import dagster as dg
from dagster_aws.s3 import S3Resource
from dagster_snowflake import SnowflakeResource
from ex04_trial.components.s3_per_file_load import S3PerFileLoad

EVENTS = []                                                   # ordered log of every side effect
FILES_UNDER = lambda prefix: [prefix, prefix + "b.csv", prefix + "a.csv", prefix + "sub dir/c d.csv"]
EMPTY_DAY = {"orders/20260703/"}
class _Pager:
    def paginate(self, Bucket, Prefix):
        EVENTS.append(("list", Prefix)); return [{"Contents": [{"Key": k} for k in ([] if Prefix in EMPTY_DAY else FILES_UNDER(Prefix))]}]
class _Client:
    def get_paginator(self, name): assert name == "list_objects_v2"; return _Pager()
    def copy_object(self, **kw): EVENTS.append(("copy_object", kw)); return {}
    def delete_objects(self, **kw): EVENTS.append(("delete_objects", kw)); return {"Deleted": [{"Key": o["Key"]} for o in kw["Delete"]["Objects"]]}
class FakeS3(S3Resource):
    def get_client(self): return _Client()
class _Cursor:
    def execute(self, sql): EVENTS.append(("sql", sql))
class _Conn:
    def cursor(self): return _Cursor()
class FakeSnowflake(SnowflakeResource):
    def get_object_to_set_on_execution_context(self): return self
    @contextlib.contextmanager
    def get_connection(self, *a, **k): yield _Conn()

attrs = yaml.safe_load(pathlib.Path("src/ex04_trial/defs/example_04_per_file/defs.yaml").read_text())["attributes"]
attrs["retries"] = 0
res = {"aws_default": FakeS3(), "snowflake_default": FakeSnowflake(account="x", user="x", password="x")}
comp = S3PerFileLoad(**attrs)
defs = dg.Definitions.merge(comp.build_defs(None), dg.Definitions(resources=res))
af = json.load(open("../../af_side4.json"))
import tempfile
inst = dg.DagsterInstance.local_temp(tempfile.mkdtemp())   # a persistent instance has a ref, which resource initialisation in schedule contexts needs
sched = defs.schedules[0]
assets = [a for a in defs.assets if isinstance(a, dg.AssetsDefinition) and a.is_executable]
out = {"ticks": []}
for t in af["ticks"]:
    EVENTS.clear()
    when = datetime.fromisoformat(t["run_after_utc"])
    ctx = dg.build_schedule_context(scheduled_execution_time=when, instance=inst, resources=res, repository_def=defs.get_repository_def())
    r = sched.evaluate_tick(ctx)
    rec = {"run_after_utc": t["run_after_utc"], "listed": [e[1] for e in EVENTS if e[0] == "list"],
           "skip": r.skip_message, "requests": [x.partition_key for x in r.run_requests],
           "partitions_now": sorted(inst.get_dynamic_partitions(comp.partitions_name)), "runs": {}}
    for key in rec["requests"]:
        EVENTS.clear()
        run = dg.materialize(assets, partition_key=key, instance=inst, resources=res)
        rec["runs"][key] = {"success": run.success, "events": [list(e) for e in EVENTS]}
    out["ticks"].append(rec)
# idempotence: evaluating the first tick again must not duplicate partitions
before = len(inst.get_dynamic_partitions(comp.partitions_name))
sched.evaluate_tick(dg.build_schedule_context(scheduled_execution_time=datetime.fromisoformat(af["ticks"][0]["run_after_utc"]), instance=inst, resources=res, repository_def=defs.get_repository_def()))
out["partitions_after_reevaluating_tick1"] = len(inst.get_dynamic_partitions(comp.partitions_name)); out["partitions_before"] = before
out["cron"] = sched.cron_schedule; out["tz"] = sched.execution_timezone
json.dump(out, open("../../dg_side4.json", "w"), indent=1, default=str)
print("dagster side 4 done; partitions:", out["partitions_after_reevaluating_tick1"])
