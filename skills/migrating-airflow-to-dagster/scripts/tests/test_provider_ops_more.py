"""Slack API post, S3 key sensor and list prefixes, GCS, SSH, Docker, Lambda, BigQuery query jobs and Spark submit (provider_ops_more.py).

Each converted DAG is loaded and run with a recording fake of the system (or a stand-in): a fake Slack/GCS/SSH/BigQuery resource, a fake S3 client, a fake `docker`
module, botocore's Stubber (which validates the Lambda request against the real service model), a stand-in `spark-submit` executable. None ran against the real service; every
mapping carries an UNVERIFIED degraded caveat that says so."""
import io
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import convert_ir as C  # noqa: E402
from test_convert_ir import _convert_and_load, dag, task  # noqa: E402

dg = pytest.importorskip("dagster")


def run(tmp_path, ty, args, pkg, resources=None, tf=None, extra_tasks=()):
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", ty, args=args, template_fields=tf or {}, callable_args={}), *extra_tasks], kind="once")]}
    rep, mod = _convert_and_load(tmp_path, ir, pkg)
    assets = [getattr(mod, n) for n in dir(mod) if n.startswith("t_")]
    defs = dg.Definitions(assets=assets, resources={k: dg.ResourceDefinition.hardcoded_resource(v) for k, v in (resources or {}).items()})
    res = defs.resolve_implicit_global_asset_job_def().execute_in_process(raise_on_error=False)
    return rep, res, mod


def deg(rep, word):
    return any(c["kind"] == "degraded" and word in c["reason"] for c in rep["caveats"])


class Rec:
    def __init__(self):
        self.calls = []


def test_slack_api_post_calls_chat_post_message_with_real_parameter_names(tmp_path):
    slack_sdk = pytest.importorskip("slack_sdk")
    import inspect
    rec = Rec()

    class Client:
        def chat_postMessage(self, **kw):
            rec.calls.append(kw)
            return {"ok": True}

    class Res:
        def get_client(self):
            return Client()
    rep, res, _ = run(tmp_path, "SlackAPIPostOperator", {"slack_conn_id": "slack_api_default", "channel": "#data", "text": "loaded", "username": "airflow"}, "p_slack", {"slack_api_default": Res()})
    assert res.success and rec.calls == [{"channel": "#data", "text": "loaded", "username": "airflow"}]
    assert set(rec.calls[0]) <= set(inspect.signature(slack_sdk.WebClient.chat_postMessage).parameters)       # the keyword names are the SDK's own
    assert deg(rep, "UNVERIFIED") and "SlackResource" in (tmp_path / "p_slack" / "defs" / "resources.py").read_text()


def test_unsupported_slack_and_other_arguments_are_loud_stubs(tmp_path):
    for ty, args, word in (("SlackAPIPostOperator", {"api_params": {"x": 1}}, "api_params"), ("DockerOperator", {"image": "i", "mounts": ["m"]}, "mounts"),
                           ("SSHOperator", {"command": "ls", "environment": {"A": "1"}}, "environment"), ("SparkSubmitOperator", {"application": "a.py", "keytab": "k"}, "keytab"),
                           ("BigQueryInsertJobOperator", {"configuration": {"load": {"x": 1}}}, "plain query job")):
        ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", ty, args=args, template_fields={}, callable_args={})], kind="once")]}
        (tmp_path / f"{ty}.json").write_text(json.dumps(ir))
        import subprocess
        subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / f"{ty}.json"), "--out-dir", str(tmp_path / ty / "defs"), "--report", str(tmp_path / f"{ty}.rep.json")], check=True, capture_output=True)
        rep = json.loads((tmp_path / f"{ty}.rep.json").read_text())["dags"]["d1"]
        stubs = [c for c in rep["caveats"] if c["kind"] == "stub"]
        assert len(stubs) == 1 and word in stubs[0]["reason"], (ty, rep["caveats"])


