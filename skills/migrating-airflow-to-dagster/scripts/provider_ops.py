"""Provider operators that convert_ir.py translates to calls into Dagster integration libraries or plain Python.

Each handler maps one family of Airflow provider operators. A handler has three parts:

    check(t)         -> (stubs, degraded)   pure on the IR: reasons the task must be a loud stub, and differences to state in the report
    build(t, ex)     -> Prov                the Python statement for the asset body; `ex(value)` renders a value (templates, XComs) as source
    template_values(t)                      what Airflow renders as templates, so convert_ir.py can check the Jinja it contains

Connections are never copied. A connection becomes either environment variables named after the Airflow connection id (read when
the step runs: HTTP, SQL, SMTP, Slack webhook) or an integration resource in resources.py whose credentials are `dg.EnvVar`s of
the same naming scheme (dbt Cloud, Airbyte, Fivetran, Databricks). The names are listed per connection in the report.

The runtime helpers the generated code calls are in PROVIDER_RUNTIME, which convert_ir.py appends to the generated `runtime.py`.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Prov:
    stmt: str                                    # source of the body (a `return ...` line when the operator pushes a value)
    kind: str                                    # key of KINDS
    conn: str | None = None                      # the Airflow connection id this uses
    resource: bool = False                       # True: the body reads `context.resources.<conn>` (an integration resource in resources.py)
    kinds_tag: str = "python"                    # the Dagster asset kind shown in the catalog
    degraded: list[str] = field(default_factory=list)


def envname(conn_id: str) -> str:
    return re.sub(r"\W", "_", conn_id).upper()


UNVERIFIED = ("UNVERIFIED against a live {what}: the call and its arguments were checked against the real library (its signature, and where noted its client run against a local fake of the service) "
              "or a recording fake, not against a real {what}")


def _a(t: dict[str, Any]) -> dict[str, Any]:
    """Constructor arguments and template fields together (the same value appears in both for template fields)."""
    return {**(t.get("args") or {}), **(t.get("template_fields") or {})}


def _empty(v: Any) -> bool:
    return v in (None, False, "", [], {}) or (isinstance(v, str) and not v.strip())


def _plain_dict(v: Any) -> bool:
    return isinstance(v, dict) and not ({"repr", "xcom_from"} & set(v))


def k8s_obj(v: Any) -> Any:
    """A Kubernetes model object the IR could only record as its repr (`{'repr': "{'name': 'MODE', 'value': 'full', 'value_from': None}"}`):
    the repr is the object's dict, so read it back as one and drop the unset fields. Anything that does not read back as a dict or list is refused."""
    if isinstance(v, list):
        return [k8s_obj(x) for x in v]
    if isinstance(v, dict) and set(v) == {"repr"}:
        try:
            parsed = ast.literal_eval(v["repr"])
        except (ValueError, SyntaxError):
            raise ValueError(f"a Kubernetes object that cannot be read back ({v['repr'][:60]!r})")
        return k8s_obj(parsed)
    if isinstance(v, dict):
        return {k: k8s_obj(x) for k, x in v.items() if x is not None}
    return v


# ---------------------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------------------
HTTP_OPTIONS = {"timeout", "verify", "check_response", "allow_redirects"}
HTTP_OPS = {"HttpOperator", "SimpleHttpOperator", "HttpSensor"}


def _http_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a, ca = _a(t), t.get("callable_args") or {}
    stubs: list[str] = []
    for k in ("response_check", "response_filter", "pagination_function"):
        if k in ca:
            stubs.append(f"{k} is a function ({ca[k]}); the converter does not carry it into the step")
    if not _empty(a.get("auth_type")):
        stubs.append("auth_type is set (the connection's login and password are not applied through it here)")
    if not _empty(a.get("request_kwargs")):
        stubs.append("request_kwargs is set")
    if not _empty(a.get("retry_args")):
        stubs.append("retry_args is set (tenacity retries on the request)")
    opts = a.get("extra_options") or {}
    if not isinstance(opts, dict) or set(opts) - HTTP_OPTIONS:
        stubs.append(f"extra_options {sorted(set(opts) - HTTP_OPTIONS) if isinstance(opts, dict) else opts!r} are not carried (supported: {sorted(HTTP_OPTIONS)})")
    if not isinstance(a.get("method") or "GET", str):
        stubs.append("method is not a string")
    deg: list[str] = []
    if a.get("deferrable"):
        deg.append("deferrable=True is ignored: the request runs inside the step")
    return stubs, deg


