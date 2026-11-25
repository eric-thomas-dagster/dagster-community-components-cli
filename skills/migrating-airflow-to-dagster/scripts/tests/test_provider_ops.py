"""Provider operators (provider_ops.py): each converted DAG is loaded and run against a local stand-in for the system (HTTP server, SQLite, SMTP
server) or a recording fake of the integration library's client. What these tests do not prove is stated in reference/conversion.md."""
import http.server
import json
import socketserver
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import convert_ir as C  # noqa: E402
import provider_ops as P  # noqa: E402
from test_convert_ir import _convert_and_load, _py, dag, task  # noqa: E402

dg = pytest.importorskip("dagster")
FIXTURE = HERE / "fixtures_providers" / "ir.json"


def ir_of(*tasks, kind="once"):
    return {"airflow_version": "2.10.5", "dags": [dag(list(tasks), kind=kind)]}


def op(tid, ty, up=(), args=None, tf=None, **kw):
    return task(tid, ty, up=list(up), args=args or {}, template_fields=tf or {}, callable_args=kw.pop("callable_args", {}), **kw)


# ---- a recording HTTP server -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def web():
    seen, script = [], []

    class H(http.server.BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(n).decode()})
            status, body = script.pop(0) if script else (200, "ok")
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body.encode())
        do_GET = do_POST = do_PUT = do_HEAD = _go

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url, srv.seen, srv.script = f"http://127.0.0.1:{srv.server_address[1]}", seen, script
    yield srv
    srv.shutdown()


# ---- the converter's own output for a real Airflow IR ----------------------------------------------------------------------------------

def test_real_airflow_ir_converts_loads_and_reports_each_operator(tmp_path):
    """tests/fixtures_providers/ir.json was written by airflow_ir.py from providers_dag.py with Airflow 2.10.5 and the real provider packages."""
    pytest.importorskip("dagster_k8s"), pytest.importorskip("dagster_dbt"), pytest.importorskip("dagster_airbyte"), pytest.importorskip("dagster_databricks")
    ir = json.loads(FIXTURE.read_text())
    ir["dags"][0]["dag_id"] = "d1"
    rep, mod = _convert_and_load(tmp_path, ir, "pkg_real_ir")
    caveats = {(c["task"], c["kind"]) for c in rep["caveats"]}
    stubs = {t for t, k in caveats if k == "stub"}
    assert stubs == set()                                             # BigQuery query jobs, SSH and Docker now convert (provider_ops_more.py); anything they cannot carry is a loud stub
    assert {t for t, k in caveats if k == "degraded"} >= {"crunch", "dbt", "airbyte", "dbr_now", "dbr_submit", "bq", "ssh", "dock"}
    assert sorted(rep["requirements"]) == ["croniter", "dagster-airbyte", "dagster-databricks", "dagster-dbt", "dagster-gcp", "dagster-k8s", "dagster-ssh", "docker", "requests", "sqlalchemy"]
    full = json.loads((tmp_path / "r.json").read_text())
    assert set(full["connections"]) == {"partner_api", "warehouse_pg", "smtp_default", "slack_alerts", "dbt_cloud_default", "airbyte_default", "databricks_default", "google_cloud_default", "edge_host"}
    assert "PARTNER_API_BASE_URL" in full["connections"]["partner_api"]["environment"]
    assert full["operators"]["HttpOperator"]["mapped_by_converter"] and full["operators"]["KubernetesPodOperator"]["mapped_by_converter"]
    assert full["operators"]["SSHOperator"]["mapped_by_converter"] and full["operators"]["DockerOperator"]["mapped_by_converter"]
    res = tmp_path / "pkg_real_ir" / "defs" / "resources.py"
    text = res.read_text()
    assert "DbtCloudClientResource(auth_token=dg.EnvVar('DBT_CLOUD_DEFAULT_API_TOKEN')" in text and "disable_schedule_on_trigger=False" in text
    assert "password=" in text and "EnvVar" in text and "secret" not in text.lower()          # credentials only as environment variable names
    import importlib
    resources_mod = importlib.import_module("pkg_real_ir.defs.resources")
    assets = [getattr(mod, n) for n in dir(mod) if n.startswith("t_")]
    defs = dg.Definitions(assets=assets, jobs=[mod.d1_job], resources=resources_mod.resources().resources)
    defs.validate_loadable(defs)


# ---- HTTP ------------------------------------------------------------------------------------------------------------------------------

def _http_args(**k):
    return {"http_conn_id": "partner_api", "method": "GET", "extra_options": {}, **k}


