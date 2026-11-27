"""Resource-first `--output components` (registry_map.py): a service-calling operator becomes a resource (a registry `*_resource` component in
defs/registry_resources/defs.yaml, or resources.py) plus an asset in defs/<dag>_steps.py that calls it in plain Python. Each generated project is
LOADED with dagster's own component loader against the registry release the converter targets (0.10.193) and the asset is RUN against a local
stand-in (an HTTP server) or a recording fake of the client library. The registry 0.10.193 package is read from EVAL_REGISTRY_193
(default: ../reg193 next to the interpreter's venv); the tests skip when it is absent."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import registry_map as RM  # noqa: E402
from test_convert_ir import dag, task, fn  # noqa: E402
from test_provider_ops import op, ir_of, web  # noqa: E402,F401

dg = pytest.importorskip("dagster")


def _reg193_pkg():
    env = os.environ.get("EVAL_REGISTRY_193")
    cands = [Path(env)] if env else [Path(sys.executable).parent.parent.parent / "reg193" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages" / "dagster_community_components"]
    return next((c for c in cands if c.exists()), None)


@pytest.fixture
def reg193(tmp_path_factory):
    src = _reg193_pkg()
    if src is None:
        pytest.skip("registry 0.10.193 is not installed (set EVAL_REGISTRY_193 to its dagster_community_components folder)")
    d = tmp_path_factory.mktemp("reg193")
    (d / "dagster_community_components").symlink_to(src)
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "dagster_community_components"}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, str(d))
    importlib.invalidate_caches()
    yield
    for k in [k for k in sys.modules if k.split(".")[0] == "dagster_community_components"]:
        del sys.modules[k]
    sys.modules.update(saved)
    sys.path.remove(str(d))


def _convert(tmp_path, ir, pkg):
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname="{pkg}"\nversion="0"\n')
    (tmp_path / pkg).mkdir(exist_ok=True)
    (tmp_path / pkg / "__init__.py").touch()
    (tmp_path / pkg / "defs").mkdir(exist_ok=True)
    (tmp_path / pkg / "defs" / "__init__.py").touch()
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"), "--report", str(tmp_path / "r.json"),
                    "--output", "components"], check=True, capture_output=True)
    return json.loads((tmp_path / "r.json").read_text())


def _load(tmp_path, pkg):
    """The whole generated project, loaded by dagster's component loader."""
    from dagster.components import load_defs
    sys.path.insert(0, str(tmp_path))
    try:
        mod = importlib.import_module(f"{pkg}.defs")
        return load_defs(mod, project_root=tmp_path)
    finally:
        sys.path.remove(str(tmp_path))


def _run(defs, key=None, **extra_resources):
    assets = [a for a in defs.assets if hasattr(a, "keys")]
    res = dg.materialize(assets, resources={**defs.resources, **extra_resources}, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:300] for e in res.get_step_failure_events()]
    return res


def test_mapping_table_names_only_resources_that_exist_in_the_targeted_release(reg193):
    """Every resource the converter can emit is in the installed registry, with the fields its YAML uses; the table rows name real components."""
    import dagster_community_components as reg
    for typ, rid in RM.RESOURCE_TYPE_IDS.items():
        assert hasattr(reg, typ), (typ, rid)
    assert RM.ANNOUNCED_NOT_VERIFIED == []
    assert {r["shape"] for r in RM.OPERATOR_MAP} == {"asset", "resource", "plain"}


