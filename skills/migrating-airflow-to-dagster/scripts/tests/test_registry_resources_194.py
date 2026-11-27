"""Resource-first `--output components` for the 0.10.194 resources (registry_map.py): each family is converted from an IR, the generated project is LOADED by dagster's own component loader against
registry 0.10.194, and the asset is RUN against a local stand-in (HTTP server, SMTP server, SQLite, a stand-in `spark-submit`) or a recording fake / botocore stubber of the client library the
resource builds (paramiko, slack_sdk, databricks-sdk, google-cloud-*; none of those packages is installed in the test interpreter). The registry package is read from EVAL_REGISTRY_194 (default:
../reg194 next to the interpreter's venv); the tests skip when it is absent.

What is checked per family: the resource id and YAML attributes (environment variable NAMES only, never a value), the plain call in the generated step, the arguments that reach the client, the
requirements and report fields, and that an argument the resource cannot carry keeps the plain-Python shim (or the Python route) with a caveat that names the gap."""
import io
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import registry_map as RM  # noqa: E402
from test_provider_ops import op, ir_of, web, smtp  # noqa: E402,F401
from test_registry_resources import _convert, _load, _run, reg194, fake_google_api_core  # noqa: E402,F401

dg = pytest.importorskip("dagster")


def _yml(tmp_path, pkg):
    return (tmp_path / pkg / "defs" / "registry_resources" / "defs.yaml").read_text()


def _reason(rep, needle):
    return [c["reason"] for c in rep["caveats"] if needle in c["reason"]]


def _steps(tmp_path, pkg, dag="d1"):
    return (tmp_path / pkg / "defs" / f"{dag}_steps.py").read_text()


def _fake_module(mp, name, **attrs):
    """A module in sys.modules (with every missing parent), for client libraries that are not installed."""
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        n = ".".join(parts[:i])
        if n in sys.modules:
            continue
        m = types.ModuleType(n)
        m.__path__ = []
        mp.setitem(sys.modules, n, m)
        if i > 1:
            setattr(sys.modules[".".join(parts[:i - 1])], parts[i - 1], m)
    for k, v in attrs.items():
        setattr(sys.modules[name], k, v)
    return sys.modules[name]


def _mod(path):
    import importlib
    return importlib.import_module(f"dagster_community_components.resources.{path}.component")


def _stubbed(service, region="us-east-1"):
    import boto3
    from botocore.stub import Stubber
    client = boto3.client(service, region_name=region, aws_access_key_id="x", aws_secret_access_key="y")
    return client, Stubber(client)


def test_the_mapping_table_and_resource_ids_name_real_components_of_0_10_194(reg194):
    import dagster_community_components as reg
    assert RM.VERIFIED_IN == "0.10.194" and RM.DCC_REQUIREMENT == "dagster-community-components>=0.10.194"
    for typ, rid in RM.RESOURCE_TYPE_IDS.items():
        assert hasattr(reg, typ), (typ, rid)
    uses = " ".join(r["uses"] for r in RM.OPERATOR_MAP)
    for rid in RM.RESOURCE_TYPE_IDS.values():
        assert rid in uses.replace(" (SlackPostResource)", ""), rid                 # every emitted resource is a row of the table
    assert {r["shape"] for r in RM.OPERATOR_MAP} == {"asset", "resource", "plain"}
    for r in RM.OPERATOR_MAP:
        if r["shape"] == "resource" and r["uses"].split()[0] in RM.RESOURCE_TYPE_IDS.values():
            assert r.get("run", "").startswith("RUN"), r                           # a family is on the table as RUN only; READ-only families are not emitted
    # the families the lead excluded are plain on purpose, with the reason on the row
    plain = {r["operator"].split(",")[0]: r["why"] for r in RM.OPERATOR_MAP if r["shape"] == "plain"}
    assert "disable_schedule_on_trigger" in plain["DbtCloudRunJobOperator"] and "unconfirmed" in plain["FivetranOperator"] and "self-hosted" in plain["AirbyteTriggerSyncOperator"]


# ---- HTTP: params and poke -------------------------------------------------------------------------------------------------------------
def test_http_get_sends_data_as_params_and_http_sensor_is_poke_inside_the_poke_loop(tmp_path, monkeypatch, web, reg194):
    sn = {"poke_interval": 0.05, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False}
    ops = [op("g", "HttpOperator", args={"http_conn_id": "c1", "method": "GET", "endpoint": "ping", "data": {"q": "z", "n": "1"}, "headers": {"X-A": "b"}, "extra_options": {"timeout": 9}}),
           op("w", "HttpSensor", up=["g"], args={"http_conn_id": "c1", "method": "GET", "endpoint": "ready", "request_params": {"s": "1"}, "headers": None}, sensor=sn)]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_http")["dags"]["d1"]
    assert rep["output"] == "components" and rep["resource_types"] == ["HTTPResourceComponent"] and rep["registry_resources"] == ["c1"]
    yml = _yml(tmp_path, "pkg194_http")
    assert 'auth_header_env_var: "C1_AUTH_HEADER"' in yml and "{{ env.C1_BASE_URL }}" in yml
    steps = _steps(tmp_path, "pkg194_http")
    assert ".poke('GET', 'ready'" in steps and "af_poke(" in steps and "params=data" not in steps
    assert _reason(rep, "poke: any status outside 2xx")                                          # the sensor difference is stated
    monkeypatch.setenv("C1_BASE_URL", web.url)
    monkeypatch.setenv("C1_AUTH_HEADER", "Bearer tok")
    web.script += [(200, "ok"), (404, "no"), (200, "ok")]                                       # the operator's call, then the sensor: not yet, then ready
    _run(_load(tmp_path, "pkg194_http"))
    got = web.seen
    assert got[0]["path"] in ("/ping?q=z&n=1", "/ping?n=1&q=z") and got[0]["headers"]["X-A"] == "b" and got[0]["headers"]["Authorization"] == "Bearer tok"
    assert [g["path"] for g in got[1:]] == ["/ready?s=1", "/ready?s=1"]


def test_http_sensor_with_a_response_check_function_keeps_the_python_route(tmp_path, reg194):
    sn = {"poke_interval": 1.0, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False}
    t = op("w", "HttpSensor", args={"http_conn_id": "c1", "endpoint": "ready"}, sensor=sn, callable_args={"response_check": "mod.check"})
    rep = _convert(tmp_path, ir_of(t), "pkg194_httpchk")["dags"]["d1"]
    assert rep["output"] == "python"