def test_http_operator_sends_the_request_and_passes_the_body_on(tmp_path, web, monkeypatch):
    monkeypatch.setenv("PARTNER_API_BASE_URL", web.url + "/api")
    monkeypatch.setenv("PARTNER_API_AUTH_HEADER", "Bearer abc")
    web.script.append((200, "hello"))
    get = op("get", "HttpOperator", args=_http_args(endpoint="v1/items", data={"limit": "10"}, headers={"X-A": "1"}),
             tf={"endpoint": "v1/items", "data": {"limit": "10"}, "headers": {"X-A": "1"}})
    use = _py("use", "capture", up=["get"]) | {"op_args": [{"xcom_from": "get", "key": "return_value"}]}
    rep, mod = _convert_and_load(tmp_path, ir_of(get, use), "pkg_http1")
    res = dg.materialize([mod.t_get, mod.t_use])
    assert res.success
    r = web.seen[0]
    assert (r["method"], r["path"]) == ("GET", "/api/v1/items?limit=10")           # base URL and endpoint joined with one slash; GET data are query parameters
    assert r["headers"]["X-A"] == "1" and r["headers"]["Authorization"] == "Bearer abc"
    assert res.asset_value(dg.AssetKey(["d1", "get"])) == "hello"


def test_http_post_sends_the_data_as_the_body_and_an_error_status_fails_the_step(tmp_path, web, monkeypatch):
    monkeypatch.setenv("PARTNER_API_BASE_URL", web.url)
    post = op("post", "HttpOperator", args=_http_args(method="POST", endpoint="/submit", data="payload=1"), tf={"endpoint": "/submit", "data": "payload=1", "headers": {}})
    rep, mod = _convert_and_load(tmp_path, ir_of(post), "pkg_http2")
    assert dg.materialize([mod.t_post]).success
    assert (web.seen[0]["method"], web.seen[0]["path"], web.seen[0]["body"]) == ("POST", "/submit", "payload=1")
    web.script.append((500, "no"))
    res = dg.materialize([mod.t_post], raise_on_error=False)
    assert not res.success                                                         # HTTP 500 fails the task, as HttpHook.check_response does
    ok = op("ok", "HttpOperator", args=_http_args(endpoint="x", extra_options={"check_response": False}), tf={"endpoint": "x", "data": None, "headers": None})
    rep, mod2 = _convert_and_load(tmp_path / "b", ir_of(ok), "pkg_http2b") if (tmp_path / "b").mkdir() is None else None
    web.script.append((500, "no"))
    assert dg.materialize([mod2.t_ok]).success                                     # check_response=False keeps going


def test_http_sensor_polls_until_the_endpoint_answers_and_a_missing_env_var_is_a_clear_failure(tmp_path, web, monkeypatch):
    s = op("wait", "HttpSensor", args=_http_args(response_error_codes_allowlist=["404"]), tf={"endpoint": "ready", "request_params": {}, "headers": {}},
           sensor={"poke_interval": 0.01, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False})
    rep, mod = _convert_and_load(tmp_path, ir_of(s), "pkg_http3")
    res = dg.materialize([mod.t_wait], raise_on_error=False)
    assert not res.success and "PARTNER_API_BASE_URL" in str([e.event_specific_data for e in res.get_step_failure_events()])
    monkeypatch.setenv("PARTNER_API_BASE_URL", web.url)
    web.script.extend([(404, "not yet"), (404, "not yet"), (200, "up")])
    assert dg.materialize([mod.t_wait]).success
    assert [r["path"] for r in web.seen] == ["/ready"] * 3                         # two 404s mean not yet, the third request ends the wait
    web.script.append((503, "down"))
    assert not dg.materialize([mod.t_wait], raise_on_error=False).success         # any other error status fails


@pytest.mark.parametrize("ty,extra,why", [
    ("HttpOperator", {"callable_args": {"response_check": "mod.check"}}, "response_check is a function"),
    ("HttpOperator", {"callable_args": {"response_filter": "mod.f"}}, "response_filter"),
    ("HttpSensor", {"callable_args": {"response_check": "mod.check"}}, "response_check"),
    ("HttpOperator", {"args": _http_args(endpoint="x", extra_options={"proxies": {}})}, "extra_options"),
    ("HttpOperator", {"args": _http_args(endpoint="x", auth_type="HTTPBasicAuth")}, "auth_type"),
])
def test_http_operators_with_a_function_or_an_unsupported_option_become_loud_stubs(tmp_path, ty, extra, why):
    t = op("t", ty, args=extra.pop("args", None) or _http_args(endpoint="x"), tf={"endpoint": "x"}, **extra,
           **({"sensor": {"poke_interval": 1, "timeout": 5, "mode": "poke", "soft_fail": False}} if ty == "HttpSensor" else {}))
    rep, mod = _convert_and_load(tmp_path, ir_of(t), "pkg_http_stub")
    stub = [c for c in rep["caveats"] if c["kind"] == "stub"]
    assert len(stub) == 1 and why in stub[0]["reason"]
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert not res.success and "AIRFLOW-CAVEAT" in str([e.event_specific_data for e in res.get_step_failure_events()])
    with pytest.raises(C.Defer, match=why):
        C.CAVEATS = "off"
        try:
            C.convert_dag(dag([t], kind="manual"), "n", {})
        finally:
            C.CAVEATS = "raise"