def test_s3_key_sensor_and_list_prefixes(tmp_path):
    class Client:
        def __init__(self):
            self.keys = {"in/a.csv", "in/b.csv"}
            self.exceptions = types.SimpleNamespace(ClientError=type("ClientError", (Exception,), {}))

        def head_object(self, Bucket, Key):
            if Key not in self.keys:
                e = self.exceptions.ClientError()
                e.response = {"ResponseMetadata": {"HTTPStatusCode": 404}}
                raise e

        def get_paginator(self, name):
            keys = self.keys
            return types.SimpleNamespace(paginate=lambda **kw: [{"Contents": [{"Key": k} for k in sorted(keys)], "CommonPrefixes": [{"Prefix": "in/"}, {"Prefix": "out/"}]}])
    rep, res, mod = run(tmp_path, "S3KeySensor", {"bucket_key": "in/a.csv", "bucket_name": "b", "aws_conn_id": "aws_default", "wildcard_match": False}, "p_s3k", {"aws_default": Client()},
                        tf={"bucket_key": "in/a.csv", "bucket_name": "b"})
    assert res.success and deg(rep, "UNVERIFIED") and "S3Resource" in (tmp_path / "p_s3k" / "defs" / "resources.py").read_text()
    c = Client()
    assert mod.s3_key_exists(c, "in/a.csv", "b", False) and not mod.s3_key_exists(c, "in/zzz.csv", "b", False)
    assert mod.s3_key_exists(c, "in/*.csv", "b", True) and not mod.s3_key_exists(c, "out/*.csv", "b", True)
    assert mod.s3_key_exists(c, ["in/a.csv", "in/b.csv"], "b", False) and not mod.s3_key_exists(c, ["in/a.csv", "in/x"], "b", False)
    rep2, res2, _ = run(tmp_path, "S3ListPrefixesOperator", {"bucket": "b", "prefix": "", "delimiter": "/", "aws_conn_id": "aws_default"}, "p_s3p", {"aws_default": Client()},
                        tf={"bucket": "b", "prefix": "", "delimiter": "/", "aws_conn_id": "aws_default"})
    assert res2.success and mod.s3_list_prefixes(Client(), "b", "", "/") == ["in/", "out/"]


def test_gcs_upload_download_list_delete_and_exists(tmp_path):
    rec = Rec()

    class Blob:
        def __init__(self, name):
            self.name = name

        def upload_from_filename(self, p, content_type=None):
            rec.calls.append(("upload", self.name, p, content_type))

        def download_to_filename(self, p):
            rec.calls.append(("download", self.name, p))

        def delete(self):
            rec.calls.append(("delete", self.name))

        def exists(self):
            return self.name == "present"

    class Client:
        def bucket(self, b):
            return types.SimpleNamespace(blob=lambda n: Blob(n))

        def list_blobs(self, b, **kw):
            rec.calls.append(("list", b, kw))
            return [Blob("x/1"), Blob("x/2")]

    class Res:
        def get_client(self):
            return Client()
    f = tmp_path / "up.txt"
    f.write_text("hi")
    for ty, args in (("LocalFilesystemToGCSOperator", {"src": str(f), "dst": "d/up.txt", "bucket": "bk", "mime_type": "text/plain"}), ("GCSToLocalFilesystemOperator", {"bucket": "bk", "object_name": "o", "filename": str(tmp_path / "dl.txt")}),
                     ("GCSListObjectsOperator", {"bucket": "bk", "prefix": "x/"}), ("GCSDeleteObjectsOperator", {"bucket_name": "bk", "objects": ["a", "b"]})):
        rep, res, _ = run(tmp_path, ty, {**args, "gcp_conn_id": "google_cloud_default"}, "p_" + ty[:6], {"google_cloud_default": Res()})
        assert res.success, (ty, [str(e.event_specific_data.error)[:200] for e in res.get_step_failure_events()])
        assert deg(rep, "UNVERIFIED")
    assert ("upload", "d/up.txt", str(f), "text/plain") in rec.calls and ("download", "o", str(tmp_path / "dl.txt")) in rec.calls
    assert ("list", "bk", {"prefix": "x/"}) in rec.calls and [c for c in rec.calls if c[0] == "delete"] == [("delete", "a"), ("delete", "b")]
    rep, res, mod = run(tmp_path, "GCSObjectExistenceSensor", {"bucket": "bk", "object": "present", "gcp_conn_id": "google_cloud_default"}, "p_gcsx", {"google_cloud_default": Res()})
    assert res.success and mod.gcs_exists(Res(), "bk", "present") and not mod.gcs_exists(Res(), "bk", "absent")
    assert "GCSResource" in (tmp_path / "p_gcsx" / "defs" / "resources.py").read_text()