# ---- email ---------------------------------------------------------------------------------------------------------------------------------
def test_email_operator_is_smtp_resource_send_email_run_against_a_local_smtp_server(tmp_path, monkeypatch, smtp, reg194):
    att = tmp_path / "report.csv"
    att.write_text("a,b\n1,2\n")
    t = op("mail", "EmailOperator", args={"conn_id": "smtp_x", "to": ["a@x.io", "b@x.io"], "subject": "Hello", "html_content": "<b>hi</b>", "cc": "c@x.io", "bcc": ["d@x.io"],
                                          "files": [str(att)], "from_email": "etl@x.io"},
           tf={"to": ["a@x.io", "b@x.io"], "subject": "Hello", "html_content": "<b>hi</b>", "cc": "c@x.io", "bcc": ["d@x.io"], "files": [str(att)]})
    rep = _convert(tmp_path, ir_of(t), "pkg194_mail")["dags"]["d1"]
    assert rep["resource_types"] == ["SMTPResourceComponent"] and rep["registry_resources"] == ["smtp_x__smtp"]
    yml = _yml(tmp_path, "pkg194_mail")
    assert "{{ env.SMTP_X_HOST }}" in yml and "{{ env.SMTP_X_PORT }}" in yml and "username" not in yml and "password" not in yml
    assert _reason(rep, "password_env_var") and _reason(rep, "use_tls")
    assert RM.DCC_REQUIREMENT in rep["requirements"]
    monkeypatch.setenv("SMTP_X_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_X_PORT", str(smtp.port))
    (tmp_path / "pkg194_mail" / "defs" / "registry_resources" / "defs.yaml").write_text(yml + "  use_tls: false\n")        # the caveat's edit: a plain relay and no login
    _run(_load(tmp_path, "pkg194_mail"))
    msg = smtp.got[-1]
    assert sorted(msg["rcpt"]) == ["a@x.io", "b@x.io", "c@x.io", "d@x.io"]
    assert "Subject: Hello" in msg["data"] and "From: etl@x.io" in msg["data"] and "Bcc" not in msg["data"] and "report.csv" in msg["data"] and "<b>hi</b>" in msg["data"]


def test_email_operator_without_a_sender_reads_the_from_variable_and_custom_headers_keep_the_shim(tmp_path, reg194):
    ok = op("m1", "EmailOperator", args={"to": "a@x.io", "subject": "s", "html_content": "h"}, tf={"to": "a@x.io", "subject": "s", "html_content": "h"})
    bad = op("m2", "EmailOperator", up=["m1"], args={"conn_id": "smtp_h", "to": "a@x.io", "subject": "s", "html_content": "h", "custom_headers": {"X-A": "1"}},
             tf={"to": "a@x.io", "subject": "s", "html_content": "h", "custom_headers": {"X-A": "1"}})
    rep = _convert(tmp_path, ir_of(ok, bad), "pkg194_mailfb")["dags"]["d1"]
    steps = _steps(tmp_path, "pkg194_mailfb")
    assert "os.environ.get('SMTP_DEFAULT_FROM', '')" in steps and "send_email(context, 'smtp_h'" in steps          # the shim carries custom_headers
    assert rep["registry_resources"] == ["smtp_default__smtp"]
    assert _reason(rep, "registry resource not used for this task: custom_headers")


# ---- Slack -----------------------------------------------------------------------------------------------------------------------------
def test_slack_webhook_operator_is_slack_resource_post_message_run_against_a_local_server(tmp_path, monkeypatch, web, reg194):
    a = {"slack_webhook_conn_id": "slk", "message": "deploy done", "channel": "#ops", "username": "bot", "icon_emoji": ":tada:", "blocks": [{"type": "divider"}], "timeout": 5}
    rep = _convert(tmp_path, ir_of(op("s", "SlackWebhookOperator", args=a, tf={"message": "deploy done"})), "pkg194_slkw")["dags"]["d1"]
    assert rep["resource_types"] == ["SlackResourceComponent"]
    yml = _yml(tmp_path, "pkg194_slkw")
    assert 'webhook_url_env_var: "SLK_WEBHOOK_URL"' in yml and "token" not in yml and "hooks.slack.com" not in yml
    assert _reason(rep, "timeout=5 is not applied") and _reason(rep, "channel='#ops' is passed but slack_resource.post_message ignores it")
    monkeypatch.setenv("SLK_WEBHOOK_URL", web.url + "/hook")
    _run(_load(tmp_path, "pkg194_slkw"))
    got = web.seen[-1]
    assert got["path"] == "/hook" and json.loads(got["body"]) == {"text": "deploy done", "blocks": [{"type": "divider"}], "username": "bot", "icon_emoji": ":tada:"}      # the channel is dropped by the resource


def test_slack_api_post_operator_is_slack_resource_with_the_bot_token_run_against_a_recording_client(tmp_path, monkeypatch, reg194):
    a = {"slack_conn_id": "slk_api", "channel": "#data", "text": "hi", "username": "Airflow"}
    rep = _convert(tmp_path, ir_of(op("s", "SlackAPIPostOperator", args=a, tf={"text": "hi", "channel": "#data"})), "pkg194_slka")["dags"]["d1"]
    assert rep["resource_types"] == ["SlackResourceComponent"] and "slack-sdk" in rep["requirements"]
    yml = _yml(tmp_path, "pkg194_slka")
    assert 'token_env_var: "SLK_API_TOKEN"' in yml and "xoxb" not in yml
    assert _reason(rep, "SlackPostResource")
    monkeypatch.setenv("SLK_API_TOKEN", "xoxb-test")
    calls = []

    class Resp(dict):
        data = {"ok": True, "ts": "1"}

    class Client:
        def chat_postMessage(self, **kw):
            calls.append(kw)
            return Resp(ok=True)
    SlackPostResource = _mod("slack_resource").SlackPostResource
    monkeypatch.setattr(SlackPostResource, "get_client", lambda self: Client())
    _run(_load(tmp_path, "pkg194_slka"))
    assert calls == [{"channel": "#data", "text": "hi", "blocks": None, "username": "Airflow"}]


