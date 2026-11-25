"""More provider operators for convert_ir.py, each on an official Dagster integration or a plain SDK (registered into `provider_ops` when that module loads).

Slack API post, S3 key sensor and list-prefixes, GCS (upload, download, list, delete, existence sensor), SSH command, Docker container, Lambda invoke, BigQuery insert job
(query jobs), Spark submit. Each handler follows `provider_ops.Handler`: `check` returns the reasons the task must be a loud stub (an argument the mapping cannot carry) and the
differences to state (the UNVERIFIED caveat when it was only run against a recording fake, a local stand-in or a request validator, never the real service), `build` writes the
step body. The runtime helpers the generated code calls are in MORE_RUNTIME, appended to the generated `runtime.py`.

Checked how: see the tests in tests/test_provider_ops_more.py and reference/conversion.md ("How each mapping was checked").
"""
from __future__ import annotations

from typing import Any, Callable

import provider_ops as P

UNV = P.UNVERIFIED
_a, _empty = P._a, P._empty


def _refuse(a: dict[str, Any], names: tuple[str, ...], what: str, defaults: dict[str, Any] | None = None) -> list[str]:
    """Reasons for each argument in `names` that is set to something other than its default (the IR holds every constructor argument, defaults included, so a default is not a refusal)."""
    d = defaults or {}
    return [f"{what} {n} is set (not carried over)" for n in names if not _empty(a.get(n)) and a.get(n) != d.get(n)]


SSH_DEFAULTS = {"banner_timeout": 30.0, "conn_timeout": None}
DOCKER_DEFAULTS = {"mount_tmp_dir": True, "tmp_dir": "/tmp/airflow", "cpus": 1.0, "retrieve_output": False, "xcom_all": False, "privileged": False, "auto_remove": "never", "force_pull": False}


# ---- Slack API (chat.postMessage through dagster-slack's SlackResource) ----------------------------------------------------------------
def _slack_api_check(t):
    a = _a(t)
    return _refuse(a, ("api_params", "method", "proxy", "base_url", "timeout", "retry_handlers"), "SlackAPIPostOperator"), [UNV.format(what="Slack workspace (a recording fake of the client was used)")]


def _slack_api_build(t, ex):
    a = _a(t)
    conn = a.get("slack_conn_id") or "slack_api_default"
    call = (f"slack_api_post(getattr(context.resources, {conn!r}), {ex(a.get('channel') or '#general')}, {ex(a.get('text') or 'No message has been set.')}, {ex(a.get('username') or 'Airflow')}, "
            f"{ex(a.get('icon_url'))}, {ex(a.get('attachments'))}, {ex(a.get('blocks'))})")
    return P.Prov(call, "slack_api", conn, resource=True, kinds_tag="slack")


# ---- S3 key sensor and list prefixes (boto3 through the aws resource) ----------------------------------------------------------------
def _s3key_check(t):
    a = _a(t)
    stubs = _refuse(a, ("check_fn", "metadata_keys", "use_regex"), "S3KeySensor")
    if a.get("deferrable"):
        pass
    return stubs, [UNV.format(what="S3 bucket (a recording fake client was used)")] + (["deferrable=True is ignored: the step polls"] if a.get("deferrable") else [])


def _s3key_build(t, ex):
    a = _a(t)
    conn = a.get("aws_conn_id") or "aws_default"
    return P.Prov(f"s3_key_exists(getattr(context.resources, {conn!r}), {ex(a.get('bucket_key'))}, {ex(a.get('bucket_name'))}, {bool(a.get('wildcard_match'))!r})", "aws_s3", conn, resource=True, kinds_tag="s3")


# ---- GCS (google-cloud-storage client through dagster-gcp's GCSResource) -------------------------------------------------------------
GCS_OPS = {"GCSCreateBucketOperator", "GCSDeleteBucketOperator", "LocalFilesystemToGCSOperator", "GCSToLocalFilesystemOperator", "GCSListObjectsOperator", "GCSDeleteObjectsOperator", "GCSObjectExistenceSensor"}


