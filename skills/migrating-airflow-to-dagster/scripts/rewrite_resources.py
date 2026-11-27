#!/usr/bin/env python3
"""The rewrite prompt's resource-first guidance: which service-calling operator families a DAG's spec uses, the compact registry-resource table for ONLY those families, and the registry-search checklist.

    families(spec)        -> ordered family ids the spec's tasks use (`http`, `smtp`, `slack`, ...), from operator class names and (for callables) the Airflow hooks they name
    resource_table(fams)  -> markdown: one compact table row per family (resource id, call, key config, known gap); '' when none
    checklist(spec)       -> [(stage label, candidate ids seen in earlier registry searches)] : one line per stage the worker must search and record
    RESOURCE_RULE         -> the short resource-first rule (always in the prompt)

Source of the rows: `reference/registry-resources-0.10.194.md` (signatures printed from the 0.10.194 classes; behaviours run offline, see `component-wishlist.md`). The rows here are abbreviated;
the full signature is `dagster-component info <id>` / `schema`. `tests/test_rewrite_resource_first.py` checks that every id here appears in that document. A resource id that is absent from the
worker's installed registry is a fall-back-to-plain-Python case, said in the decision record.

Principle (the user's): an asset is a data product; a service call is an op or plain Python INSIDE it; a service-calling operator is a RESOURCE the asset uses, not an asset of its own. Registry components
are for jobs, lifecycle and transfers; prefer a registry resource to a hand-rolled client.
"""
from __future__ import annotations

import re
from typing import Any

REGISTRY_VERSION = "0.10.194"

RESOURCE_RULE = """**Service calls are resources, not assets (resource-first).** An asset is a data product; a service-calling operator (HTTP, email, Slack, SSH, Lambda, SQL by URL, Databricks, Spark, EMR, Dataproc, Pub/Sub, Athena, Redshift, BigQuery DDL, bucket
lifecycle, dbt command, dbt Cloud, Airbyte) becomes a REGISTRY RESOURCE configured once (`dagster-component add <resource id>`, fields from its schema, env var NAMES only) and CALLED INSIDE the asset or op that
needs it, as a `ConfigurableResource` parameter. Do not write one asset per operator and do not hand-roll a client when the registry has the resource. Registry version {version}; your installed templates may be older: before
using a resource run `dagster-component info <id>` (or search); if it is absent or lacks the method, fall back to plain Python (an official integration, then `requests`/`boto3`/...) and write `fallback to plain Python: <reason>`
in the registry-search lines of the header (below). A call's gap in the table is a fallback: say so, do not hide it."""

