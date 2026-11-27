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
do the same: 0.10.193 for http, databricks run-now, spark, S3 and GCS bucket; 0.10.194 for everything else and for all of them again). A resource not in the installed release must never be emitted; `ANNOUNCED_NOT_VERIFIED` names those the registry has announced and the
converter has not run yet (empty when everything announced has been run).
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import provider_ops as P

VERIFIED_IN = "0.10.194"
DCC_REQUIREMENT = f"dagster-community-components>={VERIFIED_IN}"
ANNOUNCED_NOT_VERIFIED: list[str] = []

# operator family -> (shape, verdict, component or resource, note). shape: asset | resource | plain.
# `run` says how the resource was checked: RUN = the generated project was loaded by dagster's component loader against the registry release and the asset executed
# against a local stand-in or a recording fake of the client library; READ = schema.json and component.py were read, not run.
OPERATOR_MAP: list[dict[str, str]] = [
    {"operator": "BashOperator", "shape": "asset", "verdict": "keep asset component", "uses": "shell_command_asset", "why": "runs a command that produces files: a job, not a service call"},
    {"operator": "PythonOperator, @task", "shape": "asset", "verdict": "keep asset component", "uses": "python_callable_asset", "why": "the user's own callable is the asset"},
    {"operator": "DockerOperator", "shape": "asset", "verdict": "keep asset component", "uses": "docker_container_asset", "why": "a container run to completion is a long-running job"},
    {"operator": "BashSensor, TimeSensor", "shape": "asset", "verdict": "keep asset component", "uses": "wait_until_asset", "why": "holds a step open; no service call"},
    {"operator": "schedule", "shape": "asset", "verdict": "keep component", "uses": "CronScheduleComponent", "why": "schedules are not assets"},
    {"operator": "HttpOperator, SimpleHttpOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "http_resource", "why": "an HTTP call (`request`, GET data as `params`)", "run": "RUN 0.10.194"},
    {"operator": "HttpSensor", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "http_resource", "why": "`poke()` inside Airflow's poke loop", "run": "RUN 0.10.194"},
    {"operator": "EmailOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "smtp_resource", "why": "`send_email` (login optional)", "run": "RUN 0.10.194"},
    {"operator": "SlackWebhookOperator, SlackAPIPostOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "slack_resource (SlackPostResource)", "why": "`post_message` by webhook URL or bot token", "run": "RUN 0.10.194 (webhook: local server; token: recording fake WebClient)"},
    {"operator": "SSHOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "ssh_resource", "why": "`run_command`; a non-zero exit fails the step as the operator does", "run": "RUN 0.10.194 (recording fake paramiko client)"},
    {"operator": "LambdaInvokeFunctionOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "lambda_resource", "why": "`invoke`", "run": "RUN 0.10.194 (botocore stubber)"},
    {"operator": "SQLExecuteQueryOperator and the database SQL operators (not Snowflake)", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "sql_url_resource", "why": "`execute` from a SQLAlchemy URL in `<CONN>_URL`; one statement per call (split in the converter)", "run": "RUN 0.10.194 (SQLite)"},
    {"operator": "DatabricksRunNowOperator, DatabricksSubmitRunOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "databricks_jobs_resource", "why": "`run_now_and_wait` (job_id or job_name), `submit_run_and_wait`", "run": "RUN 0.10.194 (fake SDK client)"},
    {"operator": "SparkSubmitOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "spark_submit_resource", "why": "spark-submit through a resource (all flags; keytab, principal, proxy_user as extra_args)", "run": "RUN 0.10.194 (stand-in binary)"},
    {"operator": "EmrAddStepsOperator, EmrCreateJobFlowOperator, EmrTerminateJobFlowOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "emr_resource", "why": "`submit_step_and_wait` (one call per step), `create_cluster_and_wait`, `terminate_cluster`", "run": "RUN 0.10.194 (botocore stubber)"},
    {"operator": "DataprocCreateClusterOperator, DataprocDeleteClusterOperator, DataprocSubmitJobOperator, DataprocCreateBatchOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "dataproc_resource", "why": "`ensure_cluster`, `delete_cluster`, `submit_job_and_wait`, `create_batch_and_wait`", "run": "RUN 0.10.194 (recording fake clients)"},
    {"operator": "PubSubCreateTopicOperator, PubSubDeleteTopicOperator, PubSubCreateSubscriptionOperator, PubSubDeleteSubscriptionOperator, PubSubPublishMessageOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "pubsub_resource", "why": "`ensure_topic`, `delete_topic`, `ensure_subscription`, `delete_subscription`, `publish`", "run": "RUN 0.10.194 (recording fake clients)"},
    {"operator": "AthenaOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "athena_resource", "why": "`run_query_and_wait`", "run": "RUN 0.10.194 (botocore stubber)"},
    {"operator": "RedshiftDataOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "redshift_data_api_resource", "why": "`execute_and_wait`", "run": "RUN 0.10.194 (botocore stubber)"},
    {"operator": "BigQueryCreateEmptyDatasetOperator, BigQueryDeleteDatasetOperator, BigQueryCreateEmptyTableOperator, BigQueryDeleteTableOperator, BigQueryValueCheckOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "bigquery_ddl_resource", "why": "dataset and table DDL, scalar value check", "run": "RUN 0.10.194 (recording fake client)"},
    {"operator": "S3CreateBucketOperator, S3DeleteBucketOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "s3_bucket_resource", "why": "bucket lifecycle", "run": "RUN"},
    {"operator": "GCSCreateBucketOperator, GCSDeleteBucketOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "gcs_bucket_resource", "why": "bucket lifecycle (`project`, `storage_class` carried)", "run": "RUN 0.10.194 (recording fake client)"},
    {"operator": "S3 list, copy, delete, create object, upload", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "resources.py S3Resource (dagster-aws); registry aws_s3_resource is the same class", "why": "S3 API calls"},
    {"operator": "BigQueryInsertJobOperator", "shape": "resource", "verdict": "move to resource + plain Python", "uses": "resources.py BigQueryResource (dagster-gcp)", "why": "bigquery_ddl_resource has no query job call"},
    {"operator": "DbtCloudRunJobOperator", "shape": "plain", "verdict": "plain Python (existing shim), deliberately not on dbt_cloud_resource", "uses": "resources.py DbtCloudClientResource (dagster-dbt), disable_schedule_on_trigger=False",
     "why": "dbt_cloud_resource builds DbtCloudResource with disable_schedule_on_trigger=True and no field to change it: every trigger would switch the dbt Cloud job's cron schedule off, which the Airflow operator never does"},
    {"operator": "FivetranOperator", "shape": "plain", "verdict": "plain Python (existing shim), deliberately not on fivetran_sync_trigger_job", "uses": "resources.py FivetranResource (dagster-fivetran)",
     "why": "the component posts to /v1/connectors/{id}/force; Fivetran's current docs describe /v1/connections/{id}/sync and say force:true stops a running sync and re-runs it; the live API was not called, so the endpoint is unconfirmed"},
    {"operator": "AirbyteTriggerSyncOperator", "shape": "plain", "verdict": "plain Python (existing shim), not on airbyte_sync_trigger_job", "uses": "resources.py AirbyteResource (dagster-airbyte)",
     "why": "the component is self-hosted `/api/v1/` basic auth only (an Airbyte Cloud connection cannot be told from the IR) and does not wait unless wait_for_completion is set, where Airflow waits by default"},
    {"operator": "Cosmos per-task dbt operators, DbtRunOperator", "shape": "plain", "verdict": "existing Cosmos path (dagster-dbt DbtCliResource), not dbt_command_resource", "uses": "cosmos_ops.py",
     "why": "dbt_command_resource has no counterpart for Cosmos profile_mapping and the DbtCliResource path is already run; it would add nothing but lose the profile"},
    {"operator": "KubernetesPodOperator, other provider operators", "shape": "plain", "verdict": "plain Python (existing shims)", "uses": "provider_ops.py handlers", "why": "no registry resource fits"},
]


# registry component class -> registry id (the folder under resources/)
RESOURCE_TYPE_IDS = {"HTTPResourceComponent": "http_resource", "DatabricksJobsResourceComponent": "databricks_jobs_resource", "SparkSubmitResourceComponent": "spark_submit_resource",
                     "S3BucketResourceComponent": "s3_bucket_resource", "GcsBucketResourceComponent": "gcs_bucket_resource", "SMTPResourceComponent": "smtp_resource",
                     "SlackResourceComponent": "slack_resource", "SSHResourceComponent": "ssh_resource", "LambdaResourceComponent": "lambda_resource",
                     "SQLUrlResourceComponent": "sql_url_resource", "EmrResourceComponent": "emr_resource", "DataprocResourceComponent": "dataproc_resource",
                     "PubsubResourceComponent": "pubsub_resource", "AthenaResourceComponent": "athena_resource", "RedshiftDataApiResourceComponent": "redshift_data_api_resource",
                     "BigQueryDdlResourceComponent": "bigquery_ddl_resource"}
# operators with a resource plan but no plain-Python handler: with no plan (an argument the resource cannot carry) the DAG keeps the Python route, where the task is a stub
PLAN_ONLY_TYPES = {"EmrAddStepsOperator", "EmrCreateJobFlowOperator", "EmrTerminateJobFlowOperator", "AthenaOperator", "RedshiftDataOperator",
                   "DataprocCreateClusterOperator", "DataprocDeleteClusterOperator", "DataprocSubmitJobOperator", "DataprocCreateBatchOperator",
                   "PubSubCreateTopicOperator", "PubSubDeleteTopicOperator", "PubSubCreateSubscriptionOperator", "PubSubDeleteSubscriptionOperator", "PubSubPublishMessageOperator",
                   "BigQueryCreateEmptyDatasetOperator", "BigQueryDeleteDatasetOperator", "BigQueryCreateEmptyTableOperator", "BigQueryDeleteTableOperator", "BigQueryValueCheckOperator"}
SENSOR_PLAN_TYPES = {"HttpSensor"}      # a sensor with a plan runs inside af_poke; one without keeps the Python route


class NoPlan(Exception):
    """The operator has a resource plan, but not for these arguments: the reason is carried to the report, and the plain-Python shim (when there is one) runs the call."""


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


RESOURCE_PLANS: dict[str, Callable[[dict[str, Any], dict[str, Any], Callable[[Any], str]], Plan]] = {}


def _plan(*types: str):
    def deco(fn):
        for ty in types:
            RESOURCE_PLANS[ty] = fn
        return fn
    return deco


def _res(key: str) -> str:
    return f"getattr(context.resources, {key!r})"


def _empty(v: Any) -> bool:
    return P._empty(v)


def _refuse(a: dict[str, Any], names: tuple[str, ...], defaults: dict[str, Any] | None = None) -> None:
    """NoPlan for the first argument in `names` that is set to something other than its default (the IR holds constructor defaults as well)."""
    d = defaults or {}
    bad = [n for n in names if not _empty(a.get(n)) and a.get(n) != d.get(n)]
    if bad:
        raise NoPlan(f"{', '.join(bad)} {'is' if len(bad) == 1 else 'are'} set and the resource has no counterpart")


def _handler_ok(t: dict[str, Any], allow: tuple[str, ...] = ()) -> None:
    """The plain-Python shim's own refusals are the plan's refusals too (same arguments), except the ones in `allow`, which the resource does carry."""
    h = P.handler_for(t)
    if h is not None:
        stubs = [x for x in h.check(t)[0] if not any(k in x for k in allow)]
        if stubs:
            raise NoPlan("; ".join(stubs))


def _plain_json(v: Any) -> bool:
    return isinstance(v, (dict, list, str, int, float, bool, type(None))) and not (isinstance(v, dict) and ({"repr", "xcom_from"} & set(v)))


def _region(a: dict[str, Any], conn_env: list[str]) -> str:
    """AWS region for a resource whose `region_name` is required: the operator's own, else boto's standard variable."""
    if isinstance(a.get("region_name"), str) and a["region_name"]:
        return a["region_name"]
    conn_env.append("AWS_DEFAULT_REGION")
    return "{{ env.AWS_DEFAULT_REGION }}"


AWS_CRED_CAVEAT = "the resource uses boto3's default credential chain (no key fields are written: add `aws_access_key_id_env_var`, `aws_secret_access_key_env_var` or `profile_name` in defs/registry_resources/defs.yaml if the Airflow connection held keys)"
AWS_PROFILE_CAVEAT = "the resource uses boto3's default credential chain; it has no key fields, only `aws_profile` (add it in defs/registry_resources/defs.yaml to use a named profile)"
GCP_CRED_CAVEAT = "the resource uses Application Default Credentials (set `gcp_credentials_env_var` in defs/registry_resources/defs.yaml for a service-account JSON held in a variable)"


def prepare_args(t: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    """Arguments the IR could only record as a repr, read back when the repr is a literal the plan can use: Pub/Sub message data (bytes in Airflow) becomes text when it is UTF-8."""
    if (t.get("orig_type") or t["type"]) != "PubSubPublishMessageOperator" or not isinstance(a.get("messages"), list):
        return a
    msgs = []
    for m in a["messages"]:
        if isinstance(m, dict) and isinstance(m.get("data"), dict) and set(m["data"]) == {"repr"}:
            try:
                raw = ast.literal_eval(m["data"]["repr"])
                m = {**m, "data": raw.decode("utf-8") if isinstance(raw, bytes) else raw}
            except (ValueError, SyntaxError, UnicodeDecodeError):
                pass
        msgs.append(m)
    return {**a, "messages": msgs}


# ---------------------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------------------
HTTP_AUTH_CAVEAT = ("http_resource is configured with `{e}_BASE_URL` and `auth_header_env_var: {e}_AUTH_HEADER` (a fully formed Authorization header value; if the variable is unset no header is sent, "
                    "checked when definitions load). For basic, bearer or OAuth2 credentials replace it with `auth_type` and its `*_env_var` fields in defs/registry_resources/defs.yaml")


@_plan("HttpOperator", "HttpSensor")
def _http(t, a, ex):
    _handler_ok(t)
    opts = a.get("extra_options") or {}
    if set(opts) - {"timeout", "check_response"}:
        raise NoPlan(f"extra_options {sorted(set(opts) - {'timeout', 'check_response'})} are not carried by http_resource.request")
    sensor = t["type"] == "HttpSensor"
    method = a.get("method") or ("GET" if sensor else "POST")
    conn = a.get("http_conn_id") or "http_default"
    e = P.envname(conn)
    attrs = {"base_url": "{{ env." + e + "_BASE_URL }}", "auth_header_env_var": f"{e}_AUTH_HEADER"}
    cav = [HTTP_AUTH_CAVEAT.format(e=e)]
    if sensor:
        sn = t.get("sensor") or {}
        call = (f"return af_poke(context, lambda: {_res(conn)}.poke({method!r}, {ex(a.get('endpoint') or '')}, headers={ex(a.get('headers') or None)}, params={ex(a.get('request_params') or None)}, "
                f"timeout_seconds={opts.get('timeout')!r}), {sn.get('poke_interval', 60.0)!r}, {sn.get('timeout', 604800.0)!r}, False)")
        cav.append("HttpSensor on http_resource.poke: any status outside 2xx (and any connection error) means 'not yet'; Airflow fails the sensor on a non-404 error status unless it is in response_error_codes_allowlist")
        return Plan("HTTPResourceComponent", conn, attrs, call, ["requests"], conn_env=[f"{e}_BASE_URL", f"{e}_AUTH_HEADER (optional)"], caveats=cav)
    call = f"http_resource_call({_res(conn)}, {method!r}, {ex(a.get('endpoint'))}, {ex(a.get('data'))}, {ex(a.get('headers'))}, {opts!r}{', log=context.log' if a.get('log_response') else ''})"
    return Plan("HTTPResourceComponent", conn, attrs, call, ["requests"], conn_env=[f"{e}_BASE_URL", f"{e}_AUTH_HEADER (optional)"], caveats=cav)


# ---------------------------------------------------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------------------------------------------------
def _addrs(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,;]", v) if x.strip()]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return [y for x in v for y in _addrs(x)]
    raise NoPlan("an address list that is not text")


@_plan("EmailOperator")
def _email(t, a, ex):
    _handler_ok(t)
    _refuse(a, ("custom_headers",))
    if a.get("mime_subtype") not in (None, "", "mixed") or a.get("mime_charset") not in (None, "", "utf-8"):
        raise NoPlan(f"mime_subtype={a.get('mime_subtype')!r} / mime_charset={a.get('mime_charset')!r}: smtp_resource sends a multipart/alternative UTF-8 message")
    html = (t.get("template_files") or {}).get("html_content") or a.get("html_content")
    to = _addrs(a.get("to"))
    if not to or not isinstance(html, str):
        raise NoPlan("no recipient or html_content is not text")
    conn = a.get("conn_id") or "smtp_default"
    e = P.envname(conn)
    key = f"{conn}__smtp"
    lines, needs_os = [], False
    kw = [f"to={', '.join(to)!r}", f"subject={ex(a.get('subject'))}", "body=''", f"html_body={ex(html)}"]
    if a.get("from_email"):
        kw.append(f"from_addr={ex(a['from_email'])}")
    else:
        kw.append(f"from_addr=os.environ.get({e + '_FROM'!r}, '')")
        needs_os = True
    for k, name in (("cc", "cc"), ("bcc", "bcc")):
        if _addrs(a.get(k)):
            kw.append(f"{name}={_addrs(a.get(k))!r}")
    files = a.get("files")
    if files:
        if not (isinstance(files, list) and all(isinstance(f, str) for f in files)):
            raise NoPlan("files is not a list of paths")
        lines += ["import mimetypes", "import pathlib",
                  f"_files = [pathlib.Path(p) for p in {files!r}]",
                  "_attachments = [(p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or 'application/octet-stream') for p in _files]"]
        kw.append("attachments=_attachments")
    lines.append(f"{_res(key)}.send_email({', '.join(kw)})")
    return Plan("SMTPResourceComponent", key, {"host": "{{ env." + e + "_HOST }}", "port": "{{ env." + e + "_PORT }}"}, "\n".join(lines), [], needs_os=needs_os,
                conn_env=[f"{e}_HOST", f"{e}_PORT", f"{e}_FROM (sender when the operator sets none)"],
                caveats=["smtp_resource is configured with `host` and `port` from the environment and STARTTLS on (`use_tls`): set `use_tls: false` for a plain relay, and for a login add `username` with `password_env_var` "
                         "(both or neither) in defs/registry_resources/defs.yaml (no credential is written); the message is multipart/alternative with an empty text part and the HTML as the alternative, "
                         "where Airflow sends one HTML part"])


# ---------------------------------------------------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------------------------------------------------
@_plan("SlackWebhookOperator")
def _slack_webhook(t, a, ex):
    _handler_ok(t)
    _refuse(a, ("webhook_token", "proxy"))
    conn = a.get("slack_webhook_conn_id") or "slack_default"
    e = P.envname(conn)
    key = f"{conn}__slack"
    kw = [f"text={ex(a.get('message'))}"] if a.get("message") is not None else []
    if a.get("blocks") is not None:
        kw.append(f"blocks={ex(a['blocks'])}")
    for k in ("channel", "username", "icon_emoji", "icon_url", "attachments"):
        if a.get(k) is not None:
            kw.append(f"{k}={ex(a[k])}")
    cav = [f"slack_resource posts to the webhook URL in `{e}_WEBHOOK_URL` (name only in defs/registry_resources/defs.yaml; an unset variable fails config resolution for every run in the code location) and requires Slack's reply to be `ok`"]
    if a.get("channel"):
        cav.append(f"channel={a['channel']!r} is passed but slack_resource.post_message ignores it on the webhook path (the payload carries text, blocks and the other fields only): the message goes to the channel the webhook is bound to")
    if a.get("timeout") not in (None, 30):
        cav.append(f"timeout={a['timeout']!r} is not applied (the resource waits 30 s)")
    return Plan("SlackResourceComponent", key, {"webhook_url_env_var": f"{e}_WEBHOOK_URL"}, f"{_res(key)}.post_message({', '.join(kw)})", ["requests"], conn_env=[f"{e}_WEBHOOK_URL"], caveats=cav)


@_plan("SlackAPIPostOperator")
def _slack_api(t, a, ex):
    _handler_ok(t)
    conn = a.get("slack_conn_id") or "slack_api_default"
    e = P.envname(conn)
    key = f"{conn}__slack"
    kw = [f"channel={ex(a.get('channel') or '#general')}", f"text={ex(a.get('text') or 'No message has been set.')}", f"username={ex(a.get('username') or 'Airflow')}"]
    for k in ("icon_url", "attachments", "blocks"):
        if a.get(k) is not None:
            kw.append(f"{k}={ex(a[k])}")
    return Plan("SlackResourceComponent", key, {"token_env_var": f"{e}_TOKEN"}, f"{_res(key)}.post_message({', '.join(kw)})", ["slack-sdk"], conn_env=[f"{e}_TOKEN"],
                caveats=[f"slack_resource posts through `chat.postMessage` with the bot token in `{e}_TOKEN` (an `xoxb-` token with the chat:write scope; name only in defs/registry_resources/defs.yaml, and an unset variable fails config "
                         "resolution for every run in the code location). It replaces dagster_slack.SlackResource: the class is SlackPostResource now"])


# ---------------------------------------------------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------------------------------------------------
@_plan("SSHOperator")
def _ssh(t, a, ex):
    _handler_ok(t, allow=("environment",))
    _refuse(a, ("skip_on_exit_code", "get_pty"))
    if not isinstance(a.get("command"), str):
        raise NoPlan("command is not a string")
    conn = a.get("ssh_conn_id") or "ssh_default"
    e = P.envname(conn)
    key = f"{conn}__ssh"
    env = a.get("environment")
    if env and not (isinstance(env, dict) and _plain_json(env)):
        raise NoPlan("environment is not a plain dict")
    to = a.get("cmd_timeout")
    kw = [ex(a["command"])]
    if isinstance(to, (int, float)) and not isinstance(to, bool):
        kw.append(f"timeout={int(to)}")
    if env:
        kw.append(f"environment={ex({str(k): str(v) for k, v in env.items()})}")
    body = (f"_r = {_res(key)}.run_command({', '.join(kw)}, log=context.log)\n"
            "if _r['exit_code'] != 0:\n"
            "    raise dg.Failure(description=f\"SSH command exited with status {_r['exit_code']}\", metadata={'stderr': _r['stderr'][-4000:]})")
    return Plan("SSHResourceComponent", key, {"host_env_var": f"{e}_HOST", "username_env_var": f"{e}_USER"}, body, ["paramiko"], conn_env=[f"{e}_HOST", f"{e}_USER"],
                caveats=["ssh_resource reads the host and user from the variables `" + e + "_HOST` and `" + e + "_USER` (names only) and the port is its default 22; add one of `password_env_var`, `private_key_path_env_var` (an RSA key file) "
                         "or `private_key_env_var` (PEM text) in defs/registry_resources/defs.yaml (no credential is written); it accepts any host key unless `known_hosts_check: true`; the environment is applied as a quoted "
                         "`export` prefix (Airflow's paramiko environment needs sshd AcceptEnv)"])


# ---------------------------------------------------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------------------------------------------------
@_plan("LambdaInvokeFunctionOperator")
def _lambda(t, a, ex):
    _handler_ok(t)
    _refuse(a, ("botocore_config", "verify", "deferrable", "client_context"))
    if not isinstance(a.get("function_name"), str):
        raise NoPlan("function_name is not a string")
    pl = a.get("payload")
    if isinstance(pl, str) and pl.strip():
        try:
            pl = json.loads(pl)
        except ValueError:
            raise NoPlan("payload is text that is not JSON")
    if not (pl in (None, "", {}) or isinstance(pl, dict)):
        raise NoPlan("payload is not a JSON object (lambda_resource.invoke takes a dict)")
    if a.get("invocation_type") not in (None, "RequestResponse", "Event"):
        raise NoPlan(f"invocation_type {a.get('invocation_type')!r}")
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__lambda"
    kw = [ex(a["function_name"]), f"payload={ex(pl or None)}", f"invocation_type={(a.get('invocation_type') or 'RequestResponse')!r}"]
    if a.get("qualifier"):
        kw.append(f"qualifier={ex(a['qualifier'])}")
    if a.get("log_type") == "Tail":
        kw.append("include_logs=True")
    attrs = {"region_name": a["region_name"]} if isinstance(a.get("region_name"), str) and a["region_name"] else {}
    return Plan("LambdaResourceComponent", key, attrs, f"{_res(key)}.invoke({', '.join(kw)}, log=context.log)", ["boto3"], caveats=[AWS_CRED_CAVEAT + ("" if attrs else "; the region comes from boto3's chain")])


# ---------------------------------------------------------------------------------------------------------------------
# SQL by URL
# ---------------------------------------------------------------------------------------------------------------------
def split_sql(text: str) -> list[str] | None:
    """Statements of a SQL text split at top-level semicolons (quotes and comments respected), or None when a plain split is not safe
    (procedural bodies, dollar quoting, backslash escapes). Deterministic and dependency-free, on purpose."""
    if re.search(r"\$\w*\$|\bBEGIN\b|\bDECLARE\b|\bCREATE\s+(OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|TRIGGER)\b|\\", text, re.I):
        return None
    out, cur, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and not (text[j] == c and not (j + 1 < n and text[j + 1] == c)):
                j += 2 if (text[j] == c) else 1
            cur.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            cur.append(text[i:j])
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            cur.append(text[i:j])
            i = j
        elif c == ";":
            st = "".join(cur).strip()
            if re.sub(r"--[^\n]*|/\*.*?\*/", "", st, flags=re.S).strip():
                out.append(st)
            cur = []
            i += 1
        else:
            cur.append(c)
            i += 1
    st = "".join(cur).strip()
    if re.sub(r"--[^\n]*|/\*.*?\*/", "", st, flags=re.S).strip():
        out.append(st)
    return out


def _named_params(stmts: list[str], params: dict[str, Any]) -> list[str]:
    """Airflow passes `parameters` to the driver, so SQL carries the driver's `%(name)s`; sql_url_resource binds `:name` through SQLAlchemy `text()`. Rewrite when that is exact."""
    out = []
    for s in stmts:
        if re.search(r"%(?!\(\w+\)s)", s):
            raise NoPlan("parameters with a positional or escaped `%` in the SQL (sql_url_resource binds named `:name` parameters only)")
        s2 = re.sub(r"%\((\w+)\)s", r":\1", s)
        out.append(s2)
    used = {m for s in stmts for m in re.findall(r"%\((\w+)\)s", s)}
    if used - set(params):
        raise NoPlan(f"SQL placeholders {sorted(used - set(params))} have no value in parameters")
    return out


@_plan("SqlQuery")
def _sql(t, a, ex):
    _handler_ok(t)
    sql = P.sql_text(t)
    texts = [sql] if isinstance(sql, str) else list(sql)
    split = bool(a.get("split_statements"))
    stmts: list[str] = []
    for x in texts:
        if split:
            parts = split_sql(x)
            if parts is None:
                raise NoPlan("split_statements=True on SQL with a procedural body, dollar quoting or a backslash: it cannot be split exactly here")
            stmts += parts
        else:
            stmts.append(x)
    params = a.get("parameters")
    if isinstance(params, list) and params:
        raise NoPlan("positional parameters (sql_url_resource binds named parameters only)")
    if isinstance(params, dict) and params:
        stmts = _named_params(stmts, params)
    elif any(re.search(r"(?<![:\w]):[A-Za-z_]", s.replace("::", "")) for s in stmts):
        raise NoPlan("the SQL contains `:name` text that sql_url_resource would read as a bind parameter")
    conn = P.sql_conn(t)
    e = P.envname(conn)
    key = f"{conn}__sql"
    call = f"for _stmt in {stmts!r}:\n    context.log.info(f'Running statement: {{_stmt[:500]}}')\n    {_res(key)}.execute(_stmt{', ' + repr(params) if isinstance(params, dict) and params else ''})"
    cav = [f"sql_url_resource reads the SQLAlchemy URL from `{e}_URL` (name only; the driver for its dialect must be installed in the code location)"]
    if len(stmts) > 1 and not a.get("autocommit"):
        cav.append("each statement is committed on its own (sql_url_resource.execute has one statement per call and no shared transaction); Airflow runs them in one transaction")
    return Plan("SQLUrlResourceComponent", key, {"connection_url_env_var": f"{e}_URL"}, call, ["sqlalchemy"], conn_env=[f"{e}_URL"], caveats=cav)


# ---------------------------------------------------------------------------------------------------------------------
# Databricks
# ---------------------------------------------------------------------------------------------------------------------
RUN_NOW_KEYS = ("job_id", "job_name", "notebook_params", "python_params", "jar_params", "spark_submit_params", "job_parameters", "idempotency_token")
SUBMIT_TASK_KEYS = ("notebook_task", "spark_jar_task", "spark_python_task", "spark_submit_task", "pipeline_task", "python_wheel_task", "dbt_task", "new_cluster", "existing_cluster_id", "libraries")
SUBMIT_TOP_KEYS = ("tasks", "run_name", "timeout_seconds", "idempotency_token", "access_control_list", "git_source", "email_notifications", "notification_settings", "webhook_notifications",
                   "health", "queue", "run_as", "budget_policy_id", "environments")


@_plan("DatabricksRunNowOperator", "DatabricksSubmitRunOperator")
def _dbx(t, a, ex):
    _handler_ok(t, allow=("job_name", "no job_id", "repair_run", "cancel_previous_runs"))
    run_now = t["type"] == "DatabricksRunNowOperator"
    body = dict(a.get("json") or {})
    if not _plain_json(body):
        raise NoPlan("json is not a plain dict")
    if a.get("wait_for_termination") is False:
        raise NoPlan("wait_for_termination=False (databricks_jobs_resource always waits for the run)")
    if a.get("repair_run"):
        raise NoPlan("repair_run=True repairs the previous try's run, whose run id the retry would have to carry; repair_run_and_wait needs that id, so it is not wired from a retry")
    if a.get("cancel_previous_runs"):
        raise NoPlan("cancel_previous_runs=True (databricks_jobs_resource does not cancel earlier runs)")
    conn = a.get("databricks_conn_id") or "databricks_default"
    e = P.envname(conn)
    key = f"{conn}__jobs"
    cav = ["databricks_jobs_resource waits with the SDK's waiter and has no poll interval; the timeout is set to 7 days (the step has no other limit, as the Airflow task)"]
    if run_now:
        for k in RUN_NOW_KEYS:
            if not _empty(a.get(k)):
                body[k] = a[k]
        extra = set(body) - set(RUN_NOW_KEYS)
        if extra:
            raise NoPlan(f"run-now fields {sorted(extra)} have no argument on run_now_and_wait")
        if body.get("job_id") not in (None, "") and body.get("job_name"):
            raise NoPlan("both job_id and job_name are set (the resource takes one)")
        if body.get("job_id") not in (None, ""):
            try:
                body["job_id"] = int(body["job_id"])
            except (TypeError, ValueError):
                raise NoPlan("job_id is not a number")
        elif not body.get("job_name"):
            raise NoPlan("neither job_id nor job_name")
        else:
            cav.append("job_name is looked up with jobs.list(name=...): zero or several jobs with that name fail the step")
        call = f"{_res(key)}.run_now_and_wait({', '.join(f'{k}={ex(v)}' for k, v in body.items())}, poll_timeout_seconds=604800)"
    else:
        for k in (*SUBMIT_TASK_KEYS, *SUBMIT_TOP_KEYS, "spark_python_task"):
            if not _empty(a.get(k)) and k != "json":
                body[k] = a[k]
        top = {k: body[k] for k in body if k not in SUBMIT_TASK_KEYS}
        task = {k: body[k] for k in body if k in SUBMIT_TASK_KEYS}
        unknown = set(top) - set(SUBMIT_TOP_KEYS)
        if unknown:
            raise NoPlan(f"submit-run fields {sorted(unknown)} are not arguments of the SDK's jobs.submit")
        if task:
            if "tasks" in top:
                raise NoPlan("both `tasks` and single-task fields (notebook_task, new_cluster, ...) are set")
            rn = top.get("run_name") or t["task_id"]
            top["tasks"] = [{"task_key": rn if re.fullmatch(r"[\w\-]{1,100}", str(rn)) else t["task_id"], **task}]
            cav.append("the single-task fields (notebook_task, new_cluster, existing_cluster_id, ...) are wrapped into one entry of `tasks`, the form jobs.submit takes")
        if "run_name" not in top:
            top["run_name"] = t["task_id"]
        if not top.get("tasks"):
            raise NoPlan("no task to submit")
        call = f"{_res(key)}.submit_run_and_wait({ex(top)}, poll_timeout_seconds=604800)"
    if a.get("polling_period_seconds") not in (None, 30, 30.0):
        cav.append(f"polling_period_seconds={a['polling_period_seconds']!r} is not applied")
    cav.append(f"the workspace host is read from `{e}_HOST` and the token from `{e}_TOKEN` (name only; an unset token variable fails the call, an unset host fails definitions load)")
    return Plan("DatabricksJobsResourceComponent", key, {"workspace_host": "{{ env." + e + "_HOST }}", "workspace_token_env_var": f"{e}_TOKEN"}, call, ["databricks-sdk"],
                conn_env=[f"{e}_HOST", f"{e}_TOKEN"], caveats=cav)


# ---------------------------------------------------------------------------------------------------------------------
# Spark
# ---------------------------------------------------------------------------------------------------------------------
@_plan("SparkSubmitOperator")
def _spark(t, a, ex):
    _handler_ok(t, allow=("keytab", "principal", "proxy_user", "properties_file"))      # what the resource carries (keytab, principal, proxy_user as extra_args, properties_file) is not a reason to stay on the shim
    _refuse(a, ("use_krb5ccache", "env_vars_from"))
    if not isinstance(a.get("application"), str):
        raise NoPlan("application is not a string")
    opts = {k: a.get(k) for k in ("conf", "total_executor_cores", "executor_cores", "executor_memory", "driver_memory", "name", "num_executors", "java_class", "packages", "exclude_packages",
                                  "repositories", "jars", "py_files", "files", "archives", "driver_class_path", "queue", "deploy_mode", "properties_file") if not P._empty(a.get(k))}
    conn = a.get("conn_id") or "spark_default"
    e = P.envname(conn)
    split = lambda k: f"{[x for x in opts[k].split(',')]!r}" if isinstance(opts.get(k), str) else repr(opts.get(k))
    kw = [f"application={a['application']!r}", f"master=os.environ.get({e + '_MASTER'!r}, 'yarn')"]
    if a.get("application_args"):
        kw.append(f"application_args=[str(x) for x in {ex(a.get('application_args'))}]")
    for k, name in (("deploy_mode", "deploy_mode"), ("java_class", "class_name"), ("name", "name"), ("driver_memory", "driver_memory"), ("executor_memory", "executor_memory"),
                    ("num_executors", "num_executors"), ("total_executor_cores", "total_executor_cores"), ("executor_cores", "executor_cores"), ("queue", "queue"),
                    ("driver_class_path", "driver_class_path"), ("properties_file", "properties_file")):
        if k in opts:
            kw.append(f"{name}={opts[k]!r}")
    if "conf" in opts:
        kw.append(f"conf={{str(k): str(v) for k, v in {opts['conf']!r}.items()}}")
    for k in ("packages", "exclude_packages", "repositories", "jars", "py_files", "files", "archives"):
        if k in opts:
            kw.append(f"{k}={split(k)}")
    if a.get("verbose"):
        kw.append("verbose=True")
    if a.get("env_vars"):
        kw.append(f"env_vars={ex(a.get('env_vars'))}")
    extra, cav = [], ["the master comes from `<CONN>_MASTER` and the deploy mode from the operator, not from the Airflow connection; spark_submit_resource does not poll a cluster driver (the exit code decides)"]
    for flag, k in (("--keytab", "keytab"), ("--principal", "principal"), ("--proxy-user", "proxy_user")):
        if not P._empty(a.get(k)):
            extra += [flag, str(a[k])]
    if extra:
        kw.append(f"extra_args={extra!r}")
        cav.append("keytab, principal and proxy_user have no field on spark_submit_resource: they are passed as `extra_args` (`--keytab`, `--principal`, `--proxy-user`)")
    return Plan("SparkSubmitResourceComponent", conn, {"spark_submit_binary": a.get("spark_binary") or "spark-submit"}, f"{_res(conn)}.submit({', '.join(kw)}, log=context.log)", [],
                conn_env=[f"{e}_MASTER (default yarn)"], needs_os=True, caveats=cav)


# ---------------------------------------------------------------------------------------------------------------------
# Bucket lifecycle
# ---------------------------------------------------------------------------------------------------------------------
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
    if t["type"] == "GCSCreateBucketOperator":
        _refuse(a, ("resource", "labels", "predefined_acl", "predefined_default_object_acl", "retention_period", "uniform_bucket_level_access", "impersonation_chain", "location_extra"))
    else:
        _refuse(a, ("impersonation_chain",))
    conn = a.get("gcp_conn_id") or "google_cloud_default"
    key = f"{conn}__bucket"
    if t["type"] == "GCSCreateBucketOperator":
        kw = [ex(a.get("bucket_name")), ex(a.get("location") or "US")]
        if a.get("storage_class"):
            kw.append(f"storage_class={ex(a['storage_class'])}")
        if a.get("project_id"):
            kw.append(f"project={ex(a['project_id'])}")
        call = f"{_res(key)}.ensure_bucket({', '.join(kw)})"
    else:
        call = f"{_res(key)}.delete_bucket({ex(a.get('bucket_name'))}, {bool(a.get('force', True))!r})"
    return Plan("GcsBucketResourceComponent", key, {}, call, ["google-cloud-storage"], caveats=["gcs_bucket_resource uses Google's default credentials (GOOGLE_APPLICATION_CREDENTIALS)"])


# ---------------------------------------------------------------------------------------------------------------------
# EMR
# ---------------------------------------------------------------------------------------------------------------------
def _aws_attrs(a: dict[str, Any]) -> dict[str, Any]:
    return {"region_name": a["region_name"]} if isinstance(a.get("region_name"), str) and a["region_name"] else {}


@_plan("EmrAddStepsOperator")
def _emr_steps(t, a, ex):
    _refuse(a, ("execution_role_arn", "cluster_states"), {"cluster_states": None})
    steps = a.get("steps")
    if not (isinstance(steps, list) and steps and _plain_json(steps)):
        raise NoPlan("steps is not a plain list of step dicts")
    if not a.get("job_flow_id") and not a.get("job_flow_name"):
        raise NoPlan("neither job_flow_id nor job_flow_name")
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__emr"
    wait = bool(a.get("wait_for_completion"))
    where = f"cluster_id={ex(a['job_flow_id'])}" if a.get("job_flow_id") else f"cluster_name={ex(a['job_flow_name'])}"
    calls = []
    for st in steps:
        if not isinstance(st, dict) or set(st) - {"Name", "ActionOnFailure", "HadoopJarStep"} or not isinstance(st.get("HadoopJarStep"), dict):
            raise NoPlan("a step has keys other than Name, ActionOnFailure and HadoopJarStep")
        hj = st["HadoopJarStep"]
        if set(hj) - {"Jar", "MainClass", "Args", "Properties"}:
            raise NoPlan(f"HadoopJarStep keys {sorted(set(hj) - {'Jar', 'MainClass', 'Args', 'Properties'})}")
        props = hj.get("Properties")
        if props:
            if not all(isinstance(p, dict) and set(p) == {"Key", "Value"} for p in props):
                raise NoPlan("HadoopJarStep Properties are not Key/Value pairs")
            props = {p["Key"]: p["Value"] for p in props}
        kw = [where, f"step_name={ex(st.get('Name') or 'step')}", f"action_on_failure={(st.get('ActionOnFailure') or 'CONTINUE')!r}", f"jar={ex(hj.get('Jar') or 'command-runner.jar')}"]
        if hj.get("MainClass"):
            kw.append(f"main_class={ex(hj['MainClass'])}")
        if hj.get("Args"):
            kw.append(f"args={ex(hj['Args'])}")
        if props:
            kw.append(f"properties={ex(props)}")
        kw.append(f"wait_for_completion={wait!r}")
        if wait:
            kw.append(f"poll_interval_seconds={float(a.get('waiter_delay') or 30)!r}, timeout_seconds={int((a.get('waiter_max_attempts') or 60) * float(a.get('waiter_delay') or 30))}")
        calls.append(f"{_res(key)}.submit_step_and_wait({', '.join(kw)}, log=context.log)")
    cav = [AWS_CRED_CAVEAT + "; the region comes from boto3's chain unless the operator set region_name",
           "the steps are added one call each, in order (the operator adds them in one request); with wait_for_completion each is waited for before the next"]
    return Plan("EmrResourceComponent", key, _aws_attrs(a), "\n".join(calls), ["boto3"], caveats=cav)


EMR_TOP = {"Name", "Instances", "ReleaseLabel", "JobFlowRole", "ServiceRole", "LogUri", "VisibleToAllUsers", "Applications"}


@_plan("EmrCreateJobFlowOperator")
def _emr_create(t, a, ex):
    ov = a.get("job_flow_overrides")
    if not (isinstance(ov, dict) and _plain_json(ov)) or not ov.get("Name") or not isinstance(ov.get("Instances"), dict):
        raise NoPlan("job_flow_overrides is not a plain dict with Name and Instances (the operator would take the rest from the emr_conn_id connection, which the IR does not carry)")
    _refuse(a, ("emr_conn_id",), {"emr_conn_id": "emr_default"})
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__emr"
    apps = ov.get("Applications")
    extra = {k: v for k, v in ov.items() if k not in EMR_TOP}
    kw = [f"name={ex(ov['Name'])}", f"instances={ex(ov['Instances'])}"]
    if ov.get("ReleaseLabel"):
        kw.append(f"release_label={ex(ov['ReleaseLabel'])}")
    if apps:
        if all(isinstance(x, dict) and set(x) == {"Name"} for x in apps):
            kw.append(f"applications={[x['Name'] for x in apps]!r}")
        else:
            extra["Applications"] = apps
    for k, name in (("JobFlowRole", "job_flow_role"), ("ServiceRole", "service_role"), ("LogUri", "log_uri")):
        if ov.get(k):
            kw.append(f"{name}={ex(ov[k])}")
    if "VisibleToAllUsers" in ov:
        kw.append(f"visible_to_all_users={bool(ov['VisibleToAllUsers'])!r}")
    if extra:
        kw.append(f"extra_job_flow_args={ex(extra)}")
    wait = bool(a.get("wait_for_completion"))
    kw.append(f"wait_for_running={wait!r}")
    if wait:
        kw.append(f"poll_interval_seconds={float(a.get('waiter_delay') or 60)!r}, timeout_seconds={int((a.get('waiter_max_attempts') or 20) * float(a.get('waiter_delay') or 60))}")
    return Plan("EmrResourceComponent", key, _aws_attrs({**a, "region_name": a.get("region_name") or ov.get("region_name")}), f"{_res(key)}.create_cluster_and_wait({', '.join(kw)}, log=context.log)", ["boto3"],
                caveats=[AWS_CRED_CAVEAT, "the job flow's Name and Instances map to `name` and `instances`; Applications, roles, LogUri and the rest are passed through (`extra_job_flow_args` for keys with no named argument)"])


@_plan("EmrTerminateJobFlowOperator")
def _emr_terminate(t, a, ex):
    if not a.get("job_flow_id"):
        raise NoPlan("no job_flow_id")
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__emr"
    wait = bool(a.get("wait_for_completion"))
    kw = [f"cluster_id={ex(a['job_flow_id'])}", f"wait_for_termination={wait!r}"]
    if wait:
        kw.append(f"poll_interval_seconds={float(a.get('waiter_delay') or 60)!r}, timeout_seconds={int((a.get('waiter_max_attempts') or 20) * float(a.get('waiter_delay') or 60))}")
    return Plan("EmrResourceComponent", key, _aws_attrs(a), f"{_res(key)}.terminate_cluster({', '.join(kw)}, log=context.log)", ["boto3"], caveats=[AWS_CRED_CAVEAT])


# ---------------------------------------------------------------------------------------------------------------------
# Athena, Redshift Data API
# ---------------------------------------------------------------------------------------------------------------------
@_plan("AthenaOperator")
def _athena(t, a, ex):
    _refuse(a, ("client_request_token", "query_execution_context", "result_configuration", "deferrable"))
    q = (t.get("template_files") or {}).get("query") or a.get("query")
    if not isinstance(q, str):
        raise NoPlan("query is not text")
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__athena"
    env: list[str] = []
    region = _region(a, env)
    kw = [ex(q)]
    for k in ("database", "output_location", "workgroup"):
        if a.get(k):
            kw.append(f"{k}={ex(a[k])}")
    delay = float(a.get("sleep_time") or 30)
    kw.append(f"poll_interval_seconds={delay!r}")
    kw.append(f"timeout_seconds={float(a['max_polling_attempts']) * delay!r}" if a.get("max_polling_attempts") else "timeout_seconds=None")
    return Plan("AthenaResourceComponent", key, {"region_name": region}, f"{_res(key)}.run_query_and_wait({', '.join(kw)}, log=context.log)", ["boto3"], conn_env=env,
                caveats=[AWS_PROFILE_CAVEAT])


@_plan("RedshiftDataOperator")
def _redshift(t, a, ex):
    _refuse(a, ("statement_name", "with_event", "return_sql_result", "parameters", "session_id", "session_keepalive_seconds", "deferrable"))
    if a.get("wait_for_completion") is False:
        raise NoPlan("wait_for_completion=False (redshift_data_api_resource waits for the statement)")
    sql = a.get("sql")
    if isinstance(sql, list) and len(sql) == 1:
        sql = sql[0]
    if not isinstance(sql, str):
        raise NoPlan("sql is a list of statements (the Data API runs them as one batch; the resource takes one statement per call)")
    if not a.get("database") or bool(a.get("cluster_identifier")) == bool(a.get("workgroup_name")):
        raise NoPlan("needs a database and exactly one of cluster_identifier or workgroup_name")
    conn = a.get("aws_conn_id") or "aws_default"
    key = f"{conn}__redshift"
    env: list[str] = []
    region = _region(a, env)
    kw = [ex(sql), f"database={ex(a['database'])}"]
    for k in ("cluster_identifier", "db_user", "workgroup_name", "secret_arn"):
        if a.get(k):
            kw.append(f"{k}={ex(a[k])}")
    kw += [f"poll_interval_seconds={float(a.get('poll_interval') or 10)!r}", "timeout_seconds=None"]
    return Plan("RedshiftDataApiResourceComponent", key, {"region_name": region}, f"{_res(key)}.execute_and_wait({', '.join(kw)}, log=context.log)", ["boto3"], conn_env=env,
                caveats=[AWS_PROFILE_CAVEAT,
                         "the resource does not fetch result rows (counts only)"])


# ---------------------------------------------------------------------------------------------------------------------
# Dataproc
# ---------------------------------------------------------------------------------------------------------------------
def _dp_base(a: dict[str, Any]) -> tuple[str, str]:
    if not (isinstance(a.get("project_id"), str) and isinstance(a.get("region"), str) and a["project_id"] and a["region"]):
        raise NoPlan("project_id and region must be set on the operator (the connection's project is not in the IR)")
    return a["project_id"], a["region"]


def _dp_cluster_config(c: dict[str, Any]) -> dict[str, Any]:
    """The Dataproc ClusterConfig dict as the flat arguments of ensure_cluster, or NoPlan for anything the flat form cannot say."""
    if not isinstance(c, dict) or not _plain_json(c):
        raise NoPlan("cluster_config is not a plain dict (a ClusterGenerator object is not read back)")
    out: dict[str, Any] = {}
    extra = set(c) - {"gce_cluster_config", "master_config", "worker_config", "software_config", "lifecycle_config"}
    if extra:
        raise NoPlan(f"cluster_config keys {sorted(extra)} have no counterpart in ensure_cluster's flat cluster_config")
    g = c.get("gce_cluster_config") or {}
    if set(g) - {"zone_uri", "service_account", "tags"}:
        raise NoPlan(f"gce_cluster_config keys {sorted(set(g) - {'zone_uri', 'service_account', 'tags'})}")
    for k, name in (("zone_uri", "zone"), ("service_account", "service_account"), ("tags", "network_tags")):
        if g.get(k):
            out[name] = g[k]
    for role, prefix in (("master_config", "master"), ("worker_config", "worker")):
        m = c.get(role) or {}
        if set(m) - {"num_instances", "machine_type_uri", "disk_config"}:
            raise NoPlan(f"{role} keys {sorted(set(m) - {'num_instances', 'machine_type_uri', 'disk_config'})}")
        if m.get("machine_type_uri"):
            out[f"{prefix}_machine_type"] = m["machine_type_uri"]
        if prefix == "worker" and m.get("num_instances") is not None:
            out["num_workers"] = m["num_instances"]
        if prefix == "master" and m.get("num_instances") not in (None, 1):
            raise NoPlan("master_config.num_instances other than 1 (high-availability masters)")
        d = m.get("disk_config") or {}
        if set(d) - {"boot_disk_size_gb"}:
            raise NoPlan(f"{role}.disk_config keys {sorted(set(d) - {'boot_disk_size_gb'})}")
        if d.get("boot_disk_size_gb"):
            out[f"{prefix}_boot_disk_size_gb"] = d["boot_disk_size_gb"]
    s = c.get("software_config") or {}
    if set(s) - {"image_version", "properties"}:
        raise NoPlan(f"software_config keys {sorted(set(s) - {'image_version', 'properties'})}")
    if s.get("image_version"):
        out["image_version"] = s["image_version"]
    if s.get("properties"):
        out["software_properties"] = s["properties"]
    lc = c.get("lifecycle_config") or {}
    if lc:
        raise NoPlan("lifecycle_config (the flat form takes idle_delete_ttl_seconds only; the operator's value is a duration string)")
    return out


@_plan("DataprocCreateClusterOperator")
def _dp_create(t, a, ex):
    _refuse(a, ("virtual_cluster_config", "labels", "request_id", "deferrable", "retry", "metadata"))
    if a.get("use_if_exists") is False:
        raise NoPlan("use_if_exists=False (ensure_cluster leaves an existing cluster as it is)")
    proj, region = _dp_base(a)
    if not a.get("cluster_name"):
        raise NoPlan("no cluster_name")
    cfg = _dp_cluster_config(a.get("cluster_config") or {})
    key = f"{a.get('gcp_conn_id') or 'google_cloud_default'}__dataproc"
    return Plan("DataprocResourceComponent", key, {}, f"{_res(key)}.ensure_cluster({ex(proj)}, {ex(region)}, {ex(a['cluster_name'])}, cluster_config={ex(cfg or None)}, log=context.log)",
                ["google-cloud-dataproc"], caveats=[GCP_CRED_CAVEAT, "the cluster configuration is passed as ensure_cluster's flat cluster_config (machine types, workers, disk sizes, zone, image version, properties); delete_on_error is not applied"])


@_plan("DataprocDeleteClusterOperator")
def _dp_delete(t, a, ex):
    _refuse(a, ("cluster_uuid", "request_id", "deferrable", "retry", "metadata"))
    proj, region = _dp_base(a)
    if not a.get("cluster_name"):
        raise NoPlan("no cluster_name")
    key = f"{a.get('gcp_conn_id') or 'google_cloud_default'}__dataproc"
    return Plan("DataprocResourceComponent", key, {}, f"{_res(key)}.delete_cluster({ex(proj)}, {ex(region)}, {ex(a['cluster_name'])}, log=context.log)", ["google-cloud-dataproc"],
                caveats=[GCP_CRED_CAVEAT, "delete_cluster succeeds when the cluster is already absent (the operator fails on a missing cluster)"])


DP_JOB_TYPES = {"pyspark_job": "pyspark", "spark_job": "spark", "hive_job": "hive", "pig_job": "pig"}


@_plan("DataprocSubmitJobOperator")
def _dp_submit(t, a, ex):
    _refuse(a, ("asynchronous", "deferrable", "cancel_on_kill", "wait_timeout", "request_id", "retry", "metadata"), {"cancel_on_kill": True})
    proj, region = _dp_base(a)
    job = a.get("job")
    if not (isinstance(job, dict) and _plain_json(job)):
        raise NoPlan("job is not a plain dict")
    kinds = [k for k in job if k in DP_JOB_TYPES]
    other = set(job) - set(kinds) - {"placement", "labels"}
    if len(kinds) != 1 or other or not isinstance(job.get("placement"), dict) or set(job["placement"]) != {"cluster_name"}:
        raise NoPlan("job has fields other than placement.cluster_name, labels and one of pyspark_job, spark_job, hive_job, pig_job")
    jt = DP_JOB_TYPES[kinds[0]]
    cfg = dict(job[kinds[0]])
    allowed = {"pyspark": {"main_python_file_uri", "args", "python_file_uris", "jar_file_uris", "file_uris", "archive_uris", "properties"},
               "spark": {"main_jar_file_uri", "main_class", "args", "jar_file_uris", "file_uris", "archive_uris", "properties"},
               "hive": {"query_file_uri", "query_list", "continue_on_failure", "script_variables", "properties", "jar_file_uris"}, "pig": {"query_file_uri", "query_list", "continue_on_failure", "script_variables", "properties", "jar_file_uris"}}[jt]
    if set(cfg) - allowed:
        raise NoPlan(f"{kinds[0]} keys {sorted(set(cfg) - allowed)} have no counterpart in submit_job_and_wait's job_config")
    if "query_list" in cfg:
        ql = cfg["query_list"]
        cfg["query_list"] = ql.get("queries") if isinstance(ql, dict) else ql
    if job.get("labels"):
        cfg["labels"] = job["labels"]
    key = f"{a.get('gcp_conn_id') or 'google_cloud_default'}__dataproc"
    return Plan("DataprocResourceComponent", key, {}, f"{_res(key)}.submit_job_and_wait({ex(proj)}, {ex(region)}, {ex(job['placement']['cluster_name'])}, {jt!r}, {ex(cfg)}, log=context.log)",
                ["google-cloud-dataproc"], caveats=[GCP_CRED_CAVEAT, "poll interval 10 s and timeout 3600 s (resource defaults)"])


@_plan("DataprocCreateBatchOperator")
def _dp_batch(t, a, ex):
    _refuse(a, ("request_id", "num_retries_if_resource_is_not_ready", "result_retry", "deferrable", "asynchronous", "metadata", "timeout", "polling_interval_seconds", "retry"))
    proj, region = _dp_base(a)
    if not (isinstance(a.get("batch"), dict) and _plain_json(a["batch"])) or not isinstance(a.get("batch_id"), str) or not a["batch_id"]:
        raise NoPlan("batch is not a plain dict or there is no batch_id (the operator would generate one)")
    key = f"{a.get('gcp_conn_id') or 'google_cloud_default'}__dataproc"
    return Plan("DataprocResourceComponent", key, {}, f"{_res(key)}.create_batch_and_wait({ex(proj)}, {ex(region)}, {ex(a['batch_id'])}, {ex(a['batch'])}, log=context.log)", ["google-cloud-dataproc"],
                caveats=[GCP_CRED_CAVEAT])


# ---------------------------------------------------------------------------------------------------------------------
# Pub/Sub
# ---------------------------------------------------------------------------------------------------------------------
def _ps(a: dict[str, Any], *need: str) -> str:
    if any(not (isinstance(a.get(k), str) and a[k]) for k in need):
        raise NoPlan(f"{', '.join(need)} must be set to text on the operator")
    return f"{a.get('gcp_conn_id') or 'google_cloud_default'}__pubsub"


PS_CAV = "pubsub_resource uses Application Default Credentials (set `credentials_env_var` in defs/registry_resources/defs.yaml for a service-account JSON path held in a variable)"


@_plan("PubSubCreateTopicOperator")
def _ps_topic(t, a, ex):
    _refuse(a, ("message_storage_policy", "kms_key_name", "retry", "timeout", "metadata", "impersonation_chain"))
    if a.get("fail_if_exists"):
        raise NoPlan("fail_if_exists=True (ensure_topic never fails on an existing topic)")
    key = _ps(a, "project_id", "topic")
    kw = [ex(a["project_id"]), ex(a["topic"])] + ([f"labels={ex(a['labels'])}"] if a.get("labels") else [])
    return Plan("PubsubResourceComponent", key, {}, f"{_res(key)}.ensure_topic({', '.join(kw)})", ["google-cloud-pubsub"], caveats=[PS_CAV])


@_plan("PubSubDeleteTopicOperator")
def _ps_dtopic(t, a, ex):
    _refuse(a, ("retry", "timeout", "metadata", "impersonation_chain"))
    if a.get("fail_if_not_exists"):
        raise NoPlan("fail_if_not_exists=True (delete_topic succeeds on a missing topic)")
    key = _ps(a, "project_id", "topic")
    return Plan("PubsubResourceComponent", key, {}, f"{_res(key)}.delete_topic({ex(a['project_id'])}, {ex(a['topic'])})", ["google-cloud-pubsub"], caveats=[PS_CAV])


@_plan("PubSubCreateSubscriptionOperator")
def _ps_sub(t, a, ex):
    _refuse(a, ("retain_acked_messages", "message_retention_duration", "labels", "enable_message_ordering", "expiration_policy", "filter_", "dead_letter_policy", "retry_policy",
                "retry", "timeout", "metadata", "impersonation_chain", "subscription_project_id"))
    if a.get("fail_if_exists"):
        raise NoPlan("fail_if_exists=True (ensure_subscription never fails on an existing subscription)")
    pc = a.get("push_config")
    push = None
    if pc:
        push = pc.get("pushEndpoint") or pc.get("push_endpoint") if isinstance(pc, dict) else None
        if not push or set(pc) - {"pushEndpoint", "push_endpoint"}:
            raise NoPlan("push_config has settings other than the endpoint")
    key = _ps(a, "project_id", "topic", "subscription")
    kw = [ex(a["project_id"]), ex(a["topic"]), ex(a["subscription"]), f"ack_deadline_seconds={int(a.get('ack_deadline_secs') or 10)}"] + ([f"push_endpoint={ex(push)}"] if push else [])
    return Plan("PubsubResourceComponent", key, {}, f"{_res(key)}.ensure_subscription({', '.join(kw)})", ["google-cloud-pubsub"], caveats=[PS_CAV])


@_plan("PubSubDeleteSubscriptionOperator")
def _ps_dsub(t, a, ex):
    _refuse(a, ("retry", "timeout", "metadata", "impersonation_chain"))
    if a.get("fail_if_not_exists"):
        raise NoPlan("fail_if_not_exists=True (delete_subscription succeeds on a missing subscription)")
    key = _ps(a, "project_id", "subscription")
    return Plan("PubsubResourceComponent", key, {}, f"{_res(key)}.delete_subscription({ex(a['project_id'])}, {ex(a['subscription'])})", ["google-cloud-pubsub"], caveats=[PS_CAV])


@_plan("PubSubPublishMessageOperator")
def _ps_publish(t, a, ex):
    _refuse(a, ("retry", "timeout", "metadata", "impersonation_chain"))
    key = _ps(a, "project_id", "topic")
    msgs = a.get("messages")
    if not (isinstance(msgs, list) and msgs and all(isinstance(m, dict) and isinstance(m.get("data"), str) and not (set(m) - {"data", "attributes", "ordering_key"}) for m in msgs)):
        raise NoPlan("messages are not text `data` (UTF-8 bytes read back from their repr) with optional attributes and ordering_key")
    lines = []
    for m in msgs:
        kw = [ex(a["project_id"]), ex(a["topic"]), ex(m["data"])]
        if m.get("attributes"):
            kw.append(f"attributes={ex(m['attributes'])}")
        if m.get("ordering_key"):
            kw.append(f"ordering_key={ex(m['ordering_key'])}")
        lines.append(f"{_res(key)}.publish({', '.join(kw)})")
    return Plan("PubsubResourceComponent", key, {}, "\n".join(lines), ["google-cloud-pubsub"],
                caveats=[PS_CAV, "messages are published one call each, in order, and each waits for the service's acknowledgement; ordering keys need message ordering enabled on the topic's publisher (the resource does not enable it)"])


# ---------------------------------------------------------------------------------------------------------------------
# BigQuery DDL and value check
# ---------------------------------------------------------------------------------------------------------------------
BQ_CAV = ("bigquery_ddl_resource reads a service-account JSON file named by `GOOGLE_APPLICATION_CREDENTIALS` (or `credentials_path`, or an inline `credentials` dict): other Application Default Credentials "
          "(workload identity, gcloud login) are not accepted. No credential is written into defs/registry_resources/defs.yaml")


def _bq_key(a: dict[str, Any]) -> str:
    """One resource per connection and location (the location is the resource's default for query jobs, so two locations are two resources)."""
    loc = a.get("location")
    suffix = "_" + re.sub(r"\W", "_", loc) if isinstance(loc, str) and loc else ""
    return f"{a.get('gcp_conn_id') or 'google_cloud_default'}__bqddl{suffix}"


def _bq_attrs(a: dict[str, Any]) -> dict[str, Any]:
    return {"location": a["location"]} if isinstance(a.get("location"), str) and a["location"] else {}


def _qualify(project: Any, dataset: str) -> str:
    return dataset if "." in dataset or not isinstance(project, str) or not project else f"{project}.{dataset}"


@_plan("BigQueryCreateEmptyDatasetOperator")
def _bq_ds(t, a, ex):
    _refuse(a, ("dataset_reference", "impersonation_chain", "if_exists"))
    if not isinstance(a.get("dataset_id"), str):
        raise NoPlan("dataset_id is not text")
    kw = [ex(_qualify(a.get("project_id"), a["dataset_id"]))] + ([f"location={ex(a['location'])}"] if a.get("location") else [])
    return Plan("BigQueryDdlResourceComponent", _bq_key(a), _bq_attrs(a), f"{_res(_bq_key(a))}.ensure_dataset({', '.join(kw)})", ["google-cloud-bigquery"],
                caveats=[BQ_CAV, "ensure_dataset is always exists_ok=True; with exists_ok=False the operator fails on an existing dataset" if a.get("exists_ok") is False else "ensure_dataset is exists_ok=True"])


@_plan("BigQueryDeleteDatasetOperator")
def _bq_dds(t, a, ex):
    _refuse(a, ("impersonation_chain",))
    if not isinstance(a.get("dataset_id"), str):
        raise NoPlan("dataset_id is not text")
    return Plan("BigQueryDdlResourceComponent", _bq_key(a), _bq_attrs(a), f"{_res(_bq_key(a))}.delete_dataset({ex(_qualify(a.get('project_id'), a['dataset_id']))}, delete_contents={bool(a.get('delete_contents'))!r})",
                ["google-cloud-bigquery"], caveats=[BQ_CAV])


@_plan("BigQueryCreateEmptyTableOperator")
def _bq_tbl(t, a, ex):
    _refuse(a, ("table_resource", "gcs_schema_object", "view", "materialized_view", "encryption_configuration", "impersonation_chain", "bigquery_conn_id", "google_cloud_storage_conn_id"))
    if not (isinstance(a.get("dataset_id"), str) and isinstance(a.get("table_id"), str)):
        raise NoPlan("dataset_id or table_id is not text")
    schema = a.get("schema_fields")
    if not (isinstance(schema, list) and schema and all(isinstance(f, dict) and f.get("name") and set(f) <= {"name", "type", "mode", "description"} for f in schema)):
        raise NoPlan("schema_fields is not a plain list of name/type/mode/description fields")
    kw = [ex(_qualify(a.get("project_id"), a["dataset_id"])), ex(a["table_id"]), ex(schema)]
    tp = a.get("time_partitioning")
    if tp:
        if not isinstance(tp, dict) or set(tp) - {"type", "field"}:
            raise NoPlan("time_partitioning has settings other than type and field (expiration_ms is not carried)")
        if tp.get("field"):
            kw.append(f"table_partition_field={ex(tp['field'])}")
        kw.append(f"table_partition_type={(tp.get('type') or 'DAY')!r}")
    if a.get("cluster_fields"):
        kw.append(f"cluster_fields={ex(a['cluster_fields'])}")
    if a.get("labels"):
        kw.append(f"labels={ex(a['labels'])}")
    return Plan("BigQueryDdlResourceComponent", _bq_key(a), _bq_attrs(a), f"{_res(_bq_key(a))}.ensure_table({', '.join(kw)})", ["google-cloud-bigquery"],
                caveats=[BQ_CAV, "ensure_table is exists_ok=True and never alters an existing table's columns"])


@_plan("BigQueryDeleteTableOperator")
def _bq_dtbl(t, a, ex):
    _refuse(a, ("impersonation_chain",))
    ref = a.get("deletion_dataset_table")
    if not isinstance(ref, str) or ref.count(".") not in (1, 2):
        raise NoPlan("deletion_dataset_table is not `[project.]dataset.table` text")
    ds, _, tb = ref.rpartition(".")
    cav = [BQ_CAV]
    if not a.get("ignore_if_missing"):
        cav.append("delete_table succeeds on a missing table (the operator fails unless ignore_if_missing)")
    return Plan("BigQueryDdlResourceComponent", _bq_key(a), _bq_attrs(a), f"{_res(_bq_key(a))}.delete_table({ex(ds)}, {ex(tb)})", ["google-cloud-bigquery"], caveats=cav)


@_plan("BigQueryValueCheckOperator")
def _bq_check(t, a, ex):
    _refuse(a, ("encryption_configuration", "impersonation_chain", "labels", "deferrable"))
    if not isinstance(a.get("sql"), str):
        raise NoPlan("sql is not text")
    if a.get("tolerance") not in (None, 0):
        raise NoPlan("tolerance is relative in Airflow (a fraction of pass_value) and absolute in run_value_check")
    kw = [ex(a["sql"]), ex(a.get("pass_value"))]
    body = (f"_r = {_res(_bq_key(a))}.run_value_check({', '.join(kw)})\n"
            "if not _r['passed']:\n"
            "    raise dg.Failure(description=f\"Value check failed: expected {_r['actual_value']!r} to equal the pass value\", metadata={'query': _r['rendered_query']})")
    return Plan("BigQueryDdlResourceComponent", _bq_key(a), _bq_attrs(a), body, ["google-cloud-bigquery"],
                caveats=[BQ_CAV, "the check query runs as standard SQL (Airflow's use_legacy_sql default is True)"])


def plan_or_reason(t: dict[str, Any], ex: Callable[[Any], str]) -> tuple[Plan | None, str | None]:
    """(plan, None), (None, reason) when the operator has a plan but not for these arguments, or (None, None) when it has no plan."""
    fn = RESOURCE_PLANS.get(t.get("orig_type") or t["type"]) or RESOURCE_PLANS.get(t["type"])
    if fn is None:
        return None, None
    try:
        return fn(t, prepare_args(t, P._a(t)), ex), None
    except NoPlan as e:
        return None, str(e)


def plan_for(t: dict[str, Any], ex: Callable[[Any], str]) -> Plan | None:
    """The resource plan of a task, or None when the operator has none or its arguments need something the resource cannot do."""
    return plan_or_reason(t, ex)[0]