# ---- SSH -------------------------------------------------------------------------------------------------------------------------------
def test_ssh_operator_is_ssh_resource_run_command_and_a_non_zero_exit_fails_the_step(tmp_path, monkeypatch, reg194):
    t = op("rm", "SSHOperator", args={"ssh_conn_id": "ssh_x", "command": "ls /data", "cmd_timeout": 60, "environment": {"A": "1"}}, tf={"command": "ls /data"})
    rep = _convert(tmp_path, ir_of(t), "pkg194_ssh")["dags"]["d1"]
    assert rep["resource_types"] == ["SSHResourceComponent"] and "paramiko" in rep["requirements"]
    yml = _yml(tmp_path, "pkg194_ssh")
    assert 'host_env_var: "SSH_X_HOST"' in yml and 'username_env_var: "SSH_X_USER"' in yml and "password" not in yml and "private_key" not in yml
    assert _reason(rep, "password_env_var") and _reason(rep, "RSA key file")
    monkeypatch.setenv("SSH_X_HOST", "h.example")
    monkeypatch.setenv("SSH_X_USER", "etl")
    ran, state = [], {"code": 0}

    class Chan:
        def recv_exit_status(self):
            return state["code"]

    class Out:
        channel = Chan()

        def __init__(self, text):
            self.text = text

        def read(self):
            return self.text.encode()

    class Client:
        def exec_command(self, cmd, timeout=None):
            ran.append((cmd, timeout))
            return type("In", (), {"close": lambda s: None})(), Out("out"), Out("boom")

        def close(self):
            pass
    SSHResource = _mod("ssh_resource").SSHResource
    monkeypatch.setattr(SSHResource, "_connect", lambda self: (Client(), None))
    defs = _load(tmp_path, "pkg194_ssh")
    _run(defs)
    assert ran == [("export A=1; ls /data", 60)]
    state["code"] = 2
    with pytest.raises(AssertionError):
        _run(defs)


def test_ssh_operator_with_get_pty_keeps_the_shim_and_the_gap_is_named(tmp_path, reg194):
    t = op("rm", "SSHOperator", args={"ssh_conn_id": "ssh_x", "command": "ls", "get_pty": True}, tf={"command": "ls"})
    rep = _convert(tmp_path, ir_of(t), "pkg194_sshfb")["dags"]["d1"]
    assert rep.get("resource_types", []) == [] and "ssh_run(" in _steps(tmp_path, "pkg194_sshfb")
    assert _reason(rep, "registry resource not used for this task: get_pty")


# ---- Lambda ----------------------------------------------------------------------------------------------------------------------------
def test_lambda_invoke_operator_is_lambda_resource_invoke_run_against_a_botocore_stubber(tmp_path, monkeypatch, reg194):
    from botocore.response import StreamingBody
    t = op("fn", "LambdaInvokeFunctionOperator", args={"function_name": "etl-fn", "payload": json.dumps({"n": 1}), "qualifier": "live", "log_type": "Tail", "region_name": "eu-west-1", "aws_conn_id": "aws_x"},
           tf={"function_name": "etl-fn", "payload": json.dumps({"n": 1})})
    rep = _convert(tmp_path, ir_of(t), "pkg194_lam")["dags"]["d1"]
    assert rep["resource_types"] == ["LambdaResourceComponent"] and "boto3" in rep["requirements"]
    yml = _yml(tmp_path, "pkg194_lam")
    assert 'region_name: "eu-west-1"' in yml and "key" not in yml.lower().replace("resource_key", "")
    client, stub = _stubbed("lambda", "eu-west-1")
    body = b'{"ok": true}'
    stub.add_response("invoke", {"StatusCode": 200, "Payload": StreamingBody(io.BytesIO(body), len(body))},
                      {"FunctionName": "etl-fn", "InvocationType": "RequestResponse", "Payload": b'{"n": 1}', "Qualifier": "live", "LogType": "Tail"})
    stub.activate()
    monkeypatch.setattr(_mod("lambda_resource").LambdaResource, "_client", lambda self: client)
    _run(_load(tmp_path, "pkg194_lam"))
    stub.assert_no_pending_responses()


def test_lambda_with_a_non_object_payload_keeps_the_shim_and_says_why(tmp_path, reg194):
    t = op("fn", "LambdaInvokeFunctionOperator", args={"function_name": "f", "payload": "[1, 2]", "aws_conn_id": "aws_default"}, tf={"function_name": "f"})
    rep = _convert(tmp_path, ir_of(t), "pkg194_lamfb")["dags"]["d1"]
    assert rep["resource_types"] == [] and "lambda_invoke(" in _steps(tmp_path, "pkg194_lamfb")
    assert _reason(rep, "payload is not a JSON object")


# ---- SQL by URL ------------------------------------------------------------------------------------------------------------------------
def test_sql_operator_is_sql_url_resource_with_the_statements_split_in_the_converter(tmp_path, monkeypatch, reg194):
    db = tmp_path / "w.db"
    sql = "create table t (id integer, name text); -- a comment; with a semicolon\ninsert into t values (1, 'a;b'); insert into t values (:i, :n);"
    sql = sql.replace(":i", "%(i)s").replace(":n", "%(n)s")
    t = op("ddl", "SQLExecuteQueryOperator", args={"conn_id": "sql_x", "sql": sql, "split_statements": True, "parameters": {"i": 2, "n": "z"}}, tf={"sql": sql, "parameters": {"i": 2, "n": "z"}})
    rep = _convert(tmp_path, ir_of(t), "pkg194_sql")["dags"]["d1"]
    assert rep["resource_types"] == ["SQLUrlResourceComponent"] and "sqlalchemy" in rep["requirements"]
    assert 'connection_url_env_var: "SQL_X_URL"' in _yml(tmp_path, "pkg194_sql")
    steps = _steps(tmp_path, "pkg194_sql")
    assert "%(i)s" not in steps and ":i" in steps
    assert _reason(rep, "committed on its own")
    monkeypatch.setenv("SQL_X_URL", f"sqlite:///{db}")
    _run(_load(tmp_path, "pkg194_sql"))
    con = sqlite3.connect(db)
    assert sorted(con.execute("select id, name from t").fetchall()) == [(1, "a;b"), (2, "z")]


def test_sql_split_is_exact_for_quotes_comments_and_refuses_procedural_bodies():
    assert RM.split_sql("select 1; select ';' as x; -- c;\n select 2;") == ["select 1", "select ';' as x", "-- c;\n select 2"]
    assert RM.split_sql("select 'it''s;'; select 2") == ["select 'it''s;'", "select 2"]
    assert RM.split_sql("create function f() returns int as $$ select 1; $$ language sql") is None
    assert RM.split_sql("begin; select 1; end;") is None