# family -> dict(name, ops (operator class regex), hook (hook name regex found in callable source), resource, call, config, gap, no_map)
FAMILIES: dict[str, dict[str, Any]] = {
    "http": dict(label="HTTP", ops=r"^(Simple)?HttpOperator$|^HttpSensor$", hook=r"\bHttpHook\b", resource="http_resource",
                 call="`request(method, path, *, headers, params, body, json_body, success_status_codes, response_json_path, timeout_seconds, raise_on_failure=True)` -> result (`.success`); `poke(method, path, *, params, success_status_codes, response_check) -> bool`; `get`/`post`",
                 config="`base_url` (literal, required); `auth_type` none/basic/bearer/oauth2_client_credentials with `auth_*_env_var` names; `timeout_seconds` 30; `retries` 0",
                 gap="`poke` returns False (never raises) on a connection error or non-success status; `response_check` is a Python callable"),
    "smtp": dict(label="SMTP / Email", ops=r"^EmailOperator$|^SmtpOperator$", hook=r"\bSmtpHook\b|\bsend_email\b", resource="smtp_resource",
                 call="`send_email(to: str, subject, body, from_addr='', cc, bcc, html_body, reply_to, attachments=[(name, bytes, mime)], timeout_seconds=30)`",
                 config="`host` (required); `port` 587; `use_tls` true; `username` + `password_env_var` (both or neither)",
                 gap="`to` is ONE string; `from_addr` defaults to the username (empty without a login)"),
    "slack": dict(label="Slack", ops=r"^Slack\w*Operator$", hook=r"\bSlack\w*Hook\b", resource="slack_resource",
                  call="`post_message(channel=None, text=None, blocks=None, webhook_url=None, **kw) -> dict`; `get_client()` (token only)",
                  config="`token_env_var` (bot token) or `webhook_url_env_var`; at least one",
                  gap="`channel` required on the token path, ignored on the webhook path; token path needs `slack-sdk`"),
    "ssh": dict(label="SSH", ops=r"^SSHOperator$", hook=r"\bSSHHook\b", resource="ssh_resource",
                call="`run_command(command, timeout=None, environment=None, working_dir=None) -> {stdout, stderr, exit_code}` (no raise on non-zero exit; one connection per call)",
                config="`host_env_var`, `username_env_var` (required); auth `private_key_path_env_var` | `private_key_env_var` | `password_env_var`; `known_hosts_check` false",
                gap="key files load as RSA only (Ed25519/ECDSA as PEM text); `working_dir` unquoted; one command per call"),
    "lambda": dict(label="AWS Lambda", ops=r"^LambdaInvokeFunctionOperator$", hook=r"\bLambdaHook\b", resource="lambda_resource",
                   call="`invoke(function_name, payload: dict=None, invocation_type='RequestResponse', qualifier=None, include_logs=False, fail_on_function_error=True) -> {status_code, function_error, payload, log_tail}`",
                   config="all optional: `region_name`, `profile_name`, `aws_*_env_var` (default: boto3 chain)",
                   gap="`payload` must be a dict; raises `RuntimeError` on a function error unless `fail_on_function_error=False`"),
    "sql": dict(label="SQL by URL", ops=r"^SQLExecuteQueryOperator$|^(Postgres|MySql|MsSql|Sqlite|Oracle|Vertica)Operator$", hook=r"\b(Postgres|MySql|MsSql|Sqlite|Oracle|Vertica)Hook\b|\bDbApiHook\b", resource="sql_url_resource",
                call="`execute(query: str, params: dict=None) -> list[dict]` (DDL/DML commits, returns `[]`); `get_engine()`",
                config="`connection_url_env_var` (name of a var holding a SQLAlchemy URL) XOR `connection_url`; the dialect's driver installed",
                gap="ONE statement per call; named params only; `split_statements`, positional params or last-statement rows: use `sql_asset` instead"),
    "databricks": dict(label="Databricks", ops=r"^Databricks\w*Operator$", hook=r"\bDatabricks\w*Hook\b", resource="databricks_jobs_resource",
                       call="`run_now_and_wait(job_id | job_name, job_parameters, notebook_params, ..., poll_timeout_seconds=3600, fail_on_non_success=True)`; `submit_run_and_wait(run_json, ...)`; `repair_run_and_wait(run_id, ...)` -> dict (`run_id`, `result_state`, ...)",
                       config="`workspace_host` (literal), `workspace_token_env_var` (required); needs `databricks-sdk`",
                       gap="exactly one of `job_id`/`job_name`; `run_json` is the Jobs API submit-run body; no poll interval, only a timeout"),
    "spark": dict(label="Spark submit", ops=r"^SparkSubmitOperator$", hook=r"\bSparkSubmitHook\b", resource="spark_submit_resource",
                  call="`submit(application, master, deploy_mode, class_name, name, conf, packages, jars, py_files, application_args, env_vars, timeout_seconds, ...) -> {exit_code, command, ...}`",
                  config="`spark_submit_binary` (default `spark-submit`)",
                  gap="no `keytab`/`principal`/`proxy_user` (use `extra_args`); master comes from the Airflow connection; runs a subprocess on the code location host"),
    "emr": dict(label="EMR", ops=r"^Emr\w*Operator$", hook=r"\bEmr\w*Hook\b", resource="emr_resource",
                call="`submit_step_and_wait(cluster_id | cluster_name, step_name, jar, main_class, args, ...)`; `create_cluster_and_wait(name, instances, release_label, applications, ..., extra_job_flow_args)`; `terminate_cluster(cluster_id | cluster_name)`",
                config="all optional: `region_name`, `profile_name`, `aws_*_env_var`",
                gap="the Airflow `job_flow_overrides` dict maps onto `instances` + `extra_job_flow_args`; no mapping written yet"),
    "dataproc": dict(label="Dataproc", ops=r"^Dataproc\w*Operator$", hook=r"\bDataproc\w*Hook\b", resource="dataproc_resource",
                     call="`ensure_cluster(project_id, region, cluster_name, cluster_config)`; `delete_cluster(...)`; `submit_job_and_wait(project_id, region, cluster_name, job_type, job_config)`; `create_batch_and_wait(project_id, region, batch_id, batch_config)`",
                     config="`gcp_credentials_env_var` (optional; default ADC); needs `google-cloud-dataproc`", gap="none known; no mapping written yet"),
    "pubsub": dict(label="Pub/Sub", ops=r"^PubSub\w*Operator$", hook=r"\bPubSubHook\b", resource="pubsub_resource",
                   call="`publish(project_id, topic_id, data, attributes, ordering_key) -> message id`; `ensure_topic`; `ensure_subscription`; `delete_topic`; `delete_subscription`",
                   config="`credentials_env_var` (optional)", gap="none known; no mapping written yet"),
    "athena": dict(label="Athena", ops=r"^AthenaOperator$", hook=r"\bAthenaHook\b", resource="athena_resource",
                   call="`run_query_and_wait(query, database, output_location, workgroup, poll_interval_seconds, timeout_seconds) -> dict`",
                   config="`region_name` (required); `aws_profile`", gap="no mapping written yet"),
    "redshift": dict(label="Redshift Data API", ops=r"^RedshiftDataOperator$", hook=r"\bRedshiftDataHook\b", resource="redshift_data_api_resource",
                     call="`execute_and_wait(sql, database, cluster_identifier, db_user, workgroup_name, secret_arn, ...) -> dict`",
                     config="`region_name` (required); `aws_profile`", gap="no mapping written yet"),
    "bigquery_ddl": dict(label="BigQuery DDL / value check", ops=r"^BigQuery(Create|Delete)(Empty)?(Dataset|Table)\w*Operator$|^BigQueryValueCheckOperator$", hook=r"$^", resource="bigquery_ddl_resource",
                         call="`ensure_dataset`/`delete_dataset`/`recreate_dataset(dataset_id, ...)`; `ensure_table`/`delete_table`/`recreate_table(dataset_id, table_id, table_schema, ...)`; `run_value_check(query, expected_value, comparison='equal', tolerance)` -> dict",
                         config="`credentials` (inline dict) or `credentials_path`, `project_id`, `location`; needs `google-cloud-bigquery`",
                         gap="credentials are a dict or path, not an env-var field; no mapping written yet"),
    "bucket": dict(label="Bucket lifecycle (S3, GCS)", ops=r"^(S3|GCS)(Create|Delete)BucketOperator$", hook=r"$^", resource="s3_bucket_resource` / `gcs_bucket_resource",
                   call="S3 `create_bucket`/`ensure_bucket`/`delete_bucket(bucket_name, force=False, region=None)`; GCS `create_bucket`/`ensure_bucket(bucket_name, location='US', storage_class='STANDARD', project=None)`/`delete_bucket`",
                   config="S3: `region_name`, `profile_name`, `aws_*_env_var`, `endpoint_url`; GCS: `credentials_env_var` (all optional)",
                   gap="no GCS OBJECT resource (upload/download/list/delete/exists stay on `dagster-gcp` `GCSResource`)"),
    "dbt_command": dict(label="dbt command (per-task operators)", ops=r"^Dbt(Run|Build|Test|Seed|Snapshot|Source|Ls)\w*Operator$", hook=r"$^", resource="dbt_command_resource",
                        call="`run(command='run'|'build'|'test'|'snapshot'|'seed'|'source', select, exclude, full_refresh, target, vars, extra_args, cwd, env_vars, timeout_seconds, dry_run) -> dict` (counts from `run_results.json`; raises `dg.Failure` on non-zero exit)",
                        config="`project_dir` (required), `profiles_dir`; `dbt` on PATH",
                        gap="not a dbt asset graph (use `DbtProjectComponent` for models as assets); a Cosmos `profile_mapping` has no registry counterpart"),
    "dbt_cloud": dict(label="dbt Cloud", ops=r"^DbtCloud\w*Operator$", hook=r"\bDbtCloudHook\b", resource="dbt_cloud_resource` + `dbt_cloud_trigger_job",
                      call="resource wraps `dagster_dbt.DbtCloudResource` (`run_job_and_poll(job_id, poll_interval, poll_timeout)`); the job component (asset form) takes `dbt_cloud_job_id`, `dbt_cloud_resource_key`, `wait_for_completion` true",
                      config="resource: `auth_token_env_var`, `account_id` (both required)",
                      gap="**the resource disables the job's cron schedule on every trigger** (no field to stop it; Airflow never does): do not map it silently, use plain Python/`requests` and say so, or declare the difference"),
    "airbyte": dict(label="Airbyte", ops=r"^AirbyteTriggerSyncOperator$", hook=r"\bAirbyteHook\b", resource="airbyte_sync_trigger_job",
                    call="no method: a component in asset form (`asset_name`, `deps`, `group_name`); posts `/api/v1/connections/sync`, polls `/api/v1/jobs/get`",
                    config="`airbyte_url`, `connection_id` (required); `username_env`, `password_env`; `wait_for_completion` false; `timeout_seconds` 3600",
                    gap="self-hosted `/api/v1/` basic auth only; waiting is OFF by default (Airflow waits): set `wait_for_completion: true`"),
    "fivetran": dict(label="Fivetran", ops=r"^FivetranOperator$", hook=r"\bFivetranHook\b", resource="fivetran_sync_trigger_job",
                     call="DO NOT USE YET", config="-",
                     gap="**do not map yet**: the component posts to `/v1/connectors/{id}/force`, current Fivetran docs describe `/v1/connections/{id}/sync` (endpoint mismatch, live API not called): use plain Python and say so in the registry-search lines", no_map=True),
}