def _gcs_check(t):
    a = _a(t)
    ty = t.get("orig_type") or t["type"]
    stubs = _refuse(a, ("impersonation_chain", "user_project", "chunk_size", "encoding", "cache_control", "customer_encryption_key", "retry", "metadata", "deferrable"), ty)
    if ty == "LocalFilesystemToGCSOperator":
        stubs += _refuse(a, ("gzip", "mime_type"), ty) if a.get("gzip") else []
    if ty == "GCSCreateBucketOperator":
        stubs += _refuse(a, ("resource", "labels", "storage_class", "predefined_acl", "predefined_default_object_acl", "retention_period", "uniform_bucket_level_access"), ty)
    if ty == "GCSDeleteObjectsOperator" and bool(a.get("objects")) == bool(a.get("prefix")):
        stubs.append("GCSDeleteObjectsOperator needs exactly one of objects or prefix")
    if ty == "GCSToLocalFilesystemOperator" and (a.get("store_to_xcom_key") or not a.get("filename")):
        stubs.append("GCSToLocalFilesystemOperator without a filename (it stores to XCom)")
    return stubs, [UNV.format(what="GCS bucket (a recording fake client was used)")]


def _gcs_build(t, ex):
    a = _a(t)
    ty = t.get("orig_type") or t["type"]
    conn = a.get("gcp_conn_id") or "google_cloud_default"
    r = f"getattr(context.resources, {conn!r})"
    if ty == "LocalFilesystemToGCSOperator":
        call = f"return gcs_upload({r}, {ex(a.get('src'))}, {ex(a.get('dst'))}, {ex(a.get('bucket'))}, {ex(a.get('mime_type'))})"
    elif ty == "GCSToLocalFilesystemOperator":
        call = f"return gcs_download({r}, {ex(a.get('bucket'))}, {ex(a.get('object_name'))}, {ex(a.get('filename'))})"
    elif ty == "GCSListObjectsOperator":
        call = f"return gcs_list({r}, {ex(a.get('bucket'))}, {ex(a.get('prefix'))}, {ex(a.get('delimiter'))}, {ex(a.get('match_glob'))})"
    elif ty == "GCSCreateBucketOperator":
        call = f"return gcs_create_bucket({r}, {ex(a.get('bucket_name'))}, {ex(a.get('location') or 'US')}, {ex(a.get('project_id'))})"
    elif ty == "GCSDeleteBucketOperator":
        call = f"return gcs_delete_bucket({r}, {ex(a.get('bucket_name'))}, {bool(a.get('force', True))!r})"
    elif ty == "GCSDeleteObjectsOperator":
        call = f"return gcs_delete({r}, {ex(a.get('bucket_name'))}, {ex(a.get('objects'))}, {ex(a.get('prefix'))})"
    else:
        call = f"gcs_exists({r}, {ex(a.get('bucket'))}, {ex(a.get('object'))})"
    return P.Prov(call, "gcs", conn, resource=True, kinds_tag="gcs")


# ---- SSH (paramiko through dagster-ssh's SSHResource) --------------------------------------------------------------------------------
def _ssh_check(t):
    a = _a(t)
    stubs = _refuse(a, ("remote_host", "conn_timeout", "banner_timeout", "environment"), "SSHOperator", SSH_DEFAULTS)         # ssh_hook is built by the operator from the connection id: not a refusal
    if callable(a.get("command")) or not isinstance(a.get("command"), (str, dict)):
        stubs.append("SSHOperator command is not a string")
    return stubs, [UNV.format(what="SSH server (a recording fake connection was used)"), "the value is the command's stdout as text (Airflow pushes it base64-encoded)"] + (
        ["get_pty is not applied"] if a.get("get_pty") else [])


def _ssh_build(t, ex):
    a = _a(t)
    conn = a.get("ssh_conn_id") or "ssh_default"
    return P.Prov(f"return ssh_run(getattr(context.resources, {conn!r}), {ex(a.get('command'))}, {a.get('cmd_timeout')!r})", "ssh", conn, resource=True, kinds_tag="ssh")