def test_http_operator_is_http_resource_plus_a_plain_call_run_against_a_server(tmp_path, monkeypatch, web, reg193):
    t = op("call", "HttpOperator", args={"http_conn_id": "partner_api", "method": "POST", "endpoint": "v1/items", "data": {"a": "1"}, "headers": {"X-T": "y"}, "extra_options": {}},
           tf={"endpoint": "v1/items", "data": {"a": "1"}, "headers": {"X-T": "y"}, "http_conn_id": "partner_api"})
    rep = _convert(tmp_path, ir_of(t), "pkg_rr_http")["dags"]["d1"]
    assert rep["output"] == "components" and rep["resource_types"] == ["HTTPResourceComponent"] and rep["registry_resources"] == ["partner_api"]
    assert "dagster-community-components>=0.10.193" in rep["requirements"] and "requests" in rep["requirements"]
    yml = (tmp_path / "pkg_rr_http" / "defs" / "registry_resources" / "defs.yaml").read_text()
    assert "HTTPResourceComponent" in yml and "{{ env.PARTNER_API_BASE_URL }}" in yml and "auth_header_env_var" not in yml
    assert any("auth_header_env_var" in c["reason"] for c in rep["caveats"])                     # the gap is stated, not hidden
    monkeypatch.setenv("PARTNER_API_BASE_URL", web.url + "/api")
    defs = _load(tmp_path, "pkg_rr_http")
    _run(defs, "d1__call")
    got = web.seen[-1]
    assert got["method"] == "POST" and got["path"] == "/api/v1/items" and got["body"] == "a=1" and got["headers"]["X-T"] == "y"
    assert got["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    web.script.append((500, "no"))
    with pytest.raises(AssertionError):                                                          # a 500 fails the step, as Airflow's HttpOperator does
        _run(defs, "d1__call")


def test_http_get_sends_data_as_a_query_string_and_check_response_false_does_not_fail(tmp_path, monkeypatch, web, reg193):
    t = op("g", "SimpleHttpOperator", args={"http_conn_id": "c1", "method": "GET", "endpoint": "ping", "data": {"q": "z"}, "extra_options": {"check_response": False}},
           tf={"endpoint": "ping", "data": {"q": "z"}, "http_conn_id": "c1"})
    rep = _convert(tmp_path, ir_of(t), "pkg_rr_get")["dags"]["d1"]
    assert rep["output"] == "components"
    monkeypatch.setenv("C1_BASE_URL", web.url)
    web.script.append((404, "gone"))
    _run(_load(tmp_path, "pkg_rr_get"), "d1__g")
    assert web.seen[-1]["path"] == "/ping?q=z"


class _Fake:
    def __init__(self):
        self.calls = []


def test_databricks_run_now_is_databricks_jobs_resource_run_against_a_fake_client(tmp_path, monkeypatch, reg193):
    t = op("job", "DatabricksRunNowOperator", args={"databricks_conn_id": "dbx", "json": {"job_id": "42", "notebook_params": {"d": "1"}}, "wait_for_termination": True},
           tf={"json": {"job_id": "42", "notebook_params": {"d": "1"}}, "databricks_conn_id": "dbx"})
    rep = _convert(tmp_path, ir_of(t), "pkg_rr_dbx")["dags"]["d1"]
    assert rep["resource_types"] == ["DatabricksJobsResourceComponent"] and "databricks-sdk" in rep["requirements"]
    yml = (tmp_path / "pkg_rr_dbx" / "defs" / "registry_resources" / "defs.yaml").read_text()
    assert 'resource_key: "dbx__jobs"' in yml and "workspace_token_env_var: \"DBX_TOKEN\"" in yml and "{{ env.DBX_HOST }}" in yml
    monkeypatch.setenv("DBX_HOST", "https://h.example")
    monkeypatch.setenv("DBX_TOKEN", "tok")
    fake = _Fake()

    class State:
        life_cycle_state, result_state, state_message = "TERMINATED", "SUCCESS", ""

    class Client:
        class jobs:
            @staticmethod
            def run_now(**kw):
                fake.calls.append(kw)
                return type("W", (), {"response": type("R", (), {"run_id": 7})()})()

            @staticmethod
            def wait_get_run_job_terminated_or_skipped(run_id, timeout):
                return type("Run", (), {"state": State, "run_page_url": "http://u"})()
    from dagster_community_components.resources.databricks_jobs_resource.component import DatabricksJobsResource
    monkeypatch.setattr(DatabricksJobsResource, "_client", lambda self: Client)
    _run(_load(tmp_path, "pkg_rr_dbx"), "d1__job")
    assert fake.calls == [{"job_id": 42, "notebook_params": {"d": "1"}}]


def test_spark_submit_is_spark_submit_resource_run_with_a_stand_in_binary(tmp_path, monkeypatch, reg193):
    out = tmp_path / "argv.txt"
    binary = tmp_path / "fake-spark-submit"
    binary.write_text(f"#!/bin/sh\necho \"$@\" > {out}\n")
    binary.chmod(0o755)
    a = {"conn_id": "spark_x", "application": "/jobs/etl.py", "application_args": ["--d", "1"], "conf": {"spark.a": "b"}, "executor_memory": "2g", "num_executors": 3, "name": "etl",
         "spark_binary": str(binary), "packages": "p:q:1,r:s:2"}
    rep = _convert(tmp_path, ir_of(op("sp", "SparkSubmitOperator", args=a, tf={k: v for k, v in a.items()})), "pkg_rr_spark")["dags"]["d1"]
    assert rep["resource_types"] == ["SparkSubmitResourceComponent"]
    monkeypatch.setenv("SPARK_X_MASTER", "local[2]")
    _run(_load(tmp_path, "pkg_rr_spark"), "d1__sp")
    argv = out.read_text()
    for needle in ("--master local[2]", "--conf spark.a=b", "--executor-memory 2g", "--num-executors 3", "--name etl", "--packages p:q:1,r:s:2", "/jobs/etl.py --d 1"):
        assert needle in argv, (needle, argv)


def test_bucket_lifecycle_operators_use_the_bucket_resources(tmp_path, reg193):
    ops = [op("mk", "S3CreateBucketOperator", args={"aws_conn_id": "aws_x", "bucket_name": "nb", "region_name": "eu-west-1"}, tf={"bucket_name": "nb", "region_name": "eu-west-1", "aws_conn_id": "aws_x"}),
           op("gmk", "GCSCreateBucketOperator", up=["mk"], args={"gcp_conn_id": "g", "bucket_name": "gb", "location": "EU"}, tf={"bucket_name": "gb", "location": "EU"}),
           op("rm", "S3DeleteBucketOperator", up=["gmk"], args={"aws_conn_id": "aws_x", "bucket_name": "nb", "force_delete": True}, tf={"bucket_name": "nb", "aws_conn_id": "aws_x"}),
           op("grm", "GCSDeleteBucketOperator", up=["rm"], args={"gcp_conn_id": "g", "bucket_name": "gb"}, tf={"bucket_name": "gb"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg_rr_bkt")["dags"]["d1"]
    assert rep["resource_types"] == ["GcsBucketResourceComponent", "S3BucketResourceComponent"]
    calls = []
    import botocore.stub  # noqa: F401  (botocore is installed with boto3)
    import boto3

    class S3:
        def __init__(self):
            from botocore.exceptions import ClientError
            self.ClientError = ClientError

        def create_bucket(self, **kw):
            calls.append(("s3create", kw))

        def head_bucket(self, **kw):
            return {}

        def get_paginator(self, name):
            return type("P", (), {"paginate": lambda s, **kw: [{"Versions": [{"Key": "k", "VersionId": "1"}]}]})()

        def delete_objects(self, **kw):
            calls.append(("s3rm", kw))

        def delete_bucket(self, **kw):
            calls.append(("s3del", kw))

    mp = pytest.MonkeyPatch()
    mp.setattr(boto3, "client", lambda *a, **k: S3())
    from dagster_community_components.resources.gcs_bucket_resource.component import GcsBucketResource

    class GC:
        def bucket(self, n):
            return type("B", (), {"storage_class": None})()

        def create_bucket(self, b, location):
            calls.append(("gcscreate", location))

        def get_bucket(self, n):
            return type("B", (), {"delete": lambda s, force=False: calls.append(("gcsdel", force))})()

        def list_blobs(self, n):
            return []
    mp.setattr(GcsBucketResource, "_client", lambda self: GC())
    try:
        import google.api_core.exceptions  # noqa: F401
    except ImportError:                              # google-cloud-storage is not in this interpreter: the resource only needs the exception classes
        import types
        exc = types.ModuleType("google.api_core.exceptions")
        exc.Conflict = type("Conflict", (Exception,), {})
        exc.NotFound = type("NotFound", (Exception,), {})
        for name, m in (("google", types.ModuleType("google")), ("google.api_core", types.ModuleType("google.api_core")), ("google.api_core.exceptions", exc)):
            mp.setitem(sys.modules, name, m)
    try:
        _run(_load(tmp_path, "pkg_rr_bkt"), "d1__grm")
    finally:
        mp.undo()
    assert ("s3create", {"Bucket": "nb", "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"}}) in calls
    assert ("s3del", {"Bucket": "nb"}) in calls and ("gcscreate", "EU") in calls and ("gcsdel", False) in calls and any(c[0] == "s3rm" for c in calls)    # force_delete emptied the bucket first


def test_operators_without_a_registry_resource_use_plain_python_steps_with_the_existing_shims(tmp_path):
    ops = [op("lam", "LambdaInvokeFunctionOperator", args={"function_name": "f", "payload": {"n": 1}, "region_name": "us-east-1", "aws_conn_id": "aws_default"}),
           op("sh", "SSHOperator", up=["lam"], args={"ssh_conn_id": "ssh_default", "command": "ls /data", "cmd_timeout": 60}),
           op("dk", "DockerOperator", up=["sh"], args={"image": "busybox:1", "command": "echo hi", "environment": {"A": "1"}, "network_mode": "host"}),
           op("bq", "BigQueryInsertJobOperator", up=["dk"], args={"configuration": {"query": {"query": "select 1", "useLegacySql": False}}, "location": "EU", "project_id": "p", "gcp_conn_id": "google_cloud_default"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg_rr_plain")["dags"]["d1"]
    assert rep["output"] == "components" and rep["resource_types"] == []
    yml = (tmp_path / "pkg_rr_plain" / "defs" / "d1" / "defs.yaml").read_text()
    assert "DockerContainerAssetComponent" in yml and "LambdaInvokeAssetComponent" not in yml and "SSHAssetComponent" not in yml and "BigQueryQueryAssetComponent" not in yml
    steps = (tmp_path / "pkg_rr_plain" / "defs" / "d1_steps.py").read_text()
    assert "lambda_invoke(" in steps and "ssh_run(" in steps and "bigquery_query(" in steps
    assert "dagster-ssh" in rep["requirements"] and "dagster-gcp" in rep["requirements"]
    res = (tmp_path / "pkg_rr_plain" / "defs" / "resources.py").read_text()
    assert "SSHResource" in res and "BigQueryResource" in res
    compile(steps, "steps", "exec")


def test_a_resource_key_with_different_settings_in_another_dag_keeps_that_dag_python(tmp_path, reg193):
    a = {"conn_id": "spark_x", "application": "/a.py", "spark_binary": "spark-submit"}
    b = {"conn_id": "spark_x", "application": "/b.py", "spark_binary": "/opt/spark/bin/spark-submit"}
    d1 = dag([op("sp", "SparkSubmitOperator", args=a, tf=dict(a))])
    d2 = {**dag([op("sp", "SparkSubmitOperator", args=b, tf=dict(b))]), "dag_id": "d2"}
    rep = _convert(tmp_path, {"airflow_version": "2.10.5", "dags": [d1, d2]}, "pkg_rr_conf")["dags"]
    assert rep["d1"]["output"] == "components" and rep["d2"]["output"] == "python"


def test_every_resource_announced_for_0_10_193_loads_in_a_project_with_the_fields_read_from_its_schema(tmp_path, reg193, monkeypatch):
    """The resources the converter does not emit yet (no operator mapping is written) were still read (schema.json, component.py) and loaded by dagster's component loader here."""
    docs = {"emr_resource": ("EmrResourceComponent", {"region_name": "us-east-1"}), "dataproc_resource": ("DataprocResourceComponent", {}), "pubsub_resource": ("PubsubResourceComponent", {}),
            "bigquery_ddl_resource": ("BigQueryDdlResourceComponent", {"project_id": "p"}), "athena_resource": ("AthenaResourceComponent", {"region_name": "us-east-1"}),
            "redshift_data_api_resource": ("RedshiftDataApiResourceComponent", {"region_name": "us-east-1"}), "dbt_command_resource": ("DbtCommandResourceComponent", {"project_dir": "/p"}),
            "http_resource": ("HTTPResourceComponent", {"base_url": "http://x", "auth_type": "oauth2_client_credentials", "oauth2_token_url": "http://t", "oauth2_client_id_env_var": "CID",
                                                        "oauth2_client_secret_env_var": "CSEC", "retries": 3, "retry_backoff_seconds": 0.1})}
    (tmp_path / "pyproject.toml").write_text('[project]\nname="pkg_rr_all"\nversion="0"\n')
    d = tmp_path / "pkg_rr_all" / "defs"
    for rid, (typ, attrs) in docs.items():
        (d / rid).mkdir(parents=True)
        (d / rid / "defs.yaml").write_text(f"type: dagster_community_components.{typ}\nattributes:\n  resource_key: {rid}\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in attrs.items()))
    (tmp_path / "pkg_rr_all" / "__init__.py").touch()
    (d / "__init__.py").touch()
    defs = _load(tmp_path, "pkg_rr_all")
    assert set(defs.resources) == set(docs)


def test_the_asset_components_the_converter_keeps_load_and_run_from_registry_0_10_193(tmp_path, monkeypatch, reg193):
    """Shell command, Python callable, wait and cron schedule: each kept as an asset component, materialised from the project's own defs.yaml with the 0.10.193 classes (Docker needs a daemon: its schema was read and its document validated only)."""
    sn = {"poke_interval": 0.1, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False}
    out = tmp_path / "touched.txt"
    ops = [op("wait", "BashSensor", args={"retry_exit_code": 3}, tf={"bash_command": "exit 0", "env": None}, sensor=sn),
           task("sh", "BashOperator", up=["wait"], args={}, template_fields={"bash_command": f"echo hi > {out}", "env": None, "cwd": None}),
           task("numbers", "PythonOperator", up=["sh"], callable=fn("numbers"), op_args=[], op_kwargs={}, template_fields={})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg_rr_keep")["dags"]["d1"]
    assert rep["output"] == "components" and rep["component_types"] == ["CronScheduleComponent", "PythonCallableAssetComponent", "ShellCommandAssetComponent", "WaitUntilAssetComponent"], rep
    assert rep["resource_types"] == []
    monkeypatch.syspath_prepend(str(tmp_path))
    defs = _load(tmp_path, "pkg_rr_keep")
    _run(defs)
    assert out.read_text().strip() == "hi"