def _http_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("http_conn_id") or "http_default"
    opts = a.get("extra_options") or {}
    common = f"{conn!r}, {(a.get('method') or ('GET' if t['type'] == 'HttpSensor' else 'POST'))!r}, {ex(a.get('endpoint'))}"
    if t["type"] == "HttpSensor":
        codes = [str(c) for c in (a.get("response_error_codes_allowlist") or [])]
        call = (f"http_poke({common}, {ex(a.get('request_params'))}, {ex(a.get('headers'))}, {opts!r}, {codes!r})")
        return Prov(call, "http", conn)
    call = f"http_run(context, {common}, {ex(a.get('data'))}, {ex(a.get('headers'))}, {opts!r}, {bool(a.get('log_response'))!r})"
    return Prov("return " + call, "http", conn, kinds_tag="http")


# ---------------------------------------------------------------------------------------------------------------------
# SQL on databases other than Snowflake
# ---------------------------------------------------------------------------------------------------------------------
SQL_OPS = {"SQLExecuteQueryOperator", "PostgresOperator", "MySqlOperator", "MsSqlOperator", "SqliteOperator"}
SQL_CONN_ARGS = ("conn_id", "postgres_conn_id", "mysql_conn_id", "mssql_conn_id", "sqlite_conn_id")


def sql_conn(t: dict[str, Any]) -> str:
    a = _a(t)
    for k in SQL_CONN_ARGS:
        if a.get(k):
            return a[k]
    return {"PostgresOperator": "postgres_default", "MySqlOperator": "mysql_default", "MsSqlOperator": "mssql_default",
            "SqliteOperator": "sqlite_default"}.get(t.get("orig_type") or t["type"], "sql_default")


def sql_text(t: dict[str, Any]) -> Any:
    return (t.get("template_files") or {}).get("sql") or _a(t).get("sql")


def _sql_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a, ca = _a(t), t.get("callable_args") or {}
    stubs: list[str] = []
    sql = sql_text(t)
    if not (isinstance(sql, str) or (isinstance(sql, list) and all(isinstance(x, str) for x in sql))):
        stubs.append("sql is not a string or a list of strings")
    if "handler" in ca and not ca["handler"].endswith("fetch_all_handler"):
        stubs.append(f"handler {ca['handler']} changes what the step returns")
    if not _empty(a.get("output_processor")) or "output_processor" in ca:
        stubs.append("output_processor is set")
    if not _empty(a.get("database")):
        stubs.append(f"database={a['database']!r} overrides the connection's database (put it in the connection URL instead)")
    if not _empty(a.get("hook_params")):
        stubs.append("hook_params is set")
    if a.get("return_last") is False and isinstance(sql, list) and len(sql) > 1:
        stubs.append("return_last=False returns every statement's result")
    if not isinstance(a.get("parameters"), (dict, list, type(None))):
        stubs.append("parameters is not a dict or a list")
    return stubs, []


def _sql_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a, conn = _a(t), sql_conn(t)
    sql = sql_text(t)
    stmts = [sql] if isinstance(sql, str) else sql
    call = (f"sql_run(context, {conn!r}, {ex(stmts)}, {ex(a.get('parameters'))}, autocommit={bool(a.get('autocommit'))!r}, "
            f"split={bool(a.get('split_statements'))!r})")
    return Prov("return " + call, "sql", conn, kinds_tag="sql")


# ---------------------------------------------------------------------------------------------------------------------
# Kubernetes pods
# ---------------------------------------------------------------------------------------------------------------------
K8S_REFUSED = ("secrets", "ports", "init_containers", "full_pod_spec", "pod_template_file", "pod_template_dict", "hostnetwork", "host_aliases", "dns_config",
               "dnspolicy", "hostname", "subdomain", "schedulername", "pod_runtime_info_envs", "termination_grace_period", "container_security_context",
               "security_context", "priority_class_name", "skip_on_exit_code", "callbacks", "progress_callback")