# ---- Docker (the docker SDK) -----------------------------------------------------------------------------------------------------------
def _docker_check(t):
    a = _a(t)
    stubs = _refuse(a, ("mounts", "tls_ca_cert", "tls_client_cert", "tls_client_key", "tls_hostname", "tls_ssl_version", "device_requests", "cap_add", "extra_hosts", "privileged",
                        "mount_tmp_dir", "tmp_dir", "host_tmp_dir", "retrieve_output", "retrieve_output_path", "xcom_all", "ports", "dns", "dns_search", "docker_conn_id",
                        "mem_limit", "cpus", "shm_size", "container_name", "hostname", "domainname", "ipc_mode", "log_opts"), "DockerOperator", DOCKER_DEFAULTS)
    if a.get("entrypoint") is not None and not isinstance(a.get("entrypoint"), (str, list)):
        stubs.append("DockerOperator entrypoint is not a string or list")
    return stubs, [UNV.format(what="Docker daemon (a recording fake of the docker SDK was used)")] + (["auto_remove is applied as remove-after-run for 'success' and 'force'"] if a.get("auto_remove") in ("success", "force", True) else [])


def _docker_build(t, ex):
    a = _a(t)
    ar = a.get("auto_remove") in ("success", "force", True)
    return P.Prov(f"return docker_run({ex(a.get('image'))}, {ex(a.get('command'))}, {ex(a.get('environment'))}, {a.get('docker_url') or 'unix://var/run/docker.sock'!r}, {a.get('network_mode')!r}, "
                  f"{ex(a.get('user'))}, {ex(a.get('working_dir'))}, {bool(a.get('tty'))!r}, {ex(a.get('entrypoint'))}, {ar!r}, {bool(a.get('force_pull'))!r})", "docker", None, kinds_tag="docker")


# ---- AWS Lambda (boto3) ---------------------------------------------------------------------------------------------------------------
def _lambda_check(t):
    a = _a(t)
    stubs = _refuse(a, ("botocore_config", "verify", "deferrable", "client_context"), "LambdaInvokeFunctionOperator")
    if not isinstance(a.get("function_name"), str):
        stubs.append("LambdaInvokeFunctionOperator function_name is not a string")
    return stubs, [UNV.format(what="Lambda function (botocore's stubber validated the request shape against the service model)"), "credentials and region come from boto3's default chain (the connection is not read)"]


def _lambda_build(t, ex):
    a = _a(t)
    return P.Prov(f"return lambda_invoke({ex(a.get('function_name'))}, {ex(a.get('payload'))}, {a.get('invocation_type') or 'RequestResponse'!r}, {ex(a.get('qualifier'))}, "
                  f"{a.get('log_type')!r}, {a.get('region_name')!r})", "lambda", None, kinds_tag="aws")


# ---- BigQuery insert job: query jobs only (google-cloud-bigquery through dagster-gcp's BigQueryResource) -------------------------------
def _bq_check(t):
    a = _a(t)
    conf = a.get("configuration")
    stubs = _refuse(a, ("deferrable", "impersonation_chain", "result_retry"), "BigQueryInsertJobOperator")
    if not isinstance(conf, dict) or set(conf) - {"query", "labels", "dryRun"} or "query" not in conf:
        stubs.append("BigQueryInsertJobOperator configuration is not a plain query job (load, copy and extract jobs are not converted)")
    else:
        q = conf["query"]
        extra = set(q) - {"query", "useLegacySql", "priority"}
        if extra or not isinstance(q.get("query"), str):
            stubs.append(f"BigQueryInsertJobOperator query settings {sorted(extra) or ['query (not a string)']} are not carried over")
    return stubs, [UNV.format(what="BigQuery project (a recording fake client was used)"), "the value is the job id"]