# Candidate ids seen in earlier registry searches (stage 2a worker reports): a starting point, always confirmed with `dagster-component info <id>`.
STAGE_HINTS = {
    "BashOperator": ("shell/bash command", ["shell_command_asset"]),
    "_BashDecoratedOperator": ("shell/bash command", ["shell_command_asset"]),
    "branching": ("branch / short circuit", ["conditional_asset"]),
    "short_circuit": ("branch / short circuit", ["conditional_asset"]),
    "mapped_tasks": ("dynamic mapping / fan out", ["dynamic_fanout_asset"]),
    "sensors": ("wait / sensor", ["wait_until_asset"]),
    "venv_pipes": ("own Python environment", ["pipes_python_asset"]),
    "S3CreateObjectOperator": ("write an S3 object", ["s3_create_object_asset"]),
    "TimeDeltaSensor": ("wait / sensor", ["wait_until_asset"]),
}
TRIVIAL_OPS = {"EmptyOperator", "DummyOperator"}
PYTHON_OPS = re.compile(r"Python|Branch|ShortCircuit|Decorated|^_Asset|External|Virtualenv")      # generic Python callables: covered by the patterns and the hand-written line


def _text(t: dict[str, Any]) -> str:
    c = t.get("callable") or {}
    return (c.get("source") or "") + " " + " ".join(str(v) for v in (t.get("callable_arg_sources") or {}).values())