# ---- SQL on other databases (SQLite stands in for the database; the URL comes from the environment) --------------------------------------

def test_sql_operator_runs_statements_in_one_transaction_binds_parameters_and_returns_the_rows(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    sqlite3.connect(db).executescript("create table t(d text, n int);")
    monkeypatch.setenv("WAREHOUSE_PG_URL", f"sqlite:///{db}")
    ins = op("ins", "SQLExecuteQueryOperator", args={"conn_id": "warehouse_pg", "autocommit": False, "parameters": {"d": "2024-01-01"}},
             tf={"sql": ["insert into t values (:d, 1)", "insert into t values (:d, 2)"], "conn_id": "warehouse_pg", "parameters": {"d": "2024-01-01"}, "database": None, "hook_params": {}},
             callable_args={"handler": "airflow.providers.common.sql.hooks.sql.fetch_all_handler"})
    ins["args"]["parameters"] = {"d": "2024-01-01"}
    sel = op("sel", "PostgresOperator", up=["ins"], args={"postgres_conn_id": "warehouse_pg"}, tf={"sql": "select n from t order by n", "postgres_conn_id": "warehouse_pg"})
    use = _py("use", "capture", up=["sel"]) | {"op_args": [{"xcom_from": "sel", "key": "return_value"}]}
    rep, mod = _convert_and_load(tmp_path, ir_of(ins, sel, use), "pkg_sql1")
    assert rep["provider_connections"] == {"warehouse_pg": "sql"} and rep["status"] == "converted"
    res = dg.materialize([mod.t_ins, mod.t_sel, mod.t_use])
    assert res.success
    assert res.asset_value(dg.AssetKey(["d1", "sel"])) == [[1], [2]]               # the rows of the last statement that returned any, as lists
    assert sqlite3.connect(db).execute("select count(*) from t").fetchone() == (2,)


def test_sql_failure_rolls_the_whole_transaction_back_and_autocommit_keeps_earlier_statements(tmp_path, monkeypatch):
    db = tmp_path / "w.db"
    sqlite3.connect(db).executescript("create table t(n int unique);")
    monkeypatch.setenv("WAREHOUSE_PG_URL", f"sqlite:///{db}")
    sql = ["insert into t values (1)", "insert into t values (1)"]               # the second violates the unique constraint
    mk = lambda ac: op("q", "SQLExecuteQueryOperator", args={"conn_id": "warehouse_pg", "autocommit": ac}, tf={"sql": sql, "conn_id": "warehouse_pg", "database": None, "hook_params": {}})
    rep, mod = _convert_and_load(tmp_path / "a", ir_of(mk(False)), "pkg_sql2") if (tmp_path / "a").mkdir() is None else None
    assert not dg.materialize([mod.t_q], raise_on_error=False).success
    assert sqlite3.connect(db).execute("select count(*) from t").fetchone() == (0,)          # nothing committed
    rep, mod2 = _convert_and_load(tmp_path / "b", ir_of(mk(True)), "pkg_sql3") if (tmp_path / "b").mkdir() is None else None
    assert not dg.materialize([mod2.t_q], raise_on_error=False).success
    assert sqlite3.connect(db).execute("select count(*) from t").fetchone() == (1,)          # autocommit: the first statement stays


@pytest.mark.parametrize("extra,why", [
    ({"callable_args": {"handler": "mypkg.custom_handler"}}, "handler"),
    ({"args": {"conn_id": "c", "database": "other"}, "tf": {"sql": "select 1", "database": "other"}}, "database='other'"),
    ({"args": {"conn_id": "c", "output_processor": None}, "tf": {"sql": "select 1", "hook_params": {"x": 1}}}, "hook_params"),
])
def test_sql_options_that_change_the_result_make_a_loud_stub(tmp_path, extra, why):
    t = op("q", "SQLExecuteQueryOperator", args=extra.get("args") or {"conn_id": "c"}, tf=extra.get("tf") or {"sql": "select 1", "conn_id": "c"},
           callable_args=extra.get("callable_args") or {})
    rep, mod = _convert_and_load(tmp_path, ir_of(t), "pkg_sql_stub")
    assert [c for c in rep["caveats"] if c["kind"] == "stub" and why in c["reason"]]
    assert not dg.materialize([mod.t_q], raise_on_error=False).success


# ---- email: a minimal SMTP server records the message --------------------------------------------------------------------------------

@pytest.fixture
def smtp():
    got = []

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            w = lambda s: self.wfile.write((s + "\r\n").encode())
            w("220 test")
            rcpt, data = [], []
            while True:
                line = self.rfile.readline().decode().strip()
                cmd = line.upper()
                if cmd.startswith(("EHLO", "HELO")):
                    w("250 ok")
                elif cmd.startswith("MAIL"):
                    w("250 ok")
                elif cmd.startswith("RCPT"):
                    rcpt.append(line.split(":", 1)[1].strip(" <>"))
                    w("250 ok")
                elif cmd == "DATA":
                    w("354 go")
                    while (l := self.rfile.readline().decode()) not in (".\r\n", ""):
                        data.append(l)
                    got.append({"rcpt": rcpt, "data": "".join(data)})
                    w("250 queued")
                elif cmd == "QUIT":
                    w("221 bye")
                    return
                else:
                    w("250 ok")

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.got, srv.port = got, srv.server_address[1]
    yield srv
    srv.shutdown()


def test_email_operator_sends_one_html_message_with_the_rendered_subject_and_a_hidden_bcc(tmp_path, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_DEFAULT_HOST", "127.0.0.1")
    monkeypatch.setenv("SMTP_DEFAULT_PORT", str(smtp.port))
    monkeypatch.setenv("SMTP_DEFAULT_FROM", "airflow@example.com")
    attach = tmp_path / "report.csv"
    attach.write_text("a,b\n1,2\n")
    m = op("mail", "EmailOperator", args={"conn_id": "smtp_default", "mime_subtype": "mixed", "mime_charset": "utf-8"},
           tf={"to": ["a@example.com", "b@example.com"], "subject": "done {{ ds }}", "html_content": "<b>ok</b>", "files": [str(attach)], "cc": "c@example.com", "bcc": ["h@example.com"], "from_email": None})
    rep, mod = _convert_and_load(tmp_path, ir_of(m), "pkg_mail")
    assert dg.materialize([mod.t_mail], tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"}).success
    msg = smtp.got[0]
    assert msg["rcpt"] == ["a@example.com", "b@example.com", "c@example.com", "h@example.com"]
    import email
    parsed = email.message_from_string(msg["data"])
    assert parsed["Subject"] == "done 2026-07-02" and parsed["From"] == "airflow@example.com" and parsed["Cc"] == "c@example.com"
    parts = list(parsed.walk())
    html = next(p for p in parts if p.get_content_type() == "text/html")
    assert html.get_payload(decode=True) == b"<b>ok</b>"
    assert "h@example.com" not in msg["data"]                                                       # Bcc stays out of the headers
    assert next(p for p in parts if p.get_filename() == "report.csv").get_payload(decode=True) == b"a,b\n1,2\n"
    monkeypatch.delenv("SMTP_DEFAULT_HOST")
    assert not dg.materialize([mod.t_mail], raise_on_error=False).success                          # no host: a clear failure, not a silent success


# ---- Slack webhook ---------------------------------------------------------------------------------------------------------------------

def test_slack_webhook_operator_posts_the_message_as_json(tmp_path, web, monkeypatch):
    monkeypatch.setenv("SLACK_ALERTS_WEBHOOK_URL", web.url + "/hook")
    s = op("slack", "SlackWebhookOperator", args={"slack_webhook_conn_id": "slack_alerts"}, tf={"message": "load {{ ds }}", "channel": "#data", "username": None, "blocks": None, "attachments": None, "proxy": None})
    rep, mod = _convert_and_load(tmp_path, ir_of(s), "pkg_slack")
    assert dg.materialize([mod.t_slack], tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"}).success
    r = web.seen[0]
    assert r["path"] == "/hook" and json.loads(r["body"]) == {"text": "load 2026-07-02", "channel": "#data"}
    web.script.append((400, "invalid_payload"))
    assert not dg.materialize([mod.t_slack], raise_on_error=False).success
    stub = op("slack", "SlackWebhookOperator", args={"slack_webhook_conn_id": "x"}, tf={"message": "m", "proxy": "http://proxy"})
    rep, mod2 = _convert_and_load(tmp_path / "b", ir_of(stub), "pkg_slack2") if (tmp_path / "b").mkdir() is None else None
    assert [c for c in rep["caveats"] if c["kind"] == "stub" and "proxy" in c["reason"]]


# ---- Kubernetes: the real execute_k8s_job builds the real Job; only the cluster calls are replaced --------------------------------------

@pytest.fixture
def k8s(monkeypatch):
    kubernetes = pytest.importorskip("kubernetes")
    pytest.importorskip("dagster_k8s")
    import types
    from dagster_k8s.client import DagsterKubernetesClient
    jobs = []
    pod = types.SimpleNamespace(metadata=types.SimpleNamespace(name="pod-1"))
    api = types.SimpleNamespace(batch_api=types.SimpleNamespace(create_namespaced_job=lambda ns, job: jobs.append((ns, job))),
                                core_api=types.SimpleNamespace(read_namespaced_pod_log=None), wait_for_job=lambda **k: None,
                                wait_for_job_to_have_pods=lambda *a, **k: [pod], wait_for_pod=lambda *a, **k: None,
                                wait_for_running_job_to_succeed=lambda **k: None, get_pod_names_in_job=lambda **k: [], delete_job=lambda **k: None)

    class Watch:
        def stream(self, *a, **k):
            return iter(["a log line"])

        def stop(self):
            pass
    monkeypatch.setattr(DagsterKubernetesClient, "production_client", staticmethod(lambda: api))
    monkeypatch.setattr(kubernetes.watch, "Watch", Watch)
    loaded = []
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: loaded.append("incluster"))
    monkeypatch.setattr(kubernetes.config, "load_kube_config", lambda f=None, context=None: loaded.append(("kubeconfig", f, context)))
    return types.SimpleNamespace(jobs=jobs, loaded=loaded)


def _kpo(**k):
    a = {"image": "registry.example.com/crunch:1.2", "cmds": ["python", "-m", "crunch"], "arguments": ["--day", "{{ ds }}"], "namespace": "batch",
         "env_vars": [{"repr": "{'name': 'MODE', 'value': 'full', 'value_from': None}"}], "service_account_name": "crunch-sa", "in_cluster": True,
         "labels": {"team": "data"}, "image_pull_policy": "IfNotPresent", "image_pull_secrets": [{"repr": "{'name': 'regcred'}"}],
         "container_resources": {"repr": "{'limits': {'cpu': '1', 'memory': '2Gi'}, 'requests': None, 'claims': None}"},
         "node_selector": {"pool": "batch"}, "env_from": [{"repr": "{'config_map_ref': {'name': 'app-cfg', 'optional': None}, 'prefix': None, 'secret_ref': None}"}],
         "volumes": [], "volume_mounts": [], "do_xcom_push": False, "get_logs": True, "on_finish_action": "delete_pod", "kubernetes_conn_id": "kubernetes_default",
         "startup_timeout_seconds": 120, **k}
    return op("crunch", "KubernetesPodOperator", args=a, tf={k_: a[k_] for k_ in ("image", "cmds", "arguments", "env_vars", "namespace", "labels") if k_ in a})


def test_kubernetes_pod_operator_builds_the_job_for_the_image_command_env_and_pod_settings(tmp_path, k8s):
    rep, mod = _convert_and_load(tmp_path, ir_of(_kpo()), "pkg_k8s1")
    assert [c for c in rep["caveats"] if c["kind"] == "degraded" and "UNVERIFIED against a live Kubernetes cluster" in c["reason"]]
    res = dg.materialize([mod.t_crunch], tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"})
    assert res.success and k8s.loaded == ["incluster"]
    ns, job = k8s.jobs[0]
    pod = job.spec.template.spec
    c = pod.containers[0]
    assert ns == "batch" and c.image == "registry.example.com/crunch:1.2" and c.command == ["python", "-m", "crunch"]
    assert c.args[-2:] == ["--day", "2026-07-02"]                                  # the template is rendered
    assert {"name": "MODE", "value": "full"} in [{"name": e.name, "value": e.value} for e in c.env]
    assert c.image_pull_policy == "IfNotPresent" and c.resources.limits == {"cpu": "1", "memory": "2Gi"}
    assert pod.service_account_name == "crunch-sa" and pod.node_selector == {"pool": "batch"} and pod.image_pull_secrets[0].name == "regcred"
    assert any(e.config_map_ref and e.config_map_ref.name == "app-cfg" for e in (c.env_from or []))
    assert job.spec.template.metadata.labels["team"] == "data"


def test_kubernetes_pod_operator_uses_a_kubeconfig_context_and_refuses_what_it_cannot_carry(tmp_path, k8s):
    rep, mod = _convert_and_load(tmp_path, ir_of(_kpo(in_cluster=False, config_file="/etc/kube/config", cluster_context="prod")), "pkg_k8s2")
    assert dg.materialize([mod.t_crunch], tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"}).success
    assert k8s.loaded == [("kubeconfig", "/etc/kube/config", "prod")]
    for i, (extra, why) in enumerate([({"do_xcom_push": True}, "do_xcom_push"), ({"ports": [{"repr": "{'container_port': 80}"}]}, "ports"),
                                      ({"full_pod_spec": {"repr": "{'api_version': 'v1'}"}}, "full_pod_spec"),
                                      ({"env_vars": [{"repr": "{'name': 'S', 'value': None, 'value_from': {'secret_key_ref': {'name': 'k', 'key': 'k'}}}"}]}, "value_from"),
                                      ({"image": {"repr": "<not a string>"}}, "image")]):
        sub = tmp_path / f"s{i}"
        sub.mkdir()
        rep, mod = _convert_and_load(sub, ir_of(_kpo(**extra)), f"pkg_k8s_stub{i}")
        assert [c for c in rep["caveats"] if c["kind"] == "stub" and why in c["reason"]], (why, rep["caveats"])
        assert not dg.materialize([mod.t_crunch], raise_on_error=False).success


def test_a_mapped_provider_operator_is_a_stub_and_with_caveats_off_the_dag_is_deferred(tmp_path):
    t = op("get", "HttpOperator", args=_http_args(endpoint="x"), tf={"endpoint": "x"}, is_mapped=True,
           expand={"form": "expand", "args": {"endpoint": {"kind": "literal", "value": ["a", "b"]}}, "fixed": {}})
    rep, mod = _convert_and_load(tmp_path, ir_of(t), "pkg_mapped_http")
    assert [c for c in rep["caveats"] if c["kind"] == "stub" and "mapped HttpOperator" in c["reason"]]
    C.CAVEATS = "off"
    try:
        with pytest.raises(C.Defer, match="mapped"):
            C.convert_dag(dag([t], kind="manual"), "n", {})
    finally:
        C.CAVEATS = "raise"


# ---- integration libraries: a recording fake stands in for the service --------------------------------------------------------------------

def _binds(fn, *a, **k):
    """The call the generated code made fits the real library signature (the fakes below accept anything)."""
    import inspect
    inspect.signature(fn).bind(None, *a, **k)


class _Fake:
    def __init__(self, **kw):
        self.calls = []
        self.__dict__.update(kw)

    def rec(self, name, *a, **k):
        self.calls.append((name, a, k))


def test_dbt_cloud_job_triggers_with_overrides_and_waits_without_touching_the_schedule(tmp_path):
    pytest.importorskip("dagster_dbt")
    fake = _Fake()
    client = _Fake()
    client.run_job = lambda job_id, **body: (client.rec("run_job", job_id, **body), {"id": 99})[1]
    client.poll_run = lambda run_id, **k: client.rec("poll_run", run_id, **k)
    fake.get_dbt_client = lambda: client
    t = op("dbt", "DbtCloudRunJobOperator", args={"dbt_cloud_conn_id": "dbt_cloud_default", "wait_for_termination": True, "check_interval": 15, "timeout": 3600},
           tf={"job_id": 1234, "trigger_reason": "airflow", "steps_override": ["dbt build"], "schema_override": None, "additional_run_config": {"threads_override": 8}})
    rep, mod = _convert_and_load(tmp_path, ir_of(t), "pkg_dbt")
    assert dg.materialize([mod.t_dbt], resources={"dbt_cloud_default": fake}).success
    assert client.calls == [("run_job", (1234,), {"cause": "airflow", "steps_override": ["dbt build"], "threads_override": 8}),
                            ("poll_run", (99,), {"poll_interval": 15.0, "poll_timeout": 3600.0})]
    assert "disable_schedule_on_trigger=False" in (tmp_path / "pkg_dbt" / "defs" / "resources.py").read_text()
    from dagster_dbt.cloud.resources import DbtCloudClient
    for name, a, k in client.calls:
        _binds(getattr(DbtCloudClient, name), *a, **k)


def test_airbyte_and_fivetran_syncs_wait_unless_asynchronous(tmp_path):
    pytest.importorskip("dagster_airbyte"), pytest.importorskip("dagster_fivetran")
    ab, fv = _Fake(), _Fake()
    ab.sync_and_poll = lambda cid, **k: (ab.rec("sync_and_poll", cid, **k), _Fake(job_details={"job": {"id": 7}}))[1]
    ab.start_sync = lambda cid: (ab.rec("start_sync", cid), {"job": {"id": 8}})[1]
    fv.sync_and_poll = lambda cid, **k: fv.rec("sync_and_poll", cid, **k)
    a1 = op("ab", "AirbyteTriggerSyncOperator", args={"airbyte_conn_id": "airbyte_default", "asynchronous": False, "timeout": 1800, "wait_seconds": 10}, tf={"connection_id": "c-1"})
    a2 = op("ab2", "AirbyteTriggerSyncOperator", args={"airbyte_conn_id": "airbyte_default", "asynchronous": True}, tf={"connection_id": "c-2"})
    f1 = op("fv", "FivetranOperator", args={"fivetran_conn_id": "fivetran", "poll_frequency": 5, "schedule_type": "manual", "timeout_seconds": 600}, tf={"connector_id": "conn_x"})
    rep, mod = _convert_and_load(tmp_path, ir_of(a1, a2, f1), "pkg_sync")
    assert dg.materialize([mod.t_ab, mod.t_ab2, mod.t_fv], resources={"airbyte_default": ab, "fivetran": fv}).success
    assert ab.calls == [("sync_and_poll", ("c-1",), {"poll_interval": 10.0, "poll_timeout": 1800.0}), ("start_sync", ("c-2",), {})]
    assert fv.calls == [("sync_and_poll", ("conn_x",), {"poll_interval": 5.0, "poll_timeout": 600})]
    from dagster_airbyte import AirbyteResource
    from dagster_fivetran import FivetranResource
    for name, a, k in ab.calls:
        _binds(getattr(AirbyteResource, name), *a, **k)
    for name, a, k in fv.calls:
        _binds(getattr(FivetranResource, name), *a, **k)
    text = (tmp_path / "pkg_sync" / "defs" / "resources.py").read_text()
    assert "AirbyteResource(" in text and "FivetranResource(" in text
    bad = op("fv", "FivetranOperator", args={"fivetran_conn_id": "fivetran", "schedule_type": "daily"}, tf={"connector_id": "x"})
    sub = tmp_path / "b"
    sub.mkdir()
    rep, _ = _convert_and_load(sub, ir_of(bad), "pkg_sync2")
    assert [c for c in rep["caveats"] if c["kind"] == "stub" and "schedule_type" in c["reason"]]


def test_databricks_run_now_and_submit_post_the_same_json_and_wait(tmp_path):
    pytest.importorskip("dagster_databricks")
    posts, waits = [], []
    api = _Fake()
    api.do = lambda method, path, body=None: (posts.append((method, path, body)), {"run_id": 55})[1]
    client = _Fake(workspace_client=_Fake(api_client=api))
    client.wait_for_run_to_complete = lambda **k: waits.append(k)
    res_ = _Fake(get_client=lambda: client)
    now = op("now", "DatabricksRunNowOperator", args={"databricks_conn_id": "databricks_default", "polling_period_seconds": 20, "wait_for_termination": True,
                                                     "json": {"job_id": "77", "notebook_params": {"day": "{{ ds }}"}}}, tf={"json": {"job_id": "77", "notebook_params": {"day": "{{ ds }}"}}})
    sub_ = op("sub", "DatabricksSubmitRunOperator", args={"databricks_conn_id": "databricks_default", "wait_for_termination": False,
                                                         "json": {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}}}, tf={"json": {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}}})
    rep, mod = _convert_and_load(tmp_path, ir_of(now, sub_), "pkg_dbx")
    assert dg.materialize([mod.t_now, mod.t_sub], resources={"databricks_default": res_}, tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"}).success
    assert posts == [("POST", "/api/2.1/jobs/run-now", {"job_id": 77, "notebook_params": {"day": "2026-07-02"}}),
                     ("POST", "/api/2.1/jobs/runs/submit", {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}})]
    assert len(waits) == 1 and waits[0]["databricks_run_id"] == 55 and waits[0]["poll_interval_sec"] == 20.0      # the submit run does not wait
    from dagster_databricks import DatabricksClient
    from databricks.sdk.core import ApiClient
    _binds(DatabricksClient.wait_for_run_to_complete, **waits[0])
    for method, path, body in posts:
        _binds(ApiClient.do, method, path, body=body)
    stub = op("now", "DatabricksRunNowOperator", args={"databricks_conn_id": "d", "json": {"job_name": "nightly"}}, tf={"json": {"job_name": "nightly"}})
    d2 = tmp_path / "b"
    d2.mkdir()
    rep, _ = _convert_and_load(d2, ir_of(stub), "pkg_dbx2")
    assert [c for c in rep["caveats"] if c["kind"] == "stub" and "job_name" in c["reason"]]


# ---- --output components: registry components instead of generated Python -------------------------------------------------------------

def _components_output(tmp_path, ir, pkg):
    import subprocess
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"),
                    "--report", str(tmp_path / "r.json"), "--output", "components"], check=True, capture_output=True)
    return json.loads((tmp_path / "r.json").read_text())


def _component_defs(text):
    """Each document of the generated defs.yaml built with the registry's own component class: an invented or misspelled field fails validation here."""
    import yaml
    import dagster_community_components as reg
    out = []
    for doc in yaml.safe_load_all(text):
        cls = getattr(reg, doc["type"].rsplit(".", 1)[1])
        out.append((doc["type"], cls(**doc["attributes"]).build_defs(None)))
    return out


def test_sql_and_wait_steps_become_registry_components_that_validate_and_run(tmp_path, monkeypatch):
    pytest.importorskip("dagster_community_components")
    db = tmp_path / "w.db"
    sqlite3.connect(db).executescript("create table t(d text);")
    monkeypatch.setenv("WAREHOUSE_PG_URL", f"sqlite:///{db}")
    sn = {"poke_interval": 0.1, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False}
    wait = op("wait", "BashSensor", args={"retry_exit_code": 3}, tf={"bash_command": "exit 0", "env": None}, sensor=sn)
    clock = op("clock", "TimeSensor", up=["wait"], sensor={**sn, "target_datetime": "2000-01-01T06:00:00+00:00"})
    ins = op("ins", "SQLExecuteQueryOperator", up=["clock"], retries=2, args={"conn_id": "warehouse_pg", "autocommit": False, "parameters": {"d": "x"}},
             tf={"sql": ["insert into t values (:d)"], "conn_id": "warehouse_pg", "parameters": {"d": "x"}, "database": None, "hook_params": {}},
             callable_args={"handler": "airflow.providers.common.sql.hooks.sql.fetch_all_handler"})
    rep = _components_output(tmp_path, ir_of(wait, clock, ins), "pkg_comp1")
    d = rep["dags"]["d1"]
    assert d["output"] == "components" and d["task_keys"] == {"wait": "d1__wait", "clock": "d1__clock", "ins": "d1__ins"}
    assert rep["connections"]["warehouse_pg"]["read_by"] == "the step, when it runs"
    text = (tmp_path / "pkg_comp1" / "defs" / "d1" / "defs.yaml").read_text()
    for needle in ("WaitUntilAssetComponent", 'condition: "shell_exit_zero"', "retry_exit_code: 3", 'condition: "time_of_day"', 'time_of_day: "00:00"', 'timezone: "America/Chicago"',   # 06:00 UTC is midnight in the DAG's zone
                   
                   "SqlAssetComponent", 'connection_env: "WAREHOUSE_PG_URL"', "retry_policy_max_retries: 2"):
        assert needle in text, needle
    built = _component_defs(text)
    assets = [a for typ, defs in built if not typ.endswith("CronScheduleComponent") for a in defs.assets]
    assert len(assets) == 3
    assert dg.materialize(assets).success
    assert sqlite3.connect(db).execute("select d from t").fetchall() == [("x",)]


@pytest.mark.parametrize("mut,why", [
    (lambda t: t["args"].update(autocommit=True) or t["template_fields"].update(sql=["select 1", "select 2"]), "autocommit with several statements"),
    (lambda t: t["args"].update(parameters=["a"]) or t["template_fields"].update(parameters=["a"]), "positional parameters"),
    (lambda t: t["template_fields"].update(sql=["select '{{ ds }}'"]), "a template"),
    (lambda t: t["args"].update(split_statements=True), "split_statements"),
])
def test_a_sql_task_the_component_cannot_carry_keeps_the_dag_as_python(tmp_path, mut, why):
    t = op("q", "SQLExecuteQueryOperator", args={"conn_id": "c", "autocommit": False}, tf={"sql": ["select 1"], "conn_id": "c", "database": None, "hook_params": {}},
           callable_args={"handler": "x.fetch_all_handler"})
    mut(t)
    rep = _components_output(tmp_path, ir_of(t), "pkg_comp2")
    assert rep["dags"]["d1"]["status"] == "converted" and rep["dags"]["d1"]["output"] == "python", why
    assert not (tmp_path / "pkg_comp2" / "defs" / "d1" / "defs.yaml").exists()


def test_a_bash_sensor_without_retry_exit_code_stays_python(tmp_path):
    sn = {"poke_interval": 1.0, "timeout": 5.0, "mode": "poke", "soft_fail": False, "exponential_backoff": False, "silent_fail": False, "never_fail": False}
    plain = op("w", "BashSensor", args={"retry_exit_code": None}, tf={"bash_command": "exit 0", "env": None}, sensor=sn)
    rep = _components_output(tmp_path, ir_of(plain), "pkg_comp3")
    assert rep["dags"]["d1"]["output"] == "python"                # Airflow polls on any failure here; the component would fail at once