def test_ssh_command_value_and_failure(tmp_path):
    class Chan:
        def __init__(self, code):
            self.code = code

        def recv_exit_status(self):
            return self.code

    class Stream:
        def __init__(self, data, code=0):
            self.data, self.channel = data, Chan(code)

        def read(self):
            return self.data

    class Conn:
        def __init__(self, code):
            self.code = code

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec_command(self, cmd, timeout=None):
            self.cmd = cmd
            return None, Stream(b"hello\n", self.code), Stream(b"warn", self.code)

    class Res:
        def __init__(self, code=0):
            self.code = code

        def get_connection(self):
            return Conn(self.code)
    rep, res, mod = run(tmp_path, "SSHOperator", {"ssh_conn_id": "ssh_default", "command": "echo hello", "cmd_timeout": 30}, "p_ssh", {"ssh_default": Res()}, tf={"command": "echo hello"})
    assert res.success and mod.ssh_run(Res(), "echo hello", 5) == "hello\n" and deg(rep, "base64")
    with pytest.raises(dg.Failure, match="status 3"):
        mod.ssh_run(Res(3), "false")
    assert "SSHResource" in (tmp_path / "p_ssh" / "defs" / "resources.py").read_text()


def test_docker_run_streams_logs_and_fails_on_a_non_zero_exit(tmp_path, monkeypatch):
    rec = Rec()

    class Container:
        def __init__(self, code):
            self.code = code

        def wait(self):
            return {"StatusCode": self.code}

        def logs(self):
            return b"step one\nlast line\n"

        def remove(self, force=False):
            rec.calls.append(("remove", force))

    class Client:
        code = 0

        def __init__(self, base_url=None):
            rec.calls.append(("client", base_url))
            self.images = types.SimpleNamespace(pull=lambda i: rec.calls.append(("pull", i)))
            self.containers = types.SimpleNamespace(run=lambda image, command, **kw: (rec.calls.append(("run", image, command, kw)), Container(Client.code))[1])
    fake = types.ModuleType("docker")
    fake.DockerClient = Client
    monkeypatch.setitem(sys.modules, "docker", fake)
    rep, res, mod = run(tmp_path, "DockerOperator", {"image": "busybox:1", "command": "echo hi", "environment": {"A": "1"}, "network_mode": "host", "auto_remove": "success", "force_pull": True}, "p_dock",
                        tf={"image": "busybox:1", "command": "echo hi", "environment": {"A": "1"}})
    assert res.success and deg(rep, "UNVERIFIED")
    run_call = next(c for c in rec.calls if c[0] == "run")
    assert run_call[1:3] == ("busybox:1", "echo hi") and run_call[3]["environment"] == {"A": "1"} and run_call[3]["network_mode"] == "host" and ("remove", True) in rec.calls and ("pull", "busybox:1") in rec.calls
    Client.code = 2
    with pytest.raises(dg.Failure, match="code 2"):
        mod.docker_run("i", "c", None, "unix://x", None, None, None, False, None, False, False)