def test_sql_with_positional_parameters_or_bind_like_text_keeps_the_shim(tmp_path, reg194):
    a = op("p", "SQLExecuteQueryOperator", args={"conn_id": "sql_y", "sql": "select * from t where a = %s", "parameters": [1]}, tf={"sql": "select * from t where a = %s", "parameters": [1]})
    b = op("q", "SQLExecuteQueryOperator", up=["p"], args={"conn_id": "sql_y", "sql": "select ':ab' as t, '12:30' as c from z"}, tf={"sql": "select ':ab' as t, '12:30' as c from z"})
    rep = _convert(tmp_path, ir_of(a, b), "pkg194_sqlfb")["dags"]["d1"]
    assert rep.get("resource_types", []) == [] and _reason(rep, "positional parameters") and _reason(rep, ":name")
    assert "sql_run(" in _steps(tmp_path, "pkg194_sqlfb")


# ---- Databricks ------------------------------------------------------------------------------------------------------------------------
def _dbx_client(fake, listed=()):
    class State:
        life_cycle_state, result_state, state_message = "TERMINATED", "SUCCESS", ""

    class Client:
        class jobs:
            @staticmethod
            def list(name=None):
                fake.setdefault("listed", []).append(name)
                return [types.SimpleNamespace(job_id=j) for j in listed]

            @staticmethod
            def run_now(**kw):
                fake.setdefault("run_now", []).append(kw)
                return types.SimpleNamespace(response=types.SimpleNamespace(run_id=7))

            @staticmethod
            def submit(**kw):
                fake.setdefault("submit", []).append(kw)
                return types.SimpleNamespace(response=types.SimpleNamespace(run_id=8))

            @staticmethod
            def wait_get_run_job_terminated_or_skipped(run_id, timeout):
                fake.setdefault("timeouts", []).append(timeout.total_seconds())
                return types.SimpleNamespace(state=State, run_page_url="http://u")
    return Client


