"""Which registry components `convert_ir.py --output components` uses for Airflow operators, as DATA.

The rule (from the user's direction): an operator or hook that CALLS a service (HTTP, SQL, SSH, cloud APIs, Spark, Databricks, bucket lifecycle) is a
Dagster RESOURCE used inside an asset: the resource is configured in YAML (a `*_resource` registry component) or in `resources.py` (an integration
library resource), and the call is plain Python inside the asset. An asset COMPONENT is kept only where the operator's job is to run something that
produces a data product or is a long-running job (a shell command, a container, a Python callable, a wait).

Two tables:

    OPERATOR_MAP     one row per operator family, for the docs and the report: shape, verdict, component or resource, version verified.
    RESOURCE_PLANS   operators that use a registry RESOURCE component: a function that checks the arguments, names the resource and its YAML
                     attributes, and writes the plain-Python call. Adding a resource that a newer registry release brings is one entry here.

`VERIFIED_IN` is the registry release whose `schema.json` and `component.py` were read and whose resource was run in a scratch project (the tests
do the same). A resource not in the installed release must never be emitted; `ANNOUNCED_NOT_VERIFIED` names those the registry has announced and the
converter has not run yet (empty when everything announced has been run).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import provider_ops as P

VERIFIED_IN = "0.10.193"
DCC_REQUIREMENT = f"dagster-community-components>={VERIFIED_IN}"
ANNOUNCED_NOT_VERIFIED: list[str] = []

# operator family -> (shape, verdict, component or resource, note). shape: asset | resource | plain.
OPERATOR_MAP: list[dict[str, str]] = [
    {"operator": "BashOperator", "shape": "asset", "verdict": "keep asset component", "uses": "shell_command_asset", "why": "runs a command that produces files: a job, not a service call"},
    {"operator": "PythonOperator, @task", "shape": "asset", "verdict": "keep asset component", "uses": "python_callable_asset", "why": "the user's own callable is the asset"},
    {"operator": "DockerOperator", "shape": "asset", "verdict": "keep asset component", "uses": "docker_container_asset", "why": "a container run to completion is a long-running job"},
    {"operator": "BashSensor, TimeSensor", "shape": "asset", "verdict": "keep asset component", "uses": "wait_until_asset", "why": "holds a step open; no service call"},
    {"operator": "schedule", "shape": "asset", "verdict": "keep component", "uses": "CronScheduleComponent", "why": "schedules are not assets"},
    {"operator": "HttpOperator, SimpleHttpOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "http_resource", "why": "an HTTP call"},
    {"operator": "DatabricksRunNowOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "databricks_jobs_resource", "why": "runs an existing job by id and waits"},
    {"operator": "SparkSubmitOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "spark_submit_resource", "why": "spark-submit through a resource"},
    {"operator": "S3CreateBucketOperator, S3DeleteBucketOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "s3_bucket_resource", "why": "bucket lifecycle"},
    {"operator": "GCSCreateBucketOperator, GCSDeleteBucketOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "gcs_bucket_resource", "why": "bucket lifecycle"},
    {"operator": "S3 list, copy, delete, create object, upload", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "resources.py S3Resource (dagster-aws); registry aws_s3_resource is the same class", "why": "S3 API calls"},
    {"operator": "SQL operators on other databases", "shape": "resource", "verdict": "move to plain Python", "uses": "SQLAlchemy URL from <CONN>_URL", "why": "the registry database resources take host, database and user fields the IR does not carry"},
    {"operator": "SSHOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "resources.py SSHResource (dagster-ssh)", "why": "no registry ssh resource (sftp_resource is file transfer)"},
    {"operator": "BigQueryInsertJobOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "resources.py BigQueryResource (dagster-gcp)", "why": "bigquery_ddl_resource has no query job call"},
    {"operator": "LambdaInvokeFunctionOperator, EmailOperator, Slack operators, dbt Cloud, Airbyte, Fivetran, Kubernetes, other provider operators", "shape": "plain",
     "verdict": "plain Python (existing shims)", "uses": "provider_ops.py handlers", "why": "no registry resource fits"},
]


# registry component class -> registry id (the folder under resources/)
RESOURCE_TYPE_IDS = {"HTTPResourceComponent": "http_resource", "DatabricksJobsResourceComponent": "databricks_jobs_resource", "SparkSubmitResourceComponent": "spark_submit_resource",
                     "S3BucketResourceComponent": "s3_bucket_resource", "GcsBucketResourceComponent": "gcs_bucket_resource"}


@dataclass
class Plan:
    resource_type: str                                   # registry component class, e.g. HTTPResourceComponent
    resource_key: str
    attrs: dict[str, Any]                                # YAML attributes of the resource (no `resource_key`)
    body: str                                            # plain Python for the asset body; the resource is `getattr(context.resources, key)`
    requirements: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    conn_env: list[str] = field(default_factory=list)    # environment variables the resource or the call reads, for the report
    needs_os: bool = False


RESOURCE_PLANS: dict[str, Callable[[dict[str, Any], dict[str, Any], Callable[[Any], str]], Plan | None]] = {}


def _plan(*types: str):
    def deco(fn):
        for ty in types:
            RESOURCE_PLANS[ty] = fn
        return fn
    return deco


def _res(key: str) -> str:
    return f"getattr(context.resources, {key!r})"


@_plan("HttpOperator")
def _http(t, a, ex):
    stubs, _ = P.handler_for(t).check(t)
    opts = a.get("extra_options") or {}
    if stubs or set(opts) - {"timeout", "check_response"} or not isinstance(a.get("method") or "POST", str):
        return None
    conn = a.get("http_conn_id") or "http_default"
    e = P.envname(conn)
    call = (f"http_resource_call({_res(conn)}, {(a.get('method') or 'POST')!r}, {ex(a.get('endpoint'))}, {ex(a.get('data'))}, {ex(a.get('headers'))}, {opts!r})")
    return Plan("HTTPResourceComponent", conn, {"base_url": "{{ env." + e + "_BASE_URL }}"}, call, ["requests"], conn_env=[f"{e}_BASE_URL"],
                caveats=[f"http_resource is configured with `{e}_BASE_URL` only: add `auth_header_env_var`, or `auth_type` with its `*_env_var` fields, in defs/registry_resources/defs.yaml if the Airflow connection had credentials "
                         "(an unset variable named there fails the run, so none is written by default); a GET's data is appended to the endpoint as a query string because http_resource.request has no `params`"])


@_plan("DatabricksRunNowOperator")
def _dbx(t, a, ex):
    stubs, _ = P.handler_for(t).check(t)
    body = dict(a.get("json") or {})
    allowed = {"job_id", "notebook_params", "python_params", "job_parameters", "idempotency_token"}
    if stubs or set(body) - allowed or a.get("wait_for_termination") is False or not body.get("job_id"):
        return None
    try:
        body["job_id"] = int(body["job_id"])
    except (TypeError, ValueError):
        return None
    conn = a.get("databricks_conn_id") or "databricks_default"
    e = P.envname(conn)
    kw = ", ".join(f"{k}={ex(v)}" for k, v in body.items())
    return Plan("DatabricksJobsResourceComponent", f"{conn}__jobs", {"workspace_host": "{{ env." + e + "_HOST }}", "workspace_token_env_var": f"{e}_TOKEN"},
                f"{_res(conn + '__jobs')}.run_now_and_wait({kw})", ["databricks-sdk"], conn_env=[f"{e}_HOST", f"{e}_TOKEN"],
                caveats=["databricks_jobs_resource polls with the SDK's waiter (3600 s timeout); `polling_period_seconds` is not applied. SubmitRun and job_name lookups are not supported by it"])


@_plan("SparkSubmitOperator")
def _spark(t, a, ex):
    stubs, _ = P.handler_for(t).check(t)
    opts = {k: a.get(k) for k in ("conf", "total_executor_cores", "executor_memory", "driver_memory", "name", "num_executors", "java_class", "packages", "jars", "py_files",
                                  "queue", "deploy_mode") if not P._empty(a.get(k))}
    unsupported = [k for k in ("exclude_packages", "repositories", "files", "archives", "driver_class_path", "executor_cores", "verbose") if not P._empty(a.get(k))]
    if stubs or unsupported or not isinstance(a.get("application"), str):
        return None
    conn = a.get("conn_id") or "spark_default"
    e = P.envname(conn)
    split = lambda k: f"[x for x in {opts[k]!r}.split(',')]" if isinstance(opts.get(k), str) else repr(opts.get(k))
    kw = [f"application={a['application']!r}", f"master=os.environ.get({e + '_MASTER'!r}, 'yarn')"]
    if a.get("application_args"):
        kw.append(f"application_args=[str(x) for x in {ex(a.get('application_args'))}]")
    for k, name in (("deploy_mode", "deploy_mode"), ("java_class", "class_name"), ("name", "name"), ("driver_memory", "driver_memory"), ("executor_memory", "executor_memory"),
                    ("num_executors", "num_executors"), ("total_executor_cores", "total_executor_cores"), ("queue", "queue")):
        if k in opts:
            kw.append(f"{name}={opts[k]!r}")
    if "conf" in opts:
        kw.append(f"conf={{str(k): str(v) for k, v in {opts['conf']!r}.items()}}")
    for k in ("packages", "jars", "py_files"):
        if k in opts:
            kw.append(f"{k}={split(k)}")
    if a.get("env_vars"):
        kw.append(f"env_vars={ex(a.get('env_vars'))}")
    return Plan("SparkSubmitResourceComponent", conn, {"spark_submit_binary": a.get("spark_binary") or "spark-submit"}, f"{_res(conn)}.submit({', '.join(kw)}, log=context.log)", [],
                conn_env=[f"{e}_MASTER (default yarn)"], needs_os=True,
                caveats=["the master comes from `<CONN>_MASTER` and the deploy mode from the operator, not from the Airflow connection; spark_submit_resource does not poll a cluster driver (the exit code decides)"])


@_plan("S3CreateBucketOperator", "S3DeleteBucketOperator")
def _s3_bucket(t, a, ex):
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__bucket"
    if t["type"] == "S3CreateBucketOperator":
        call = f"{_res(key)}.ensure_bucket({ex(a.get('bucket_name'))}, {ex(a.get('region_name'))})"
    else:
        call = f"{_res(key)}.delete_bucket({ex(a.get('bucket_name'))}, {bool(a.get('force_delete'))!r})"
    return Plan("S3BucketResourceComponent", key, {}, call, ["boto3"],
                caveats=["s3_bucket_resource uses boto3's default credential chain (no key fields are written: an unset variable named there fails the run); delete_bucket on an absent bucket succeeds"])


@_plan("GCSCreateBucketOperator", "GCSDeleteBucketOperator")
def _gcs_bucket(t, a, ex):
    stubs, _ = P.handler_for(t).check(t)
    if stubs or (t["type"] == "GCSCreateBucketOperator" and a.get("project_id")):
        return None
    conn = a.get("gcp_conn_id") or "google_cloud_default"
    key = f"{conn}__bucket"
    if t["type"] == "GCSCreateBucketOperator":
        call = f"{_res(key)}.ensure_bucket({ex(a.get('bucket_name'))}, {ex(a.get('location') or 'US')})"
    else:
        call = f"{_res(key)}.delete_bucket({ex(a.get('bucket_name'))}, {bool(a.get('force', True))!r})"
    return Plan("GcsBucketResourceComponent", key, {}, call, ["google-cloud-storage"],
                caveats=["gcs_bucket_resource uses Google's default credentials (GOOGLE_APPLICATION_CREDENTIALS); a project_id on the operator is not carried (such a task stays Python)"])


def plan_for(t: dict[str, Any], ex: Callable[[Any], str]) -> Plan | None:
    """The resource plan of a task, or None when the operator has none or its arguments need something the resource cannot do."""
    fn = RESOURCE_PLANS.get(t.get("orig_type") or t["type"]) or RESOURCE_PLANS.get(t["type"])
    if fn is None:
        return None
    return fn(t, P._a(t), ex)
