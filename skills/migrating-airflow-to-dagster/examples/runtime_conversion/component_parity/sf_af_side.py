"""The COPY INTO statement Airflow's operator builds, for several argument sets (the hook is mocked, nothing runs)."""
import json, os, tempfile, warnings
from unittest import mock
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp()
from airflow.providers.snowflake.transfers.copy_into_snowflake import CopyFromExternalStageToSnowflakeOperator as Op
CASES = {
    "prefix + format string": dict(table="T", stage="ST", prefix="orders/20260701/", file_format="(type = 'CSV' field_delimiter = ',')", schema="RAW"),
    "files list + named format": dict(table="T", stage="ST", files=["a.csv", "b.csv"], file_format="(format_name = 'MYFMT')", schema="RAW"),
    "pattern + copy options": dict(table="T", stage="ST", pattern=".*[.]csv", file_format="(type = 'CSV')", copy_options="ON_ERROR = CONTINUE", schema="RAW"),
    "validation mode": dict(table="T", stage="ST", prefix="p/", file_format="(type = 'JSON')", validation_mode="VALIDATION_MODE = RETURN_ERRORS", schema="RAW"),
}
out = {}
for name, kw in CASES.items():
    op = Op(task_id="c", snowflake_conn_id="snowflake_default", **kw)
    with mock.patch("airflow.providers.snowflake.transfers.copy_into_snowflake.SnowflakeHook") as H:
        op.execute({})
        out[name] = {"sql": H.return_value.run.call_args.kwargs["sql"], "args": kw}
json.dump(out, open("sf_af_out.json", "w"), indent=1); print("airflow copy statements:", len(out))