def test_databricks_run_now_by_job_name_and_submit_run_from_json_use_databricks_jobs_resource(tmp_path, monkeypatch, reg194):
    ops = [op("byname", "DatabricksRunNowOperator", args={"databricks_conn_id": "dbx", "job_name": "nightly", "json": {}, "job_parameters": {"d": "1"}, "jar_params": ["a"], "spark_submit_params": ["--x"],
                                                           "polling_period_seconds": 10}),
           op("sub", "DatabricksSubmitRunOperator", up=["byname"], args={"databricks_conn_id": "dbx", "json": {"run_name": "adhoc", "tasks": [{"task_key": "t", "notebook_task": {"notebook_path": "/n"}}]}}),
           op("legacy", "DatabricksSubmitRunOperator", up=["sub"], args={"databricks_conn_id": "dbx", "json": {}, "notebook_task": {"notebook_path": "/m"}, "existing_cluster_id": "c-1", "timeout_seconds": 600})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_dbx")["dags"]["d1"]
    assert rep["resource_types"] == ["DatabricksJobsResourceComponent"] and rep["registry_resources"] == ["dbx__jobs"] and "databricks-sdk" in rep["requirements"]
    yml = _yml(tmp_path, "pkg194_dbx")
    assert 'workspace_token_env_var: "DBX_TOKEN"' in yml and "{{ env.DBX_HOST }}" in yml
    assert _reason(rep, "job_name is looked up") and _reason(rep, "single-task fields") and _reason(rep, "polling_period_seconds=10")
    monkeypatch.setenv("DBX_HOST", "https://h.example")
    monkeypatch.setenv("DBX_TOKEN", "tok")
    fake: dict = {}
    DBR = _mod("databricks_jobs_resource").DatabricksJobsResource
    monkeypatch.setattr(DBR, "_client", lambda self: _dbx_client(fake, listed=[33]))

    class SubmitTask:
        from_dict = staticmethod(lambda d: ("task", d))
    _fake_module(monkeypatch, "databricks.sdk.service.jobs", SubmitTask=SubmitTask)
    _run(_load(tmp_path, "pkg194_dbx"))
    assert fake["listed"] == ["nightly"]
    assert fake["run_now"] == [{"job_id": 33, "job_parameters": {"d": "1"}, "jar_params": ["a"], "spark_submit_params": ["--x"]}]
    assert fake["submit"][0]["run_name"] == "adhoc" and fake["submit"][0]["tasks"] == [("task", {"task_key": "t", "notebook_task": {"notebook_path": "/n"}})]
    legacy = fake["submit"][1]
    assert legacy["tasks"] == [("task", {"task_key": "legacy", "notebook_task": {"notebook_path": "/m"}, "existing_cluster_id": "c-1"})] and legacy["timeout_seconds"] == 600 and "notebook_task" not in legacy
    assert set(fake["timeouts"]) == {604800.0}


def test_databricks_arguments_the_resource_cannot_carry_keep_the_shim_and_say_why(tmp_path, reg194):
    ops = [op("a", "DatabricksRunNowOperator", args={"databricks_conn_id": "d2", "json": {"job_id": 5}, "wait_for_termination": False}),
           op("b", "DatabricksRunNowOperator", up=["a"], args={"databricks_conn_id": "d2", "json": {"job_id": 5, "python_named_params": {"k": "v"}}}),
           op("c", "DatabricksRunNowOperator", up=["b"], args={"databricks_conn_id": "d2", "json": {"job_id": 6}, "cancel_previous_runs": True})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_dbxfb")["dags"]["d1"]
    assert rep["output"] == "python"                   # the shim also refuses cancel_previous_runs: the DAG keeps the Python route, where that task is the shim's marked refusal
    # the same arguments one at a time, for the reasons
    for k, v, why in (("wait_for_termination", False, "always waits"), ("repair_run", True, "repair_run=True")):
        p = tmp_path / k
        p.mkdir()
        r = _convert(p, ir_of(op("a", "DatabricksRunNowOperator", args={"databricks_conn_id": "d3", "json": {"job_id": 5}, k: v})), "pkg194_dbxfb_" + k)["dags"]["d1"]
        assert r.get("resource_types", []) == [] or r["output"] == "python"
        if r["output"] == "components":
            assert _reason(r, why)


# ---- Spark -----------------------------------------------------------------------------------------------------------------------------
def test_spark_submit_carries_all_flags_and_keytab_principal_proxy_user_as_extra_args(tmp_path, monkeypatch, reg194):
    out = tmp_path / "argv.txt"
    binary = tmp_path / "fake-spark-submit"
    binary.write_text(f"#!/bin/sh\necho \"$@\" > {out}\n")
    binary.chmod(0o755)
    a = {"conn_id": "spark_p7", "application": "/jobs/etl.py", "application_args": ["--d", "1"], "conf": {"spark.a": "b"}, "executor_memory": "2g", "num_executors": 3, "name": "etl", "spark_binary": str(binary),
         "packages": "p:q:1,r:s:2", "exclude_packages": "x:y", "repositories": "https://r1,https://r2", "files": "a.txt,b.txt", "archives": "z.zip", "driver_class_path": "/cp", "executor_cores": 4,
         "verbose": True, "properties_file": "/etc/spark.conf", "keytab": "/k.tab", "principal": "me@R", "proxy_user": "bob"}
    rep = _convert(tmp_path, ir_of(op("sp", "SparkSubmitOperator", args=a, tf=dict(a))), "pkg194_spark")["dags"]["d1"]
    assert rep["resource_types"] == ["SparkSubmitResourceComponent"]
    assert _reason(rep, "keytab, principal and proxy_user have no field")
    monkeypatch.setenv("SPARK_P7_MASTER", "local[2]")
    _run(_load(tmp_path, "pkg194_spark"))
    argv = out.read_text()
    for needle in ("--master local[2]", "--conf spark.a=b", "--executor-memory 2g", "--num-executors 3", "--name etl", "--packages p:q:1,r:s:2", "--exclude-packages x:y", "--repositories https://r1,https://r2",
                   "--files a.txt,b.txt", "--archives z.zip", "--driver-class-path /cp", "--executor-cores 4", "--verbose", "--properties-file /etc/spark.conf", "--keytab /k.tab", "--principal me@R",
                   "--proxy-user bob", "/jobs/etl.py --d 1"):
        assert needle in argv, (needle, argv)


# ---- EMR -------------------------------------------------------------------------------------------------------------------------------
def test_emr_add_steps_create_job_flow_and_terminate_use_emr_resource_run_against_a_botocore_stubber(tmp_path, monkeypatch, reg194):
    steps = [{"Name": "s1", "ActionOnFailure": "CANCEL_AND_WAIT", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", "a.py"], "Properties": [{"Key": "k", "Value": "v"}]}},
             {"Name": "s2", "HadoopJarStep": {"Jar": "x.jar", "MainClass": "M", "Args": []}}]
    ov = {"Name": "c", "ReleaseLabel": "emr-6.9.0", "Applications": [{"Name": "Spark"}], "Instances": {"InstanceCount": 2, "KeepJobFlowAliveWhenNoSteps": True}, "JobFlowRole": "r1", "ServiceRole": "r2",
          "Tags": [{"Key": "t", "Value": "1"}], "VisibleToAllUsers": False}
    ops = [op("mk", "EmrCreateJobFlowOperator", args={"aws_conn_id": "aws_p7", "job_flow_overrides": ov, "region_name": "us-east-1", "wait_for_completion": True, "waiter_delay": 0.01}),
           op("add", "EmrAddStepsOperator", up=["mk"], args={"aws_conn_id": "aws_p7", "job_flow_id": "j-ABC", "steps": steps, "wait_for_completion": True, "waiter_delay": 0.01, "region_name": "us-east-1"}),
           op("rm", "EmrTerminateJobFlowOperator", up=["add"], args={"aws_conn_id": "aws_p7", "job_flow_id": "j-ABC", "wait_for_completion": False, "region_name": "us-east-1"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_emr")["dags"]["d1"]
    assert rep["resource_types"] == ["EmrResourceComponent"] and rep["registry_resources"] == ["aws_p7__emr"] and rep["output"] == "components"
    assert 'region_name: "us-east-1"' in _yml(tmp_path, "pkg194_emr")
    assert _reason(rep, "default credential chain") and _reason(rep, "one call each")
    client, stub = _stubbed("emr")
    stub.add_response("run_job_flow", {"JobFlowId": "j-ABC"}, {"Name": "c", "Instances": {"InstanceCount": 2, "KeepJobFlowAliveWhenNoSteps": True}, "VisibleToAllUsers": False, "ReleaseLabel": "emr-6.9.0",
                                                               "Applications": [{"Name": "Spark"}], "JobFlowRole": "r1", "ServiceRole": "r2", "Tags": [{"Key": "t", "Value": "1"}]})
    stub.add_response("describe_cluster", {"Cluster": {"Status": {"State": "WAITING"}}}, {"ClusterId": "j-ABC"})
    stub.add_response("add_job_flow_steps", {"StepIds": ["s-1"]}, {"JobFlowId": "j-ABC", "Steps": [{"Name": "s1", "ActionOnFailure": "CANCEL_AND_WAIT",
                                                                                                   "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", "a.py"], "Properties": [{"Key": "k", "Value": "v"}]}}]})
    stub.add_response("describe_step", {"Step": {"Status": {"State": "COMPLETED"}}}, {"ClusterId": "j-ABC", "StepId": "s-1"})
    stub.add_response("add_job_flow_steps", {"StepIds": ["s-2"]}, {"JobFlowId": "j-ABC", "Steps": [{"Name": "s2", "ActionOnFailure": "CONTINUE", "HadoopJarStep": {"Jar": "x.jar", "Args": [], "MainClass": "M"}}]})
    stub.add_response("describe_step", {"Step": {"Status": {"State": "COMPLETED"}}}, {"ClusterId": "j-ABC", "StepId": "s-2"})
    stub.add_response("terminate_job_flows", {}, {"JobFlowIds": ["j-ABC"]})
    stub.activate()
    monkeypatch.setattr(_mod("emr_resource").EmrResource, "_client", lambda self: client)
    _run(_load(tmp_path, "pkg194_emr"))
    stub.assert_no_pending_responses()


def test_emr_with_an_execution_role_has_no_resource_plan_and_the_dag_keeps_the_python_route(tmp_path, reg194):
    t = op("add", "EmrAddStepsOperator", args={"aws_conn_id": "aws_default", "job_flow_id": "j-1", "execution_role_arn": "arn:aws:iam::1:role/x", "steps": [{"Name": "s", "HadoopJarStep": {"Jar": "j"}}]})
    rep = _convert(tmp_path, ir_of(t), "pkg194_emrfb")["dags"]["d1"]
    assert rep["output"] == "python"
    assert any(c["kind"] == "stub" and c["task"] == "add" for c in rep["caveats"])


# ---- Athena, Redshift Data API ---------------------------------------------------------------------------------------------------------
def test_athena_and_redshift_data_operators_use_their_resources_run_against_a_botocore_stubber(tmp_path, monkeypatch, reg194):
    ops = [op("q", "AthenaOperator", args={"aws_conn_id": "aws_p7", "query": "select 1", "database": "db", "output_location": "s3://b/o/", "workgroup": "wg", "sleep_time": 0.01, "max_polling_attempts": 100,
                                           "region_name": "eu-west-1"}),
           op("rs", "RedshiftDataOperator", up=["q"], args={"aws_conn_id": "aws_p7", "sql": "vacuum t", "database": "dev", "cluster_identifier": "cl", "db_user": "u", "poll_interval": 0.01, "wait_for_completion": True})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_ath")["dags"]["d1"]
    assert rep["resource_types"] == ["AthenaResourceComponent", "RedshiftDataApiResourceComponent"]
    yml = _yml(tmp_path, "pkg194_ath")
    assert 'region_name: "eu-west-1"' in yml and "{{ env.AWS_DEFAULT_REGION }}" in yml
    ath, st1 = _stubbed("athena", "eu-west-1")
    st1.add_response("start_query_execution", {"QueryExecutionId": "q1"}, {"QueryString": "select 1", "QueryExecutionContext": {"Database": "db"}, "ResultConfiguration": {"OutputLocation": "s3://b/o/"}, "WorkGroup": "wg"})
    st1.add_response("get_query_execution", {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}, {"QueryExecutionId": "q1"})
    st1.activate()
    rsd, st2 = _stubbed("redshift-data")
    st2.add_response("execute_statement", {"Id": "st-1"}, {"Database": "dev", "Sql": "vacuum t", "ClusterIdentifier": "cl", "DbUser": "u"})
    st2.add_response("describe_statement", {"Id": "st-1", "Status": "FINISHED"}, {"Id": "st-1"})
    st2.activate()
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(_mod("athena_resource").AthenaResource, "_client", lambda self: ath)
    monkeypatch.setattr(_mod("redshift_data_api_resource").RedshiftDataApiResource, "_client", lambda self: rsd)
    _run(_load(tmp_path, "pkg194_ath"))
    st1.assert_no_pending_responses()
    st2.assert_no_pending_responses()


def test_redshift_batch_and_athena_client_token_keep_the_python_route(tmp_path, reg194):
    t = op("rs", "RedshiftDataOperator", args={"sql": ["a", "b"], "database": "dev", "workgroup_name": "wg"})
    rep = _convert(tmp_path, ir_of(t), "pkg194_rsfb")["dags"]["d1"]
    assert rep["output"] == "python"


# ---- Dataproc --------------------------------------------------------------------------------------------------------------------------
def test_dataproc_operators_use_dataproc_resource_run_against_recording_clients(tmp_path, monkeypatch, reg194):
    cfg = {"master_config": {"num_instances": 1, "machine_type_uri": "n2-standard-8"}, "worker_config": {"num_instances": 3, "machine_type_uri": "n2-standard-4", "disk_config": {"boot_disk_size_gb": 100}},
           "software_config": {"image_version": "2.1", "properties": {"spark:a": "b"}}, "gce_cluster_config": {"zone_uri": "us-central1-a"}}
    job = {"placement": {"cluster_name": "cl"}, "pyspark_job": {"main_python_file_uri": "gs://b/j.py", "args": ["1"]}, "labels": {"k": "v"}}
    base = {"project_id": "proj", "region": "us-central1", "gcp_conn_id": "gcp_p7"}
    ops = [op("mk", "DataprocCreateClusterOperator", args={**base, "cluster_name": "cl", "cluster_config": cfg}),
           op("job", "DataprocSubmitJobOperator", up=["mk"], args={**base, "job": job}),
           op("batch", "DataprocCreateBatchOperator", up=["job"], args={**base, "batch": {"pyspark_batch": {"main_python_file_uri": "gs://b/j.py"}}, "batch_id": "b-1"}),
           op("rm", "DataprocDeleteClusterOperator", up=["batch"], args={**base, "cluster_name": "cl"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_dp")["dags"]["d1"]
    assert rep["resource_types"] == ["DataprocResourceComponent"] and rep["registry_resources"] == ["gcp_p7__dataproc"] and "google-cloud-dataproc" in rep["requirements"]
    assert _reason(rep, "Application Default Credentials") and _reason(rep, "flat cluster_config")
    fake_google_api_core(monkeypatch)
    seen = {"created": [], "jobs": [], "batches": [], "deleted": []}
    NotFound = sys.modules["google.api_core.exceptions"].NotFound
    present = {"cl": False}

    class Op:
        def __init__(self, v=None):
            self.v = v

        def result(self, timeout=None):
            return self.v
    state = lambda n: types.SimpleNamespace(name=n)

    class Cluster:
        def get_cluster(self, project_id, region, cluster_name):
            if not present[cluster_name]:
                raise NotFound("no")
            return types.SimpleNamespace(status=types.SimpleNamespace(state=state("RUNNING")))

        def create_cluster(self, project_id, region, cluster):
            seen["created"].append(cluster)
            present[cluster["cluster_name"]] = True
            return Op(types.SimpleNamespace(status=types.SimpleNamespace(state=state("RUNNING"))))

        def delete_cluster(self, project_id, region, cluster_name):
            seen["deleted"].append(cluster_name)
            return Op()

    class Job:
        def submit_job(self, project_id, region, job):
            seen["jobs"].append(job)
            return types.SimpleNamespace(reference=types.SimpleNamespace(job_id="j1"), status=types.SimpleNamespace(state=state("DONE"), details=None), driver_output_resource_uri=None)

    class Batch:
        def create_batch(self, parent, batch, batch_id):
            seen["batches"].append((parent, batch, batch_id))
            return Op(types.SimpleNamespace(state=state("SUCCEEDED"), state_message=""))
    _fake_module(monkeypatch, "google.cloud.dataproc_v1", Batch=lambda **kw: ("Batch", kw))
    DR = _mod("dataproc_resource").DataprocResource
    monkeypatch.setattr(DR, "_cluster_client", lambda self, region: Cluster())
    monkeypatch.setattr(DR, "_job_client", lambda self, region: Job())
    monkeypatch.setattr(DR, "_batch_client", lambda self, region: Batch())
    _run(_load(tmp_path, "pkg194_dp"))
    c = seen["created"][0]["config"]
    assert c["master_config"]["machine_type_uri"] == "n2-standard-8" and c["worker_config"]["num_instances"] == 3 and c["worker_config"]["disk_config"] == {"boot_disk_size_gb": 100}
    assert c["software_config"] == {"image_version": "2.1", "properties": {"spark:a": "b"}} and c["gce_cluster_config"] == {"zone_uri": "us-central1-a"}
    j = seen["jobs"][0]
    assert j["placement"] == {"cluster_name": "cl"} and j["labels"] == {"k": "v"} and j["pyspark_job"]["main_python_file_uri"] == "gs://b/j.py" and j["pyspark_job"]["args"] == ["1"]
    assert seen["batches"] == [("projects/proj/regions/us-central1", ("Batch", {"pyspark_batch": {"main_python_file_uri": "gs://b/j.py"}}), "b-1")] and seen["deleted"] == ["cl"]


def test_dataproc_cluster_config_the_flat_form_cannot_say_keeps_the_python_route(tmp_path, reg194):
    t = op("mk", "DataprocCreateClusterOperator", args={"project_id": "p", "region": "r", "cluster_name": "c", "cluster_config": {"autoscaling_config": {"policy_uri": "x"}}})
    rep = _convert(tmp_path, ir_of(t), "pkg194_dpfb")["dags"]["d1"]
    assert rep["output"] == "python"
    with pytest.raises(RM.NoPlan, match="autoscaling_config"):
        RM._dp_cluster_config({"autoscaling_config": {}})


# ---- Pub/Sub ---------------------------------------------------------------------------------------------------------------------------
def test_pubsub_operators_use_pubsub_resource_run_against_recording_clients(tmp_path, monkeypatch, reg194):
    base = {"project_id": "proj", "gcp_conn_id": "gcp_ps"}
    ops = [op("t", "PubSubCreateTopicOperator", args={**base, "topic": "tp", "labels": {"a": "b"}, "fail_if_exists": False}),
           op("s", "PubSubCreateSubscriptionOperator", up=["t"], args={**base, "topic": "tp", "subscription": "sb", "ack_deadline_secs": 30, "push_config": {"pushEndpoint": "https://h/p"}}),
           op("p", "PubSubPublishMessageOperator", up=["s"], args={**base, "topic": "tp", "messages": [{"data": {"repr": "b'hello'"}, "attributes": {"k": "v"}}, {"data": {"repr": "b'two'"}}]}),
           op("ds", "PubSubDeleteSubscriptionOperator", up=["p"], args={**base, "subscription": "sb"}),
           op("dt", "PubSubDeleteTopicOperator", up=["ds"], args={**base, "topic": "tp"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_ps")["dags"]["d1"]
    assert rep["resource_types"] == ["PubsubResourceComponent"] and "google-cloud-pubsub" in rep["requirements"] and rep["output"] == "components"
    fake_google_api_core(monkeypatch)
    log = []

    class Pub:
        def topic_path(self, p, t):
            return f"projects/{p}/topics/{t}"

        def create_topic(self, request):
            log.append(("create_topic", request))
            return types.SimpleNamespace(labels={"a": "b"})

        def publish(self, path, data, **kw):
            log.append(("publish", path, data, kw))
            return types.SimpleNamespace(result=lambda: "m-1")

        def delete_topic(self, request):
            log.append(("delete_topic", request))

    class Sub:
        def topic_path(self, p, t):
            return f"projects/{p}/topics/{t}"

        def subscription_path(self, p, s):
            return f"projects/{p}/subscriptions/{s}"

        def create_subscription(self, request):
            log.append(("create_subscription", request))

        def delete_subscription(self, request):
            log.append(("delete_subscription", request))
    PR = _mod("pubsub_resource").PubsubResource
    monkeypatch.setattr(PR, "_publisher_client", lambda self: Pub())
    monkeypatch.setattr(PR, "_subscriber_client", lambda self: Sub())
    _run(_load(tmp_path, "pkg194_ps"))
    assert ("create_topic", {"name": "projects/proj/topics/tp", "labels": {"a": "b"}}) in log
    assert ("create_subscription", {"name": "projects/proj/subscriptions/sb", "topic": "projects/proj/topics/tp", "ack_deadline_seconds": 30, "push_config": {"push_endpoint": "https://h/p"}}) in log
    assert ("publish", "projects/proj/topics/tp", b"hello", {"k": "v"}) in log and ("publish", "projects/proj/topics/tp", b"two", {}) in log
    assert ("delete_subscription", {"subscription": "projects/proj/subscriptions/sb"}) in log and ("delete_topic", {"topic": "projects/proj/topics/tp"}) in log


def test_pubsub_fail_if_exists_true_keeps_the_python_route(tmp_path, reg194):
    t = op("t", "PubSubCreateTopicOperator", args={"project_id": "p", "topic": "tp", "fail_if_exists": True})
    assert _convert(tmp_path, ir_of(t), "pkg194_psfb")["dags"]["d1"]["output"] == "python"


# ---- BigQuery DDL and value check ------------------------------------------------------------------------------------------------------
def test_bigquery_dataset_table_and_value_check_operators_use_bigquery_ddl_resource(tmp_path, monkeypatch, reg194):
    base = {"gcp_conn_id": "gcp_bq"}
    schema = [{"name": "id", "type": "INTEGER", "mode": "REQUIRED"}, {"name": "d", "type": "DATE"}]
    ops = [op("ds", "BigQueryCreateEmptyDatasetOperator", args={**base, "dataset_id": "raw", "project_id": "proj", "location": "EU", "exists_ok": False}),
           op("tb", "BigQueryCreateEmptyTableOperator", up=["ds"], args={**base, "dataset_id": "raw", "table_id": "t", "project_id": "proj", "schema_fields": schema,
                                                                          "time_partitioning": {"type": "DAY", "field": "d"}, "cluster_fields": ["id"], "labels": {"a": "b"}, "location": "EU"}),
           op("chk", "BigQueryValueCheckOperator", up=["tb"], args={**base, "sql": "select count(*) from raw.t", "pass_value": 3, "location": "EU"}),
           op("dt", "BigQueryDeleteTableOperator", up=["chk"], args={**base, "deletion_dataset_table": "proj.raw.t", "ignore_if_missing": True}),
           op("dd", "BigQueryDeleteDatasetOperator", up=["dt"], args={**base, "dataset_id": "raw", "project_id": "proj", "delete_contents": True})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_bq")["dags"]["d1"]
    assert rep["resource_types"] == ["BigQueryDdlResourceComponent"] and rep["registry_resources"] == ["gcp_bq__bqddl", "gcp_bq__bqddl_EU"] and "google-cloud-bigquery" in rep["requirements"]
    yml = _yml(tmp_path, "pkg194_bq")
    assert 'location: "EU"' in yml and "credentials" not in yml.replace("resource", "") and "GOOGLE_APPLICATION_CREDENTIALS" not in yml
    assert _reason(rep, "GOOGLE_APPLICATION_CREDENTIALS") and _reason(rep, "exists_ok=False") and _reason(rep, "standard SQL")
    fake_google_api_core(monkeypatch)
    log = []

    class Ref:
        from_string = staticmethod(lambda q, default_project=None: ("ref", q))

    class Obj:
        def __init__(self, *a, **kw):
            self.a = a

    bq = _fake_module(monkeypatch, "google.cloud.bigquery", DatasetReference=Ref, Dataset=lambda ref: types.SimpleNamespace(ref=ref), TimePartitioningType=types.SimpleNamespace(DAY="DAY"),
                      TimePartitioning=lambda type_=None, field=None: ("tp", type_, field), SchemaField=lambda name, field_type, mode, description=None: (name, field_type, mode),
                      Table=lambda ref, schema=None: types.SimpleNamespace(ref=ref, schema=schema))

    class Row:
        def __init__(self, v):
            self._v = v

        def values(self):
            return [self._v]

    class Client:
        def create_dataset(self, ds, exists_ok):
            log.append(("create_dataset", ds.ref, ds.location, exists_ok))
            return types.SimpleNamespace(location="EU", labels={})

        def create_table(self, table, exists_ok):
            log.append(("create_table", table.ref, [s[0] for s in table.schema], table.time_partitioning, table.clustering_fields, table.labels, exists_ok))
            return types.SimpleNamespace(schema=[types.SimpleNamespace(name="id")], time_partitioning=types.SimpleNamespace(field="d"))

        def query(self, q):
            log.append(("query", q))
            return types.SimpleNamespace(result=lambda: [Row(3)])

        def delete_table(self, ref, not_found_ok):
            log.append(("delete_table", ref, not_found_ok))

        def get_table(self, ref):
            raise sys.modules["google.api_core.exceptions"].NotFound("gone")

        def delete_dataset(self, ref, delete_contents, not_found_ok):
            log.append(("delete_dataset", ref, delete_contents))

        def get_dataset(self, ref):
            raise sys.modules["google.api_core.exceptions"].NotFound("gone")
    monkeypatch.setattr(_mod("bigquery_ddl_resource").BigQueryDdlResource, "_client", lambda self: (Client(), "proj"))
    _run(_load(tmp_path, "pkg194_bq"))
    assert ("create_dataset", ("ref", "proj.raw"), "EU", True) in log
    assert ("create_table", "proj.raw.t", ["id", "d"], ("tp", "DAY", "d"), ["id"], {"a": "b"}, True) in log
    assert ("query", "select count(*) from raw.t") in log and ("delete_table", "proj.raw.t", True) in log and ("delete_dataset", ("ref", "proj.raw"), True) in log
    del bq


def test_bigquery_value_check_that_fails_fails_the_step_and_a_relative_tolerance_keeps_the_python_route(tmp_path, monkeypatch, reg194):
    t = op("chk", "BigQueryValueCheckOperator", args={"gcp_conn_id": "gcp_bq2", "sql": "select 1", "pass_value": 2})
    rep = _convert(tmp_path, ir_of(t), "pkg194_bqchk")["dags"]["d1"]
    assert rep["resource_types"] == ["BigQueryDdlResourceComponent"]
    monkeypatch.setattr(_mod("bigquery_ddl_resource").BigQueryDdlResource, "_client",
                        lambda self: (types.SimpleNamespace(query=lambda q: types.SimpleNamespace(result=lambda: [types.SimpleNamespace(values=lambda: [1])])), "p"))
    with pytest.raises(AssertionError):
        _run(_load(tmp_path, "pkg194_bqchk"))
    t2 = op("chk", "BigQueryValueCheckOperator", args={"sql": "select 1", "pass_value": 2, "tolerance": 0.1})
    p = tmp_path / "t2"
    p.mkdir()
    assert _convert(p, ir_of(t2), "pkg194_bqtol")["dags"]["d1"]["output"] == "python"


# ---- credentials are never written, and the report names the variables ---------------------------------------------------------------------
def test_no_credential_value_reaches_a_resource_field_and_the_report_names_the_variables_to_set(tmp_path, reg194):
    ops = [op("a", "SlackAPIPostOperator", args={"slack_conn_id": "s1", "channel": "#c", "text": "t"}),
           op("b", "SSHOperator", up=["a"], args={"ssh_conn_id": "h1", "command": "true"}),
           op("c", "DatabricksRunNowOperator", up=["b"], args={"databricks_conn_id": "d1", "json": {"job_id": 1}}),
           op("d", "SQLExecuteQueryOperator", up=["c"], args={"conn_id": "q1", "sql": "select 1"}),
           op("e", "HttpOperator", up=["d"], args={"http_conn_id": "w1", "method": "GET", "endpoint": "x"})]
    rep = _convert(tmp_path, ir_of(*ops), "pkg194_cred")["dags"]["d1"]
    yml = _yml(tmp_path, "pkg194_cred")
    for line in yml.splitlines():
        if line.strip().startswith(("token", "password", "private_key", "workspace_token", "auth_header", "webhook", "host_env", "username_env", "connection_url")):
            assert line.split(":", 1)[0].strip().endswith("_env_var") or "{{ env." in line, line      # a name of a variable (or a template that reads one), never a value
    assert all(w in " ".join(c["reason"] for c in rep["caveats"]) for w in ("S1_TOKEN", "H1_HOST", "D1_TOKEN", "Q1_URL", "W1_AUTH_HEADER"))      # the variables to set are named in the report
    assert sorted(rep["registry_resources"]) == ["d1__jobs", "h1__ssh", "q1__sql", "s1__slack", "w1"]
    assert rep["registry_resource_env"] == {"d1__jobs": ["D1_HOST", "D1_TOKEN"], "h1__ssh": ["H1_HOST", "H1_USER"], "q1__sql": ["Q1_URL"], "s1__slack": ["S1_TOKEN"], "w1": ["W1_AUTH_HEADER (optional)", "W1_BASE_URL"]}