def _bq_build(t, ex):
    a = _a(t)
    conn = a.get("gcp_conn_id") or "google_cloud_default"
    q = a["configuration"]["query"]
    return P.Prov(f"return bigquery_query(getattr(context.resources, {conn!r}), {ex(q['query'])}, {bool(q.get('useLegacySql', True))!r}, {q.get('priority')!r}, {a.get('location')!r}, {a.get('project_id')!r})",
                  "bigquery", conn, resource=True, kinds_tag="bigquery")


# ---- Spark submit (spark-submit on the PATH) ----------------------------------------------------------------------------------------
SPARK_SUPPORTED = {"application", "conf", "conn_id", "total_executor_cores", "executor_cores", "executor_memory", "driver_memory", "keytab", "principal", "proxy_user", "name", "num_executors",
                   "status_poll_interval", "application_args", "env_vars", "verbose", "java_class", "packages", "exclude_packages", "repositories", "jars", "py_files", "files", "archives", "driver_class_path",
                   "queue", "deploy_mode", "spark_binary", "properties_file", "yarn_queue", "use_krb5ccache", "openlineage_inject_parent_job_info", "openlineage_inject_transport_info"}


def _spark_check(t):
    a = _a(t)
    stubs = _refuse(a, ("keytab", "principal", "proxy_user", "use_krb5ccache", "properties_file", "env_vars_from"), "SparkSubmitOperator") + (["SparkSubmitOperator env_vars is not a plain dict"] if a.get("env_vars") and not P._plain_dict(a["env_vars"]) else [])
    if not isinstance(a.get("application"), str):
        stubs.append("SparkSubmitOperator application is not a string")
    return stubs, [UNV.format(what="Spark cluster (a stand-in spark-submit that records its arguments was run)"), "the master and deploy mode come from the environment (<CONN>_MASTER, <CONN>_DEPLOY_MODE), not the connection",
                   "the driver status is not polled (spark-submit's own exit code decides)"]


def _spark_build(t, ex):
    a = _a(t)
    conn = a.get("conn_id") or "spark_default"
    opts = {k: a.get(k) for k in ("conf", "total_executor_cores", "executor_cores", "executor_memory", "driver_memory", "name", "num_executors", "java_class", "packages", "exclude_packages",
                                  "repositories", "jars", "py_files", "files", "archives", "driver_class_path", "queue", "deploy_mode", "verbose") if not _empty(a.get(k))}
    return P.Prov(f"spark_submit({P.envname(conn)!r}, {ex(a.get('application'))}, {ex(a.get('application_args'))}, {ex(opts)}, {ex(a.get('env_vars'))}, {a.get('spark_binary') or 'spark-submit'!r})", "spark", conn, kinds_tag="spark")


def _tf(*n):
    return lambda t: {k: _a(t).get(k) for k in n}


MORE = {
    "slack_api": P.Handler({"SlackAPIPostOperator"}, _slack_api_check, _slack_api_build, _tf("channel", "text", "username", "attachments", "blocks")),
    "s3key": P.Handler({"S3KeySensor"}, _s3key_check, _s3key_build, _tf("bucket_key", "bucket_name")),
    "gcs": P.Handler(GCS_OPS, _gcs_check, _gcs_build, _tf("src", "dst", "bucket", "object_name", "filename", "prefix", "delimiter", "objects", "bucket_name", "object")),
    "ssh": P.Handler({"SSHOperator"}, _ssh_check, _ssh_build, _tf("command")),
    "docker": P.Handler({"DockerOperator"}, _docker_check, _docker_build, _tf("image", "command", "environment", "user", "working_dir", "entrypoint")),
    "lambda": P.Handler({"LambdaInvokeFunctionOperator"}, _lambda_check, _lambda_build, _tf("function_name", "payload", "qualifier")),
    "bigquery": P.Handler({"BigQueryInsertJobOperator"}, _bq_check, _bq_build, lambda t: {"sql": ((_a(t).get("configuration") or {}).get("query") or {}).get("query") if isinstance(_a(t).get("configuration"), dict) else None}),
    "spark": P.Handler({"SparkSubmitOperator"}, _spark_check, _spark_build, _tf("application", "application_args", "conf", "env_vars", "jars", "packages", "files", "py_files")),
}