def families(spec: dict[str, Any]) -> list[str]:
    """Ordered family ids the spec uses: by operator class, or a hook named in a callable's source."""
    found = []
    for fid, f in FAMILIES.items():
        for t in spec.get("tasks") or []:
            if re.search(f["ops"], t.get("operator") or "") or re.search(f["hook"], _text(t)):
                found.append(fid)
                break
    return found


def resource_table(fams: list[str]) -> str:
    """The compact table for exactly these families ('' when none)."""
    if not fams:
        return ""
    L = [f"### Registry resources for this DAG's service operators (registry {REGISTRY_VERSION}; abbreviated: `dagster-component info <id>` has the full signature)", "",
         "| Operator family | Resource id (`dagster-component add <id>` writes its `defs.yaml`) | Call inside the asset or op | Key config (env var NAMES) | Gap / warning |", "|---|---|---|---|---|"]
    for fid in fams:
        f = FAMILIES[fid]
        L.append(f"| {f['label']} | `{f['resource']}` | {f['call']} | {f['config']} | {f['gap']} |")
    L += ["", "A gap is a fall-back-to-plain-Python case: write it in the registry-search lines. Map each operator task to the asset that calls the resource (several service tasks that feed one product share that asset as ops), keep the task's own state and retries."]
    return "\n".join(L)


def stages(spec: dict[str, Any], pats: list[str]) -> list[tuple[str, list[str]]]:
    """The stages the worker must search, one line each: service families, then the other non-trivial operators and detected patterns, with candidate ids seen before."""
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    def add(label: str, ids: list[str]) -> None:
        if label not in seen:
            seen.add(label)
            out.append((label, ids))
    for fid in families(spec):
        f = FAMILIES[fid]
        add(f"{f['label']} (resource)", [f["resource"].replace("`", "").split(" / ")[0].split(" + ")[0]] if not f.get("no_map") else [])
    for t in spec.get("tasks") or []:
        op = t.get("operator") or ""
        if op in STAGE_HINTS:
            add(*STAGE_HINTS[op])
        elif not (op in TRIVIAL_OPS or PYTHON_OPS.search(op) or any(re.search(f["ops"], op) for f in FAMILIES.values())):
            add(op, [])
    for p in pats:
        if p in STAGE_HINTS:
            add(*STAGE_HINTS[p])
    return out


def checklist_text(spec: dict[str, Any], pats: list[str]) -> str:
    st = stages(spec, pats)
    L = ["**Registry search lines (a separate paragraph in the header, after the decision record and a blank line; one line per stage, filled in; searching is cheap: `dagster-component search \"<terms>\" --json`, then `info <id>` for a hit; candidate ids were seen in earlier registry searches and may be absent from your install).** "
         "Form: `- <stage>: searched \"<terms>\": found <id> | not found; used | not used: <reason>` (`fallback to plain Python: <reason>` where a resource or component is absent or has a gap). Stages for this DAG:"]
    for label, ids in st:
        L.append(f"- {label}" + (f" (candidate {', '.join('`' + i + '`' for i in ids)}; confirm with `info`)" if ids else ""))
    L.append("- python callables / glue that no registry component fits: say `searched \"<terms>\": not found; hand-written`")
    return "\n".join(L)
