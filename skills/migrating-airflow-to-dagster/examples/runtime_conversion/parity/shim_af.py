import json, os, sys, tempfile, warnings
from unittest import mock
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.getcwd())
import shim_scenarios as S
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
with mock.patch.object(S3Hook, "get_conn", return_value=S.FakeClient()):
    out = S.run(S3Hook(aws_conn_id="aws_default"))
json.dump(out, open("shim_af.json", "w"), indent=1); print("airflow hook scenarios:", len(out))