RESOURCES = {
    "slack_api": {"import": "from dagster_slack import SlackResource", "ctor": lambda c: f"SlackResource(token=dg.EnvVar('{P.envname(c)}_TOKEN'))", "env": lambda c: [f"{P.envname(c)}_TOKEN"]},
    "aws_s3": {"import": "from dagster_aws.s3 import S3Resource", "ctor": lambda c: "S3Resource()", "env": lambda c: ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"]},
    "gcs": {"import": "from dagster_gcp import GCSResource", "ctor": lambda c: f"GCSResource(project=dg.EnvVar('{P.envname(c)}_PROJECT'))", "env": lambda c: [f"{P.envname(c)}_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"]},
    "bigquery": {"import": "from dagster_gcp import BigQueryResource", "ctor": lambda c: f"BigQueryResource(project=dg.EnvVar('{P.envname(c)}_PROJECT'))", "env": lambda c: [f"{P.envname(c)}_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"]},
    "ssh": {"import": "from dagster_ssh import SSHResource",
            "ctor": lambda c: (f"SSHResource(remote_host=dg.EnvVar('{P.envname(c)}_HOST'), remote_port=dg.EnvVar.int('{P.envname(c)}_PORT'), username=dg.EnvVar('{P.envname(c)}_USER'), "
                               f"password=dg.EnvVar('{P.envname(c)}_PASSWORD'))"), "env": lambda c: [f"{P.envname(c)}_{s}" for s in ("HOST", "PORT", "USER", "PASSWORD")]},
}
REQ = {"slack_api": ["dagster-slack"], "aws_s3": ["dagster-aws"], "gcs": ["dagster-gcp"], "bigquery": ["dagster-gcp"], "ssh": ["dagster-ssh"], "docker": ["docker"], "lambda": ["boto3"], "spark": []}
ENV = {"spark": lambda c: [f"{P.envname(c)}_MASTER (default yarn)", f"{P.envname(c)}_DEPLOY_MODE (optional)"], "docker": lambda c: ["DOCKER_HOST (optional)"], "lambda": lambda c: ["AWS_* (boto3 default chain)"]}
SENSOR_TYPES = {"S3KeySensor", "GCSObjectExistenceSensor"}
S3_EXTRA = {"S3ListPrefixesOperator"}

MORE_RUNTIME = '''

# ---- more provider operators (provider_ops_more.py) -----------------------------------------------------------------------------
def slack_api_post(resource, channel, text, username, icon_url, attachments, blocks):
    """chat.postMessage through the SlackResource's client (SlackAPIPostOperator calls the same Web API method)."""
    kw = {k: v for k, v in {"channel": channel, "text": text, "username": username, "icon_url": icon_url, "attachments": attachments, "blocks": blocks}.items() if v is not None}
    resp = resource.get_client().chat_postMessage(**kw)
    if not resp["ok"]:
        raise dg.Failure(description=f"Slack API error: {resp.get('error')}")


def s3_key_exists(client, bucket_key, bucket_name, wildcard):
    """S3KeySensor: True when the key (or any key matching the wildcard) exists; a list of keys must all exist."""
    import fnmatch
    keys = [bucket_key] if isinstance(bucket_key, str) else list(bucket_key)
    for k in keys:
        b, key = s3_bucket_key(bucket_name, k)
        if wildcard:
            prefix = key.split("*")[0].split("?")[0].split("[")[0]
            found = False
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
                if any(fnmatch.fnmatch(o["Key"], key) for o in page.get("Contents", [])):
                    found = True
                    break
            if not found:
                return False
        elif not _s3_head(client, b, key):
            return False
    return True


def s3_list_prefixes(client, bucket, prefix=None, delimiter=None):
    """S3ListPrefixesOperator: the common prefixes under `prefix` (S3Hook.list_prefixes)."""
    out = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix or "", Delimiter=delimiter or ""):
        out += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
    return out


def gcs_upload(resource, src, dst, bucket, mime_type=None):
    """LocalFilesystemToGCSOperator: a file (or every file of a directory) to gs://bucket/dst."""
    import os
    client = resource.get_client()
    b = client.bucket(bucket)
    if os.path.isdir(src):
        for root, _, files in os.walk(src):
            for f in files:
                p = os.path.join(root, f)
                b.blob((dst or "") + os.path.relpath(p, src)).upload_from_filename(p, content_type=mime_type)
    else:
        b.blob(dst or os.path.basename(src)).upload_from_filename(src, content_type=mime_type)


def gcs_download(resource, bucket, object_name, filename):
    resource.get_client().bucket(bucket).blob(object_name).download_to_filename(filename)
    return filename


def gcs_list(resource, bucket, prefix=None, delimiter=None, match_glob=None):
    kw = {k: v for k, v in {"prefix": prefix, "delimiter": delimiter, "match_glob": match_glob}.items() if v}
    return [b.name for b in resource.get_client().list_blobs(bucket, **kw)]


def gcs_delete(resource, bucket, objects=None, prefix=None):
    client = resource.get_client()
    names = [objects] if isinstance(objects, str) else list(objects) if objects else [b.name for b in client.list_blobs(bucket, prefix=prefix)]
    for n in names:
        client.bucket(bucket).blob(n).delete()


def gcs_create_bucket(resource, bucket, location="US", project=None):
    """GCSCreateBucketOperator (name and location only): create the bucket; an existing bucket of that name is left as it is (the hook's `create_bucket` logs and continues on a conflict)."""
    client = resource.get_client()
    try:
        client.create_bucket(bucket, location=location, project=project)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ != "Conflict":
            raise


def gcs_delete_bucket(resource, bucket, force=True):
    """GCSDeleteBucketOperator: delete the bucket (with its objects when `force`)."""
    resource.get_client().bucket(bucket).delete(force=force)


def s3_create_bucket(client, bucket, region=None):
    """S3CreateBucketOperator: create the bucket unless it exists (S3Hook.create_bucket checks first); outside us-east-1 the location constraint is the region."""
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception:  # noqa: BLE001
        pass
    kw = {"CreateBucketConfiguration": {"LocationConstraint": region}} if region and region != "us-east-1" else {}
    client.create_bucket(Bucket=bucket, **kw)


def s3_delete_bucket(client, bucket, force=False):
    """S3DeleteBucketOperator: delete the bucket; with `force` empty it first (every key, then every version)."""
    if force:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
    client.delete_bucket(Bucket=bucket)


def gcs_exists(resource, bucket, obj):
    return resource.get_client().bucket(bucket).blob(obj).exists()


def ssh_run(resource, command, timeout=None):
    """SSHOperator: run the command, fail on a non-zero exit status, the value is stdout."""
    with resource.get_connection() as client:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out, err = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
        status = stdout.channel.recv_exit_status()
    if status != 0:
        raise dg.Failure(description=f"SSH command exited with status {status}", metadata={"stderr": err[-4000:]})
    return out


def docker_run(image, command, environment, docker_url, network_mode, user, working_dir, tty, entrypoint, remove, force_pull):
    """DockerOperator: run the image to completion, stream its logs, fail on a non-zero exit code; the value is the last log line (Airflow pushes it)."""
    import docker
    client = docker.DockerClient(base_url=docker_url)
    if force_pull:
        client.images.pull(image)
    c = client.containers.run(image, command, environment=environment or None, network_mode=network_mode, user=user, working_dir=working_dir, tty=tty, entrypoint=entrypoint, detach=True)
    try:
        res = c.wait()
        logs = c.logs().decode(errors="replace") if hasattr(c.logs(), "decode") else str(c.logs())
    finally:
        if remove:
            c.remove(force=True)
    code = res.get("StatusCode", 0) if isinstance(res, dict) else int(res)
    if code != 0:
        raise dg.Failure(description=f"docker container exited with code {code}", metadata={"logs": logs[-4000:]})
    lines = logs.strip().splitlines()
    return lines[-1] if lines else None


def lambda_invoke(function_name, payload, invocation_type="RequestResponse", qualifier=None, log_type=None, region=None):
    """LambdaInvokeFunctionOperator: boto3 `lambda.invoke`; a function error raises; the value is the response payload (JSON when it parses)."""
    import json as _json
    import boto3
    kw = {"FunctionName": function_name, "InvocationType": invocation_type}
    if payload is not None:
        kw["Payload"] = payload if isinstance(payload, (str, bytes)) else _json.dumps(payload)
    if qualifier:
        kw["Qualifier"] = qualifier
    if log_type:
        kw["LogType"] = log_type
    resp = boto3.client("lambda", region_name=region).invoke(**kw)
    body = resp["Payload"].read() if hasattr(resp.get("Payload"), "read") else resp.get("Payload")
    text = body.decode() if isinstance(body, bytes) else body
    if resp.get("FunctionError"):
        raise dg.Failure(description=f"Lambda function error: {resp['FunctionError']}", metadata={"payload": (text or "")[:4000]})
    try:
        return _json.loads(text) if text else None
    except ValueError:
        return text


def bigquery_query(resource, sql, use_legacy_sql=True, priority=None, location=None, project=None):
    """BigQueryInsertJobOperator with a query configuration: run the query and wait; the value is the job id."""
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(use_legacy_sql=use_legacy_sql, **({"priority": priority} if priority else {}))
    with resource.get_client() as client:
        job = client.query(sql, job_config=cfg, location=location, project=project)
        job.result()
        return job.job_id


def spark_submit(conn_env, application, args, opts, env_vars, binary="spark-submit"):
    """SparkSubmitOperator: build the spark-submit command line as the Spark hook does and run it; the exit code decides."""
    import os
    import subprocess
    cmd = [binary, "--master", os.environ.get(conn_env + "_MASTER", "yarn")]
    dm = opts.get("deploy_mode") or os.environ.get(conn_env + "_DEPLOY_MODE")
    for k, v in (opts.get("conf") or {}).items():
        cmd += ["--conf", f"{k}={v}"]
    flags = {"jars": "--jars", "packages": "--packages", "exclude_packages": "--exclude-packages", "repositories": "--repositories", "py_files": "--py-files", "files": "--files", "archives": "--archives",
             "driver_class_path": "--driver-class-path", "total_executor_cores": "--total-executor-cores", "executor_cores": "--executor-cores", "executor_memory": "--executor-memory",
             "driver_memory": "--driver-memory", "num_executors": "--num-executors", "queue": "--queue", "name": "--name", "java_class": "--class"}
    for k, flag in flags.items():
        if opts.get(k) not in (None, "", [], {}):
            cmd += [flag, str(opts[k])]
    if dm:
        cmd += ["--deploy-mode", dm]
    if opts.get("verbose"):
        cmd.append("--verbose")
    cmd.append(application)
    cmd += [str(x) for x in (args or [])]
    proc = subprocess.run(cmd, env={**os.environ, **{str(k): str(v) for k, v in (env_vars or {}).items()}}, capture_output=True, text=True)
    if proc.returncode != 0:
        raise dg.Failure(description=f"spark-submit exited {proc.returncode}", metadata={"stderr": proc.stderr[-4000:], "command": " ".join(cmd)})
    return proc.stdout[-2000:]
'''


def register() -> None:
    P.HANDLERS.update(MORE)
    P.TYPES |= set().union(*(h.types for h in MORE.values()))
    P.SENSOR_TYPES |= SENSOR_TYPES
    P.RESOURCE_KINDS.update(RESOURCES)
    P.REQUIREMENTS.update(REQ)
    P.ENV_KINDS.update(ENV)
    P.PROVIDER_RUNTIME += MORE_RUNTIME
