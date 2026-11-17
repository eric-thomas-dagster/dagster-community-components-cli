"""One file's COPY fails. Do the other files still get archived and deleted? (Dagster side of the all-or-nothing question.)"""
import contextlib, pathlib, tempfile, warnings, yaml
warnings.filterwarnings("ignore")
import dagster as dg
from dagster_aws.s3 import S3Resource
from dagster_snowflake import SnowflakeResource
from ex04_trial.components.s3_per_file_load import S3PerFileLoad
LOG = []
BAD = "orders/20260701/b.csv"
class _Client:
    def copy_object(self, **kw): LOG.append(("archive", kw["CopySource"]["Key"])); return {}
    def delete_objects(self, **kw): LOG.append(("delete", kw["Delete"]["Objects"][0]["Key"])); return {"Deleted": []}
class FakeS3(S3Resource):
    def get_client(self): return _Client()
class _Cursor:
    def execute(self, sql):
        if sql.lstrip().startswith("COPY") and BAD in sql: raise RuntimeError("COPY failed for " + BAD)
        if sql.lstrip().startswith("COPY"): LOG.append(("copy", sql.split("FILES=('")[1].split("')")[0]))
class _Conn:
    def cursor(self): return _Cursor()
class FakeSnowflake(SnowflakeResource):
    def get_object_to_set_on_execution_context(self): return self
    @contextlib.contextmanager
    def get_connection(self, *a, **k): yield _Conn()
attrs = yaml.safe_load(pathlib.Path("src/ex04_trial/defs/example_04_per_file/defs.yaml").read_text())["attributes"]; attrs["retries"] = 0
res = {"aws_default": FakeS3(), "snowflake_default": FakeSnowflake(account="x", user="x", password="x")}
defs = S3PerFileLoad(**attrs).build_defs(None)
assets = [a for a in defs.assets if isinstance(a, dg.AssetsDefinition)]
inst = dg.DagsterInstance.local_temp(tempfile.mkdtemp())
inst.add_dynamic_partitions("example_04_s3_to_snowflake_dynamic_task_mapping_files", ["orders/20260701/a.csv", BAD, "orders/20260701/c.csv"])
for key in ("orders/20260701/a.csv", BAD, "orders/20260701/c.csv"):
    r = dg.materialize(assets, partition_key=key, instance=inst, resources=res, raise_on_error=False)
    print(f"{key:26} run success={r.success}")
print("effects:", LOG)
archived = {k for e, k in LOG if e == "archive"}; deleted = {k for e, k in LOG if e == "delete"}
print("failed file archived/deleted:", BAD in archived, BAD in deleted, "| other files archived:", sorted(archived), "| deleted:", sorted(deleted))