def _k8s_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    stubs: list[str] = []
    for k in K8S_REFUSED:
        if not _empty(a.get(k)):
            stubs.append(f"{k} is set and has no counterpart in the converted step (execute_k8s_job)")
    if a.get("do_xcom_push"):
        stubs.append("do_xcom_push=True reads /airflow/xcom/return.json from a sidecar, which the converted step does not do")
    if not isinstance(a.get("image"), str):
        stubs.append("image is not a string")
    for k in ("env_vars", "image_pull_secrets", "volumes", "volume_mounts", "container_resources", "env_from", "tolerations", "affinity"):
        try:
            k8s_obj(a.get(k))
        except ValueError as e:
            stubs.append(f"{k}: {e}")
    try:
        for e in _env_list(a.get("env_vars")):
            if "value_from" in e:
                stubs.append(f"env var {e.get('name')!r} reads from a secret, config map or field reference (value_from), which is not carried")
    except ValueError as e:
        stubs.append(str(e))
    deg = [UNVERIFIED.format(what="Kubernetes cluster")]
    deg.append("a Job that succeeds is left in the cluster (with its pod): execute_k8s_job does not delete it, as the operator's on_finish_action would; "
               "clean up with a TTL on the Job or a cluster policy")
    if a.get("on_finish_action") in ("keep_pod", "delete_succeeded_pod"):
        deg.append(f"on_finish_action={a['on_finish_action']!r}: failed Jobs are kept for inspection, as the operator keeps them")
    if a.get("get_logs") is False:
        deg.append("get_logs=False: execute_k8s_job always streams the pod logs into the run log")
    if a.get("in_cluster") is None and not a.get("config_file"):
        deg.append("neither in_cluster nor config_file is set: the default kubeconfig is used (Airflow falls back to in-cluster config when there is none)")
    if a.get("kubernetes_conn_id") not in (None, "kubernetes_default"):
        deg.append(f"kubernetes_conn_id={a['kubernetes_conn_id']!r}: the cluster comes from in_cluster/config_file/cluster_context or the default kubeconfig, not from that connection")
    if a.get("startup_timeout_seconds") not in (None, 120):
        deg.append(f"startup_timeout_seconds={a['startup_timeout_seconds']} is not applied")
    if a.get("active_deadline_seconds"):
        deg.append("active_deadline_seconds is applied as the step's wait timeout, not as the pod's own deadline")
    return stubs, deg


def _env_list(v: Any) -> list[dict[str, Any]]:
    """Env vars as a list of {name, value} (or {name, value_from}); Airflow keeps either a dict or a list of V1EnvVar."""
    v = k8s_obj(v)
    if not v:
        return []
    if isinstance(v, dict):
        return [{"name": k, "value": x} for k, x in v.items()]
    out: list[dict[str, Any]] = []
    for e in v:
        if not isinstance(e, dict) or "name" not in e:
            raise ValueError(f"env var {e!r} is not a name and value")
        out.append({k: x for k, x in e.items() if x is not None})
    return out


def _k8s_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    env = [(e["name"], e.get("value", "")) for e in _env_list(a.get("env_vars"))]
    pull = [(x["name"] if isinstance(x, dict) else x) for x in (k8s_obj(a.get("image_pull_secrets")) or [])]
    env_from = k8s_obj(a.get("env_from")) or []
    cfg = [e["config_map_ref"]["name"] for e in env_from if e.get("config_map_ref")] + list(a.get("configmaps") or [])
    sec = [e["secret_ref"]["name"] for e in env_from if e.get("secret_ref")]
    pod_spec = {k: k8s_obj(a.get(k)) for k in ("node_selector", "tolerations", "affinity") if not _empty(k8s_obj(a.get(k)))}
    kw = {
        "image": ex(a.get("image")), "command": ex(a.get("cmds") or None), "args": ex(a.get("arguments") or None), "namespace": ex(a.get("namespace") or None),
        "env_vars": ("[" + ", ".join(f"{n!r} + '=' + str({ex(v)})" for n, v in env) + "]") if env else "None",
        "image_pull_policy": repr(a.get("image_pull_policy") or None), "image_pull_secrets": repr([{"name": n} for n in pull] or None),
        "service_account_name": repr(a.get("service_account_name") or None), "env_config_maps": repr(cfg or None), "env_secrets": repr(sec or None),
        "volumes": repr(k8s_obj(a.get("volumes")) or None), "volume_mounts": repr(k8s_obj(a.get("volume_mounts")) or None),
        "labels": ex(a.get("labels") or None), "resources": repr(k8s_obj(a.get("container_resources")) or None),
        "pod_spec_config": repr(pod_spec or None),
        "load_incluster_config": repr(bool(a.get("in_cluster"))), "kubeconfig_file": repr(a.get("config_file") or None),
        "kube_context": repr(a.get("cluster_context") or None),
        "timeout": repr(int(a["active_deadline_seconds"]) if a.get("active_deadline_seconds") else None),
        "delete_failed_k8s_jobs": repr(a.get("on_finish_action") in (None, "delete_pod")),
    }
    return Prov("k8s_pod_run(context, " + ", ".join(f"{k}={v}" for k, v in kw.items()) + ")", "k8s", None, kinds_tag="kubernetes")


# ---------------------------------------------------------------------------------------------------------------------
# Email and Slack
# ---------------------------------------------------------------------------------------------------------------------
EMAIL_OPS = {"EmailOperator"}


def _email_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    stubs = []
    html = (t.get("template_files") or {}).get("html_content") or a.get("html_content")
    if not isinstance(html, str):
        stubs.append("html_content is not a string")
    if not _empty(a.get("files")) and not isinstance(a.get("files"), list):
        stubs.append("files is not a list")
    if not _empty(a.get("custom_headers")) and not _plain_dict(a.get("custom_headers")):
        stubs.append("custom_headers is not a plain dict")
    return stubs, []