def test_lambda_invoke_validates_the_request_with_botocores_stubber(tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    real = boto3.client("lambda", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    body = json.dumps({"ok": True}).encode()
    with Stubber(real) as st:
        st.add_response("invoke", {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(body), len(body))},
                        {"FunctionName": "f", "InvocationType": "RequestResponse", "Payload": '{"n": 1}', "Qualifier": "PROD"})             # a wrong parameter name or type fails here
        monkeypatch.setattr(boto3, "client", lambda *a, **k: real)
        rep, res, mod = run(tmp_path, "LambdaInvokeFunctionOperator", {"function_name": "f", "payload": {"n": 1}, "qualifier": "PROD", "aws_conn_id": "aws_default"}, "p_lam",
                            tf={"function_name": "f", "payload": {"n": 1}, "qualifier": "PROD"})
        assert res.success and deg(rep, "stubber")
        st.assert_no_pending_responses()
    with Stubber(real) as st:
        st.add_response("invoke", {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": StreamingBody(io.BytesIO(b'{"errorMessage": "x"}'), 21)})
        with pytest.raises(dg.Failure, match="Unhandled"):
            mod.lambda_invoke("f", None)


def test_bigquery_query_job_uses_a_real_query_job_config(tmp_path):
    bigquery = pytest.importorskip("google.cloud.bigquery")
    rec = Rec()

    class Job:
        job_id = "job-123"

        def result(self):
            rec.calls.append("result")

    class Client:
        def query(self, sql, job_config=None, location=None, project=None):
            rec.calls.append((sql, job_config.use_legacy_sql, location, project))
            assert isinstance(job_config, bigquery.QueryJobConfig)
            return Job()

    class Res:
        def get_client(self):
            from contextlib import contextmanager

            @contextmanager
            def cm():
                yield Client()
            return cm()
    conf = {"query": {"query": "select 1", "useLegacySql": False}}
    rep, res, _ = run(tmp_path, "BigQueryInsertJobOperator", {"configuration": conf, "location": "EU", "project_id": "proj", "gcp_conn_id": "google_cloud_default"}, "p_bq", {"google_cloud_default": Res()},
                      tf={"configuration": conf})
    assert res.success and rec.calls == [("select 1", False, "EU", "proj"), "result"] and deg(rep, "job id")
    assert "BigQueryResource" in (tmp_path / "p_bq" / "defs" / "resources.py").read_text()


def test_spark_submit_builds_the_command_line_and_runs_it(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    out = tmp_path / "argv.txt"
    script = bindir / "spark-submit"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {out}\nenv | grep '^MYVAR=' >> {out}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SPARK_DEFAULT_MASTER", "spark://m:7077")
    rep, res, _ = run(tmp_path, "SparkSubmitOperator", {"application": "/jobs/etl.py", "conn_id": "spark_default", "conf": {"spark.x": "1"}, "executor_memory": "2g", "num_executors": 3, "name": "etl",
                                                        "application_args": ["--day", "2024-01-01"], "env_vars": {"MYVAR": "v"}, "jars": "a.jar,b.jar"}, "p_spark",
                      tf={"application": "/jobs/etl.py", "application_args": ["--day", "2024-01-01"]})
    assert res.success, [str(e.event_specific_data.error)[:300] for e in res.get_step_failure_events()]
    argv = out.read_text().split("\n")
    assert argv[:4] == ["--master", "spark://m:7077", "--conf", "spark.x=1"] and "--executor-memory" in argv and "2g" in argv and "--num-executors" in argv and "--jars" in argv
    assert argv[argv.index("/jobs/etl.py"):argv.index("/jobs/etl.py") + 3] == ["/jobs/etl.py", "--day", "2024-01-01"] and "MYVAR=v" in argv
    assert deg(rep, "stand-in") and deg(rep, "_MASTER")


def test_report_lists_the_new_operators_as_mapped(tmp_path):
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", "SlackAPIPostOperator", args={"channel": "c", "text": "x"}, template_fields={}, callable_args={})], kind="once")]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    import subprocess
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pk" / "defs"), "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    ops = json.loads((tmp_path / "r.json").read_text())["operators"]
    assert ops["SlackAPIPostOperator"]["mapped_by_converter"] is True


def test_bucket_create_and_delete_for_s3_and_gcs(tmp_path):
    rec = Rec()

    class S3:
        def head_bucket(self, Bucket):
            raise RuntimeError("404")

        def create_bucket(self, **kw):
            rec.calls.append(("s3create", kw))

        def get_paginator(self, n):
            return types.SimpleNamespace(paginate=lambda **kw: [{"Contents": [{"Key": "k1"}]}])

        def delete_objects(self, **kw):
            rec.calls.append(("s3rm", kw))

        def delete_bucket(self, **kw):
            rec.calls.append(("s3delbucket", kw))

    class GCSClient:
        def create_bucket(self, b, location=None, project=None):
            rec.calls.append(("gcscreate", b, location, project))

        def bucket(self, b):
            return types.SimpleNamespace(delete=lambda force=False: rec.calls.append(("gcsdel", b, force)))

    class GRes:
        def get_client(self):
            return GCSClient()
    for ty, args, res in (("S3CreateBucketOperator", {"bucket_name": "nb", "region_name": "eu-west-1", "aws_conn_id": "aws_default"}, {"aws_default": S3()}),
                          ("S3DeleteBucketOperator", {"bucket_name": "nb", "force_delete": True, "aws_conn_id": "aws_default"}, {"aws_default": S3()}),
                          ("GCSCreateBucketOperator", {"bucket_name": "gb", "location": "EU", "gcp_conn_id": "google_cloud_default"}, {"google_cloud_default": GRes()}),
                          ("GCSDeleteBucketOperator", {"bucket_name": "gb", "gcp_conn_id": "google_cloud_default"}, {"google_cloud_default": GRes()})):
        rep, r, _ = run(tmp_path, ty, args, "p_" + ty[:8] + ty[-10:-8], res, tf={k: v for k, v in args.items()})
        assert r.success, (ty, [str(e.event_specific_data.error)[:200] for e in r.get_step_failure_events()])
    assert ("s3create", {"Bucket": "nb", "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"}}) in rec.calls
    assert ("s3rm", {"Bucket": "nb", "Delete": {"Objects": [{"Key": "k1"}]}}) in rec.calls and ("s3delbucket", {"Bucket": "nb"}) in rec.calls
    assert ("gcscreate", "gb", "EU", None) in rec.calls and ("gcsdel", "gb", True) in rec.calls


# the Lambda, SSH, Docker and BigQuery component forms were replaced by resource-first steps: see test_registry_resources.py
