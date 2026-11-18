"""Run the real Airflow S3 operators against the recording fake client; write what they called and returned."""
import json, os, sys, tempfile, warnings
from unittest import mock
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.getcwd())
import fake_s3 as S
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.amazon.aws.operators.s3 import S3CopyObjectOperator, S3DeleteObjectsOperator, S3ListOperator
from airflow.providers.amazon.aws.transfers.local_to_s3 import LocalFilesystemToS3Operator

tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt"); tmp.write(b"hello"); tmp.close()
out = {}


def step(name, op, ctx=None):
    S.CALLS.clear()
    with mock.patch.object(S3Hook, "get_conn", return_value=S.FakeClient()):
        try:
            r = op.execute(ctx or {})
            out[name] = {"result": r, "calls": [list(map(str, c)) for c in S.CALLS]}
        except Exception as e:
            out[name] = {"error": type(e).__name__, "calls": [list(map(str, c)) for c in S.CALLS]}


step("copy by url", S3CopyObjectOperator(task_id="c", source_bucket_key="s3://b/src.txt", dest_bucket_key="s3://b/dst.txt"))
step("copy by bucket+key", S3CopyObjectOperator(task_id="c", source_bucket_name="b", source_bucket_key="src.txt", dest_bucket_name="b2", dest_bucket_key="dst.txt"))
step("delete keys", S3DeleteObjectsOperator(task_id="d", bucket="b", keys=["a", "b"]))
step("delete reports an error", S3DeleteObjectsOperator(task_id="d", bucket="b", keys=["a", "bad"]))
step("delete 1500 keys", S3DeleteObjectsOperator(task_id="d", bucket="b", keys=[f"k{i}" for i in range(1500)]))
step("list prefix", S3ListOperator(task_id="l", bucket="b", prefix="p/"))
step("upload replace", LocalFilesystemToS3Operator(task_id="u", filename=tmp.name, dest_key="k/f.txt", dest_bucket="b", replace=True))
step("upload no replace, key exists", LocalFilesystemToS3Operator(task_id="u", filename=tmp.name, dest_key="exists.txt", dest_bucket="b", replace=False))
step("upload no replace, key absent", LocalFilesystemToS3Operator(task_id="u", filename=tmp.name, dest_key="k/new.txt", dest_bucket="b", replace=False))
out["_local_file"] = tmp.name
json.dump(out, open("af_out.json", "w"), indent=1); print("airflow operator scenarios:", len(out) - 1)