def _email_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("conn_id") or "smtp_default"
    html = (t.get("template_files") or {}).get("html_content") or a.get("html_content")
    call = (f"send_email(context, {conn!r}, {ex(a.get('to'))}, {ex(a.get('subject'))}, {ex(html)}, {ex(a.get('files') or None)}, {ex(a.get('cc'))}, "
            f"{ex(a.get('bcc'))}, {ex(a.get('from_email'))}, {a.get('mime_subtype') or 'mixed'!r}, {a.get('mime_charset') or 'utf-8'!r}, {ex(a.get('custom_headers') or None)})")
    return Prov(call, "smtp", conn, kinds_tag="email")


SLACK_OPS = {"SlackWebhookOperator"}


def _slack_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    stubs = []
    if not _empty(a.get("proxy")):
        stubs.append("proxy is set")
    for k in ("blocks", "attachments"):
        if not _empty(a.get(k)) and not isinstance(a.get(k), (list, dict)):
            stubs.append(f"{k} is not plain JSON")
    return stubs, []


def _slack_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("slack_webhook_conn_id") or "slack_default"
    payload = {"text": a.get("message"), "channel": a.get("channel"), "username": a.get("username"), "icon_emoji": a.get("icon_emoji"),
               "icon_url": a.get("icon_url"), "attachments": a.get("attachments"), "blocks": a.get("blocks")}
    payload = {k: v for k, v in payload.items() if v is not None}
    return Prov(f"slack_webhook_post(context, {conn!r}, {ex(payload)}, {a.get('timeout')!r})", "slack", conn, kinds_tag="slack")


# ---------------------------------------------------------------------------------------------------------------------
# Services with a Dagster integration library
# ---------------------------------------------------------------------------------------------------------------------
def _dbt_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    stubs = [f"{k}=True is not supported" for k in ("reuse_existing_run", "retry_from_failure") if a.get(k)]
    deg = [UNVERIFIED.format(what="dbt Cloud account")]
    if a.get("account_id"):
        deg.append("account_id is taken from the connection's environment variable (<CONN>_ACCOUNT_ID), not from the operator argument")
    if a.get("deferrable"):
        deg.append("deferrable=True is ignored: the step polls")
    return stubs, deg


def _dbt_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("dbt_cloud_conn_id") or "dbt_cloud_default"
    extra = a.get("additional_run_config") or {}
    wait = a.get("wait_for_termination", True)
    call = (f"dbt_cloud_run(context, getattr(context.resources, {conn!r}), {ex(a.get('job_id'))}, {ex(a.get('trigger_reason'))}, {ex(a.get('steps_override'))}, "
            f"{ex(a.get('schema_override'))}, {ex(extra)}, wait={bool(wait)!r}, poll_interval={float(a.get('check_interval') or 60)!r}, "
            f"poll_timeout={float(a.get('timeout') or 604800)!r})")
    return Prov("return " + call, "dbt_cloud", conn, resource=True, kinds_tag="dbt", degraded=[])


def _airbyte_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    deg = [UNVERIFIED.format(what="Airbyte server")]
    if a.get("deferrable"):
        deg.append("deferrable=True is ignored: the step polls")
    return [], deg


def _airbyte_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("airbyte_conn_id") or "airbyte_default"
    call = (f"airbyte_sync(context, getattr(context.resources, {conn!r}), {ex(a.get('connection_id'))}, wait={not a.get('asynchronous')!r}, "
            f"poll_interval={float(a.get('wait_seconds') or 3)!r}, poll_timeout={float(a.get('timeout') or 3600)!r})")
    return Prov("return " + call, "airbyte", conn, resource=True, kinds_tag="airbyte")


def _fivetran_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    stubs = []
    if a.get("schedule_type") not in (None, "manual"):
        stubs.append(f"schedule_type={a['schedule_type']!r} (only the default, manual, is converted)")
    return stubs, [UNVERIFIED.format(what="Fivetran account")]


def _fivetran_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("fivetran_conn_id") or "fivetran"
    call = (f"fivetran_sync(context, getattr(context.resources, {conn!r}), {ex(a.get('connector_id'))}, "
            f"poll_interval={float(a.get('poll_frequency') or 15)!r}, poll_timeout={a.get('timeout_seconds')!r})")
    return Prov("return " + call, "fivetran", conn, resource=True, kinds_tag="fivetran")


def _dbx_check(t: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = _a(t)
    body = a.get("json") or {}
    stubs = []
    if not _plain_dict(body):
        stubs.append("json is not a plain dict")
    elif t["type"] == "DatabricksRunNowOperator":
        if body.get("job_name") and not body.get("job_id"):
            stubs.append("job_name needs a lookup of the job id, which is not converted (use job_id)")
        if not body.get("job_id"):
            stubs.append("no job_id")
    for k in ("repair_run", "cancel_previous_runs"):
        if a.get(k):
            stubs.append(f"{k}=True is not supported")
    deg = [UNVERIFIED.format(what="Databricks workspace")]
    if a.get("deferrable"):
        deg.append("deferrable=True is ignored: the step polls")
    return stubs, deg


def _dbx_build(t: dict[str, Any], ex: Callable[[Any], str]) -> Prov:
    a = _a(t)
    conn = a.get("databricks_conn_id") or "databricks_default"
    body = dict(a.get("json") or {})
    if t["type"] == "DatabricksRunNowOperator" and "job_id" in body:
        try:
            body["job_id"] = int(body["job_id"])          # Airflow stores the id as given; the API wants a number
        except (TypeError, ValueError):
            pass
    path = "/api/2.1/jobs/run-now" if t["type"] == "DatabricksRunNowOperator" else "/api/2.1/jobs/runs/submit"
    call = (f"databricks_run(context, getattr(context.resources, {conn!r}), {path!r}, {ex(body)}, wait={bool(a.get('wait_for_termination', True))!r}, "
            f"poll_interval={float(a.get('polling_period_seconds') or 30)!r})")
    return Prov("return " + call, "databricks", conn, resource=True, kinds_tag="databricks")


# ---------------------------------------------------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Handler:
    types: set[str]
    check: Callable[[dict[str, Any]], tuple[list[str], list[str]]]
    build: Callable[[dict[str, Any], Callable[[Any], str]], Prov]
    fields: Callable[[dict[str, Any]], Any]


def _tf(*names: str) -> Callable[[dict[str, Any]], Any]:
    return lambda t: {n: _a(t).get(n) for n in names}


HANDLERS: dict[str, Handler] = {
    "http": Handler({"HttpOperator", "SimpleHttpOperator", "HttpSensor"}, _http_check, _http_build, _tf("endpoint", "data", "headers", "request_params")),
    "sql": Handler(SQL_OPS - {"SQLExecuteQueryOperator"} | {"SqlQuery"}, _sql_check, _sql_build, lambda t: [sql_text(t), _a(t).get("parameters")]),
    "k8s": Handler({"KubernetesPodOperator"}, _k8s_check, _k8s_build, lambda t: _k8s_fields(t)),
    "email": Handler(EMAIL_OPS, _email_check, _email_build, _tf("to", "subject", "html_content", "files", "cc", "bcc", "from_email", "custom_headers")),
    "slack": Handler(SLACK_OPS, _slack_check, _slack_build, _tf("message", "channel", "username", "blocks", "attachments")),
    "dbt_cloud": Handler({"DbtCloudRunJobOperator"}, _dbt_check, _dbt_build, _tf("job_id", "trigger_reason", "steps_override", "schema_override", "additional_run_config")),
    "airbyte": Handler({"AirbyteTriggerSyncOperator"}, _airbyte_check, _airbyte_build, _tf("connection_id")),
    "fivetran": Handler({"FivetranOperator"}, _fivetran_check, _fivetran_build, _tf("connector_id")),
    "databricks": Handler({"DatabricksRunNowOperator", "DatabricksSubmitRunOperator"}, _dbx_check, _dbx_build, _tf("json")),
}


def _k8s_fields(t: dict[str, Any]) -> dict[str, Any]:
    a = _a(t)
    out = {k: a.get(k) for k in ("image", "cmds", "arguments", "namespace", "labels")}
    try:
        out["env_vars"] = _env_list(a.get("env_vars"))
    except ValueError:
        pass                                   # reported by _k8s_check
    return out


TYPES: set[str] = set().union(*(h.types for h in HANDLERS.values()))
ALIASES = {"SimpleHttpOperator": "HttpOperator"}
SENSOR_TYPES = {"HttpSensor"}


def handler_for(t: dict[str, Any]) -> Handler | None:
    for h in HANDLERS.values():
        if t["type"] in h.types:
            return h
    return None


# Dagster integration resources that resources.py defines per connection id. `env` lists the variables the generated code reads.
RESOURCE_KINDS: dict[str, dict[str, Any]] = {
    "dbt_cloud": {"import": "from dagster_dbt import DbtCloudClientResource",
                  "ctor": lambda c: f"DbtCloudClientResource(auth_token=dg.EnvVar('{envname(c)}_API_TOKEN'), account_id=dg.EnvVar.int('{envname(c)}_ACCOUNT_ID'), "
                                    "disable_schedule_on_trigger=False)",
                  "env": lambda c: [f"{envname(c)}_API_TOKEN", f"{envname(c)}_ACCOUNT_ID"]},
    "airbyte": {"import": "from dagster_airbyte import AirbyteResource",
                "ctor": lambda c: (f"AirbyteResource(host=dg.EnvVar('{envname(c)}_HOST'), port=dg.EnvVar('{envname(c)}_PORT'), "
                                   f"username=dg.EnvVar('{envname(c)}_USER'), password=dg.EnvVar('{envname(c)}_PASSWORD'))"),
                "env": lambda c: [f"{envname(c)}_{s}" for s in ("HOST", "PORT", "USER", "PASSWORD")]},
    "fivetran": {"import": "from dagster_fivetran import FivetranResource",
                 "ctor": lambda c: f"FivetranResource(api_key=dg.EnvVar('{envname(c)}_API_KEY'), api_secret=dg.EnvVar('{envname(c)}_API_SECRET'))",
                 "env": lambda c: [f"{envname(c)}_API_KEY", f"{envname(c)}_API_SECRET"]},
    "databricks": {"import": "from dagster_databricks import DatabricksClientResource",
                   "ctor": lambda c: f"DatabricksClientResource(host=dg.EnvVar('{envname(c)}_HOST'), token=dg.EnvVar('{envname(c)}_TOKEN'))",
                   "env": lambda c: [f"{envname(c)}_HOST", f"{envname(c)}_TOKEN"]},
}
# Environment variables of the kinds that read them inside the step (no resource).
ENV_KINDS: dict[str, Callable[[str], list[str]]] = {
    "http": lambda c: [f"{envname(c)}_BASE_URL", f"{envname(c)}_AUTH_HEADER (optional)", f"{envname(c)}_LOGIN and {envname(c)}_PASSWORD (optional)",
                       f"{envname(c)}_HEADERS (optional JSON)"],
    "sql": lambda c: [f"{envname(c)}_URL (a SQLAlchemy URL, e.g. postgresql+psycopg2://user:password@host/db)"],
    "smtp": lambda c: [f"{envname(c)}_HOST", f"{envname(c)}_PORT (default 25)", f"{envname(c)}_USER and {envname(c)}_PASSWORD (optional)",
                       f"{envname(c)}_STARTTLS / {envname(c)}_SSL (1 to enable)", f"{envname(c)}_FROM (default sender)"],
    "slack": lambda c: [f"{envname(c)}_WEBHOOK_URL"],
}
REQUIREMENTS: dict[str, list[str]] = {
    "http": ["requests"], "sql": ["sqlalchemy"], "k8s": ["dagster-k8s"], "smtp": [], "slack": ["requests"],
    "dbt_cloud": ["dagster-dbt"], "airbyte": ["dagster-airbyte"], "fivetran": ["dagster-fivetran"], "databricks": ["dagster-databricks"],
}


PROVIDER_RUNTIME = '''

# ---- provider operators (provider_ops.py) -----------------------------------------------------------------------------------
import re as _re


def _envname(conn_id):
    return _re.sub(r"\\W", "_", conn_id).upper()


def _need_env(name, what):
    value = os.environ.get(name)
    if not value:
        raise dg.Failure(description=f"{what}: set the environment variable {name} (the Airflow connection's value is not copied by the conversion)")
    return value


def _http_session(conn_id):
    import requests
    e = _envname(conn_id)
    session = requests.Session()
    if os.environ.get(e + "_LOGIN"):
        session.auth = (os.environ[e + "_LOGIN"], os.environ.get(e + "_PASSWORD", ""))
    if os.environ.get(e + "_AUTH_HEADER"):
        session.headers["Authorization"] = os.environ[e + "_AUTH_HEADER"]
    if os.environ.get(e + "_HEADERS"):
        session.headers.update(json.loads(os.environ[e + "_HEADERS"]))
    return session


def _http_url(conn_id, endpoint):
    """HttpHook.url_from_endpoint: the connection's base URL and the endpoint joined with exactly one slash."""
    base = _need_env(_envname(conn_id) + "_BASE_URL", f"HTTP connection {conn_id}")
    if base and not base.endswith("/") and endpoint and not endpoint.startswith("/"):
        return base + "/" + endpoint
    if base and base.endswith("/") and endpoint and endpoint.startswith("/"):
        return base + endpoint[1:]
    return (base or "") + (endpoint or "")


def _http_send(conn_id, method, endpoint, data, headers, options):
    """HttpHook.run: GET sends the data as query parameters, other methods as the body; HTTP errors fail unless check_response is false."""
    import requests
    session = _http_session(conn_id)
    url = _http_url(conn_id, endpoint)
    method = method.upper()
    if method == "GET":
        req = requests.Request(method, url, params=data, headers=headers)
    elif method == "HEAD":
        req = requests.Request(method, url, headers=headers)
    else:
        req = requests.Request(method, url, data=data, headers=headers)
    prepped = session.prepare_request(req)
    settings = session.merge_environment_settings(prepped.url, proxies={}, stream=None, verify=options.get("verify"), cert=None)
    response = session.send(prepped, timeout=options.get("timeout"), allow_redirects=options.get("allow_redirects", True), **settings)
    if options.get("check_response", True):
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            raise dg.Failure(description=f"{response.status_code}:{response.reason}", metadata={"url": url, "body": response.text[:2000]})
    return response


def http_run(context, conn_id, method, endpoint, data=None, headers=None, options=None, log_response=False):
    """HttpOperator.execute: the response body is the task's value."""
    response = _http_send(conn_id, method, endpoint, data, headers, options or {})
    if log_response:
        context.log.info(response.text)
    return response.text


def http_resource_call(resource, method, endpoint, data=None, headers=None, options=None, log=None):
    """HttpOperator.execute through the registry's http_resource (`resource.request`): the connection's base URL and auth live in the resource.
    A GET sends dict data as the query string (`params`), other methods send a form body (a dict) or the text as given; the body text is returned."""
    import urllib.parse
    options = options or {}
    method = method.upper()
    path, params, body, hdrs = endpoint or "", None, None, dict(headers or {})
    if method == "GET":
        if isinstance(data, dict):
            params = data
        elif data:
            path += ("&" if "?" in path else "?") + (urllib.parse.urlencode(data) if isinstance(data, list) else str(data))
    elif method != "HEAD" and data is not None:
        if isinstance(data, (dict, list)):
            body = urllib.parse.urlencode(data)
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data
    result = resource.request(method, path, headers=hdrs or None, params=params, body=body, timeout_seconds=options.get("timeout"), raise_on_failure=options.get("check_response", True))
    if log is not None:
        log.info(result.body)
    return result.body


def http_poke(conn_id, method, endpoint, params, headers, options, allowed_codes):
    """HttpSensor.poke without a response_check: true once the request succeeds; the allowed error codes (404 by default) mean not yet."""
    try:
        _http_send(conn_id, method, endpoint, params, headers, options or {})
    except dg.Failure as e:
        if str(e.description).startswith(tuple(allowed_codes)):
            return False
        raise
    return True


def _split_sql(text):
    try:
        import sqlparse
    except ImportError:
        raise dg.Failure(description="split_statements=True needs the sqlparse package (pip install sqlparse)")
    return [s for s in sqlparse.split(text) if s.strip()]


def sql_run(context, conn_id, statements, parameters=None, autocommit=False, split=False):
    """DbApiHook.run through a SQLAlchemy URL: every statement gets the parameters; one transaction committed at the end, or each statement committed when
    autocommit is set. Returns the rows (as lists) of the last statement that returned any, which is what the operator pushes."""
    from sqlalchemy import create_engine
    url = _need_env(_envname(conn_id) + "_URL", f"SQL connection {conn_id}")
    stmts = []
    for s in statements:
        stmts += _split_sql(s) if split else [s]
    engine = create_engine(url)
    rows = None
    try:
        with engine.connect() as conn:
            for s in stmts:
                context.log.info(f"Running statement: {s[:500]}")
                cur = conn.exec_driver_sql(s, parameters) if parameters else conn.exec_driver_sql(s)
                if cur.returns_rows:
                    rows = [list(r) for r in cur.fetchall()]
                if autocommit:
                    conn.commit()
            conn.commit()
    finally:
        engine.dispose()
    return rows


def _addresses(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in _re.split(r"[,;]", v) if x.strip()]
    return list(v)


def send_email(context, conn_id, to, subject, html, files, cc, bcc, from_email, mime_subtype, mime_charset, custom_headers):
    """EmailOperator through smtplib (airflow.utils.email.send_email_smtp): one HTML part, files attached by name, Bcc kept out of the headers."""
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formatdate
    e = _envname(conn_id)
    host = _need_env(e + "_HOST", f"SMTP connection {conn_id}")
    sender = from_email or _need_env(e + "_FROM", f"SMTP connection {conn_id} (no from_email on the operator)")
    to, cc, bcc = _addresses(to), _addresses(cc), _addresses(bcc)
    msg = MIMEMultipart(mime_subtype)
    msg["Subject"], msg["From"], msg["To"], msg["Date"] = subject, sender, ", ".join(to), formatdate(localtime=True)
    if cc:
        msg["Cc"] = ", ".join(cc)
    for k, v in (custom_headers or {}).items():
        msg[k] = v
    msg.attach(MIMEText(html, "html", mime_charset))
    for path in files or []:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(path))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
        msg.attach(part)
    port = int(os.environ.get(e + "_PORT") or 25)
    on = lambda name: os.environ.get(name, "").lower() in ("1", "true", "yes")
    smtp = (smtplib.SMTP_SSL if on(e + "_SSL") else smtplib.SMTP)(host, port)
    try:
        if on(e + "_STARTTLS"):
            smtp.starttls()
        if os.environ.get(e + "_USER"):
            smtp.login(os.environ[e + "_USER"], os.environ.get(e + "_PASSWORD", ""))
        smtp.sendmail(sender, to + cc + bcc, msg.as_string())
    finally:
        smtp.quit()
    context.log.info(f"Sent an email to {len(to + cc + bcc)} recipient(s)")


def slack_webhook_post(context, conn_id, payload, timeout=None):
    """SlackWebhookOperator: POST the message as JSON to the webhook URL; any other status than 200 fails."""
    import requests
    url = _need_env(_envname(conn_id) + "_WEBHOOK_URL", f"Slack webhook connection {conn_id}")
    resp = requests.post(url, json=payload, timeout=timeout or 30)
    if resp.status_code != 200:
        raise dg.Failure(description=f"Slack webhook returned {resp.status_code}", metadata={"body": resp.text[:500]})


def k8s_pod_run(context, *, kube_context=None, **kw):
    """KubernetesPodOperator through dagster-k8s: run the image as a Kubernetes Job, wait for it, stream its logs into the run log.
    (PipesK8sClient needs a container that speaks the Pipes protocol; a pod operator's image usually does not.)"""
    from dagster_k8s import execute_k8s_job
    kw = {k: v for k, v in kw.items() if v is not None}
    if kube_context is not None:
        kw["_kubeconfig_file_context"] = kube_context
    execute_k8s_job(context=context, **kw)


def dbt_cloud_run(context, resource, job_id, cause, steps_override, schema_override, extra, wait=True, poll_interval=60.0, poll_timeout=None):
    """DbtCloudRunJobOperator through dagster-dbt's client. The resource is built with disable_schedule_on_trigger=False: the operator never touches the job's schedule."""
    client = resource.get_dbt_client()
    body = {k: v for k, v in {"cause": cause or "Triggered via Dagster", "steps_override": steps_override, "schema_override": schema_override}.items() if v is not None}
    body.update(extra or {})
    job_id = int(job_id)
    if not wait:
        return client.run_job(job_id, **body)["id"]
    run = client.run_job(job_id, **body)
    client.poll_run(run["id"], poll_interval=poll_interval, poll_timeout=poll_timeout)
    return run["id"]


def airbyte_sync(context, resource, connection_id, wait=True, poll_interval=3.0, poll_timeout=3600.0):
    """AirbyteTriggerSyncOperator through dagster-airbyte: start the sync, poll until it ends (unless asynchronous); the job id is the value."""
    if not wait:
        return resource.start_sync(connection_id)["job"]["id"]
    return resource.sync_and_poll(connection_id, poll_interval=poll_interval, poll_timeout=poll_timeout).job_details["job"]["id"]


def fivetran_sync(context, resource, connector_id, poll_interval=15.0, poll_timeout=None):
    """FivetranOperator through dagster-fivetran: sync the connector and poll until it ends (the resource sets the connector to manual scheduling, as the operator does)."""
    resource.sync_and_poll(connector_id, poll_interval=poll_interval, poll_timeout=poll_timeout)


def af_client(resource):
    """What a step gets for `context.resources.<key>` is not always the configured resource: dagster-databricks, dagster-gcp (GCS) and dagster-aws hand the step the CLIENT they build
    (a `DatabricksClient`, a `storage.Client`, a boto3 client) and only a plain resource (dagster-slack) still has `get_client()`. Use `get_client()` when it is there, else the object itself."""
    return resource.get_client() if hasattr(resource, "get_client") else resource


def af_bq_client(resource):
    """A context manager for a BigQuery client from whatever the step was given: dagster-gcp's BigQueryResource attaches a GENERATOR that yields the open client (close it to release the
    client), a plain resource has the `get_client()` context manager, and a bare client is used as it is."""
    import contextlib
    import inspect

    @contextlib.contextmanager
    def cm():
        if inspect.isgenerator(resource):
            client = next(resource)
            try:
                yield client
            finally:
                resource.close()
        elif hasattr(resource, "get_client"):
            with resource.get_client() as client:
                yield client
        else:
            yield resource
    return cm()


def databricks_run(context, resource, path, body, wait=True, poll_interval=30.0):
    """DatabricksRunNowOperator / DatabricksSubmitRunOperator: POST the same JSON body to the Jobs API, then wait for the run to finish; the run id is the value."""
    client = af_client(resource)
    run_id = client.workspace_client.api_client.do("POST", path, body=body)["run_id"]
    context.log.info(f"Databricks run {run_id} started")
    if wait:
        client.wait_for_run_to_complete(logger=context.log, databricks_run_id=run_id, poll_interval_sec=poll_interval, max_wait_time_sec=604800, verbose_logs=True)
    return run_id
'''
