"""Converter pass 9 (reference/converter-triage.md, "Stage 2a, pass 9"): the pass 7/8 code run against the REAL client libraries.

Earlier passes ran the emitted code against recording fakes because dagster-docker, dagster-k8s, paramiko, slack-sdk, databricks-sdk and the google-cloud clients were not installed in the test
interpreter. Here the real classes are imported and constructed; only the far end of the wire is local: a fake Docker engine (the container "runs" the wrapper script in a subprocess), a fake
Kubernetes API server (a pod "runs" the wrapper script), an in-process paramiko SSH server, a local HTTP server for Slack and Databricks. Where no emulator exists (Dataproc, Pub/Sub, BigQuery
need a gRPC/REST service), the real request types are built and the real client methods' signatures are bound to what the converter passes; the transport call stays a recording fake.
Every test skips cleanly when its library is not installed (the original test interpreter has none of them)."""
import inspect
import json
import os
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import isolation_ops as IS  # noqa: E402,F401
from test_converter_venv import CONTAINERS, convert, load_dag  # noqa: E402


def _bind(fn, *a, **k):
    """The call must be accepted by the real signature (TypeError otherwise)."""
    inspect.signature(fn).bind(*a, **k)


# ---- a container "runs" by executing the command in a subprocess ---------------------------------------------------------------------------------------------------------

def _run_command(command, env):
    """What a container does with `command`: the wrapper script is run by the current interpreter (it has dagster-pipes); stdout is the container log."""
    cmd = [sys.executable if command[0] in ("python3", "python") else command[0], *command[1:]]
    p = subprocess.run(cmd, env={**os.environ, **{str(k): str(v) for k, v in env.items()}}, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout, p.stderr


# ---- Docker: the real PipesDockerClient, a fake engine under `docker.client.from_env` ----------------------------------------------------------------------------------------

@pytest.fixture
def docker_engine(monkeypatch):
    docker = pytest.importorskip("docker")
    pytest.importorskip("dagster_docker")
    from docker.models.containers import _create_container_args
    created = []

    class Container:
        def __init__(self, image, command, environment, **kw):
            # the real docker-py validates the keyword names it will send to the daemon (an unknown one is a TypeError there)
            _create_container_args({"image": image, "command": command, "environment": environment, "version": "1.41", **kw})
            self.code, self.out, self.err = _run_command(list(command), environment)

        def start(self):
            pass

        def logs(self, stdout=True, stderr=True, stream=False, follow=False):
            return iter([line.encode() + b"\n" for line in (self.out + self.err).splitlines()])

        def wait(self):
            return {"StatusCode": self.code}

        def stop(self):
            pass

    class Engine:
        class containers:
            @staticmethod
            def create(image, command=None, **kw):
                created.append({"image": image, "command": command, **kw})
                return Container(image, command, **kw)

        def login(self, **kw):
            pass
    monkeypatch.setattr(docker.client, "from_env", lambda: Engine())
    return created


def test_task_docker_runs_through_the_real_pipes_docker_client(tmp_path, docker_engine):
    ir, rep, pkg = convert(tmp_path, CONTAINERS)
    assert rep["status"] == "converted"
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_in_docker], raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:400] for e in res.get_step_failure_events()]
    c = docker_engine[0]
    assert c["image"] == "python:3.11" and c["network_mode"] == "bridge" and c["environment"]["A"] == "1" and c["detach"] is True       # the real client adds `detach` and the pipes variables
    assert "DAGSTER_PIPES_CONTEXT" in c["environment"] and c["command"][:2] == ["python3", "-c"]
    meta = {e.asset_key.to_user_string(): e.materialization.metadata for e in res.get_asset_materialization_events()}
    assert meta["d/in_docker"]["airflow/return_value"].value == 2          # in_docker(1) returned x + 1 inside the "container" and came back as a Pipes message


def test_pipes_docker_client_signature_takes_everything_the_converter_passes():
    pytest.importorskip("dagster_docker")
    from dagster_docker import PipesDockerClient
    _bind(PipesDockerClient.run, None, context=None, image="i", command=["python3", "-c", "x"], env={"A": "1"}, extras={}, container_kwargs={"network_mode": "bridge", "user": "u", "working_dir": "/w"})
    _bind(PipesDockerClient.__init__, None)
    # the docker-py create arguments the converter forwards are real ones
    from docker.models.containers import _create_container_args
    _create_container_args({"image": "i", "command": ["x"], "environment": {"A": "1"}, "network_mode": "bridge", "user": "u", "working_dir": "/w", "detach": True, "version": "1.41"})


# ---- Kubernetes: the real PipesK8sClient, a fake API server behind a real kubeconfig -----------------------------------------------------------------------------------------

class _K8sApi(BaseHTTPRequestHandler):
    server_version = "fake-k8s"
    pods: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = (obj if isinstance(obj, (bytes, str)) else json.dumps(obj))
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _pod_json(self, p):
        return {"apiVersion": "v1", "kind": "Pod", "metadata": p["body"]["metadata"], "spec": p["body"]["spec"],
                "status": {"phase": "Succeeded" if p["code"] == 0 else "Failed",
                           "containerStatuses": [{"name": p["body"]["spec"]["containers"][0]["name"], "ready": False, "restartCount": 0, "image": "i", "imageID": "x",
                                                  "state": {"terminated": {"exitCode": p["code"], "reason": "Completed" if p["code"] == 0 else "Error"}}}]}}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ns = self.path.split("/")[4]
        cont = body["spec"]["containers"][0]
        env = {e["name"]: e.get("value", "") for e in cont.get("env", [])}
        code, out, err = _run_command(list(cont["command"]) + list(cont.get("args") or []), env)
        self.server.pods[body["metadata"]["name"]] = {"body": body, "ns": ns, "code": code, "log": out + err}
        self.server.created.append({"ns": ns, "body": body})
        self._send(body, 201)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts[-1] == "log":
            p = self.server.pods[parts[-2]]
            self._send("".join(f"2024-03-22T02:17:29.{i:06d}Z {line}\n" for i, line in enumerate(p["log"].splitlines())), ctype="text/plain")      # the kubelet stamps every line differently
        elif parts[-1] == "pods":
            self._send({"apiVersion": "v1", "kind": "PodList", "metadata": {}, "items": [self._pod_json(p) for p in self.server.pods.values()]})
        else:
            self._send({"kind": "Status", "status": "Failure", "code": 404, "reason": "NotFound"}, 404)

    def do_DELETE(self):
        name = self.path.split("?")[0].strip("/").split("/")[-1]
        self.server.deleted.append(name)
        self._send(self._pod_json(self.server.pods[name]))                      # DELETE on a pod answers with the pod (the client deserializes it as one)


@pytest.fixture
def k8s_api(tmp_path, monkeypatch):
    pytest.importorskip("kubernetes")
    pytest.importorskip("dagster_k8s")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _K8sApi)
    srv.pods, srv.created, srv.deleted = {}, [], []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    kc = tmp_path / "kubeconfig"
    kc.write_text(f"""apiVersion: v1
kind: Config
current-context: other
clusters:
- name: c
  cluster: {{server: "http://127.0.0.1:{srv.server_port}"}}
contexts:
- name: ctx
  context: {{cluster: c, user: u, namespace: team-ns}}
- name: other
  context: {{cluster: nowhere, user: u}}
users:
- name: u
  user: {{token: t}}
""")
    srv.kubeconfig = str(kc)
    yield srv
    srv.shutdown()


K8S_CONTAINER = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.kubernetes(image="python:3.11", namespace="jobs", env_vars={"B": "2"}, labels={"team": "x"}, config_file=%(cfg)r, cluster_context="ctx")
        def in_pod(x):
            return x * 2

        in_pod(21)
    d()
'''


def test_task_kubernetes_runs_through_the_real_pipes_k8s_client_and_a_real_kubeconfig(tmp_path, k8s_api):
    ir, rep, pkg = convert(tmp_path, K8S_CONTAINER % {"cfg": k8s_api.kubeconfig})
    assert rep["status"] == "converted", rep["caveats"]
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_in_pod], raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:600] for e in res.get_step_failure_events()]
    made = k8s_api.created[0]
    cont = made["body"]["spec"]["containers"][0]
    assert made["ns"] == "jobs" and cont["image"] == "python:3.11" and {"team": "x"}.items() <= made["body"]["metadata"]["labels"].items()
    assert {"name": "B", "value": "2"} in cont["env"] and any(e["name"] == "DAGSTER_PIPES_CONTEXT" for e in cont["env"])
    assert k8s_api.deleted                                                  # the client cleans the pod up
    meta = {e.asset_key.to_user_string(): e.materialization.metadata for e in res.get_asset_materialization_events()}
    assert meta["d/in_pod"]["airflow/return_value"].value == 42


def test_pipes_k8s_client_signature_and_config_rules():
    pytest.importorskip("dagster_k8s")
    from dagster_k8s import PipesK8sClient
    _bind(PipesK8sClient.run, None, context=None, image="i", command=["x"], namespace="n", env={}, extras={}, base_pod_meta={"labels": {}})
    _bind(PipesK8sClient.__init__, None, kubeconfig_file="/k", kube_context="c")
    _bind(PipesK8sClient.__init__, None, load_incluster_config=True)
    with pytest.raises(Exception, match="should not be set"):                  # the real client refuses an in-cluster config together with a kubeconfig file
        PipesK8sClient(load_incluster_config=True, kubeconfig_file="/k")



# ---- SSH: real paramiko client and resources against a real paramiko server in a thread --------------------------------------------------------------------------------

@pytest.fixture
def ssh_server(tmp_path):
    paramiko = pytest.importorskip("paramiko")
    import socket
    host_key = paramiko.RSAKey.generate(2048)
    seen = []

    class Server(paramiko.ServerInterface):
        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED

        def check_auth_password(self, username, password):
            seen.append(("password", username, password))
            return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            seen.append(("key", username))
            return paramiko.AUTH_SUCCESSFUL

        def get_allowed_auths(self, username):
            return "password,publickey"

        def check_channel_exec_request(self, channel, command):
            seen.append(("exec", command.decode()))
            threading.Thread(target=self._run, args=(channel, command.decode()), daemon=True).start()
            return True

        def _run(self, channel, command):
            p = subprocess.run(["sh", "-c", command], capture_output=True, timeout=30)
            channel.sendall(p.stdout)
            channel.sendall_stderr(p.stderr)
            channel.send_exit_status(p.returncode)
            channel.shutdown_write()
            channel.close()

    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    stop = threading.Event()

    def accept():
        lsock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = lsock.accept()
            except OSError:
                continue
            t = paramiko.Transport(conn)
            t.add_server_key(host_key)
            t.start_server(server=Server())
    threading.Thread(target=accept, daemon=True).start()
    key_path = tmp_path / "id_rsa"
    paramiko.RSAKey.generate(2048).write_private_key_file(str(key_path))
    yield types.SimpleNamespace(port=lsock.getsockname()[1], seen=seen, key_path=str(key_path))
    stop.set()
    lsock.close()


def _generated_resources(pkg):
    """The `resources` definitions the converter wrote (defs/resources.py), imported as the project would."""
    import importlib
    return importlib.import_module(f"{pkg}.defs.resources").resources()


def test_ssh_operator_shim_runs_a_command_over_a_real_paramiko_connection_with_the_real_dagster_ssh_resource(tmp_path, monkeypatch, ssh_server):
    pytest.importorskip("dagster_ssh")
    from test_provider_ops_more import _convert_and_load, dag, task
    monkeypatch.setenv("SSH_DEFAULT_HOST", "127.0.0.1")
    monkeypatch.setenv("SSH_DEFAULT_PORT", str(ssh_server.port))
    monkeypatch.setenv("SSH_DEFAULT_USER", "etl")
    monkeypatch.setenv("SSH_DEFAULT_PASSWORD", "pw")
    cmd = "echo hello; echo warn >&2"
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", "SSHOperator", args={"ssh_conn_id": "ssh_default", "command": cmd, "cmd_timeout": 30}, template_fields={"command": cmd}, callable_args={})], kind="once")]}
    rep, mod = _convert_and_load(tmp_path, ir, "p9_ssh")
    defs = _generated_resources("p9_ssh")
    res = dg.materialize([mod.t_t], resources=defs.resources, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    assert ("exec", cmd) in ssh_server.seen and ("password", "etl", "pw") in ssh_server.seen
    with dg.build_resources(defs.resources) as r:                           # the same resource, called directly: stdout is the value, a non-zero exit fails the step
        assert mod.ssh_run(r.ssh_default, "printf abc", 5) == "abc"
        with pytest.raises(dg.Failure, match="status 3"):
            mod.ssh_run(r.ssh_default, "exit 3")


def test_ssh_operator_registry_route_runs_over_a_real_paramiko_connection(tmp_path, monkeypatch, ssh_server):
    pytest.importorskip("paramiko")
    pytest.importorskip("dagster_community_components")
    import registry_map as RM  # noqa: F401
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, _reg194_pkg
    if _reg194_pkg() is None:
        pytest.skip("registry 0.10.194 is not installed")
    t = op("rm", "SSHOperator", args={"ssh_conn_id": "ssh_x", "command": "echo $A", "cmd_timeout": 60, "environment": {"A": "it's 1"}}, tf={"command": "echo $A"})
    _convert(tmp_path, ir_of(t), "pkg9_ssh")
    yml_path = tmp_path / "pkg9_ssh" / "defs" / "registry_resources" / "defs.yaml"
    yml_path.write_text(yml_path.read_text().replace('username_env_var: "SSH_X_USER"', 'username_env_var: "SSH_X_USER"\n  private_key_path_env_var: "SSH_X_KEY"').rstrip() + f"\n  port: {ssh_server.port}\n")
    assert f"port: {ssh_server.port}" in yml_path.read_text()
    monkeypatch.setenv("SSH_X_HOST", "127.0.0.1")
    monkeypatch.setenv("SSH_X_USER", "etl")
    monkeypatch.setenv("SSH_X_KEY", ssh_server.key_path)
    from test_registry_resources import reg194  # noqa: F401
    _run(_load(tmp_path, "pkg9_ssh"))
    execs = [c for k, c in (s for s in ssh_server.seen if s[0] == "exec")]
    assert execs and execs[0] == "export A='it'\"'\"'s 1'; echo $A"          # the environment is quoted by the resource, the command is the DAG's


# ---- Slack: the real slack-sdk WebClient (formatting, headers, response parsing) against a local server ----------------------------------------------------------------------

@pytest.fixture
def slack_web(monkeypatch):
    pytest.importorskip("slack_sdk")
    from slack_sdk.web.client import WebClient
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "ctype": self.headers.get("Content-Type"), "body": self.rfile.read(n).decode()})
            body = json.dumps({"ok": True, "ts": "1.2", "channel": "C1"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    orig = WebClient.__init__

    def init(self, *a, **kw):
        kw["base_url"] = f"http://127.0.0.1:{srv.server_port}/"
        orig(self, *a, **kw)
    monkeypatch.setattr(WebClient, "__init__", init)
    yield seen
    srv.shutdown()


def _form(body):
    from urllib.parse import parse_qs
    try:
        return json.loads(body)
    except ValueError:
        return {k: v[0] for k, v in parse_qs(body).items()}


def test_slack_api_post_shim_posts_through_the_real_dagster_slack_resource_and_web_client(tmp_path, monkeypatch, slack_web):
    pytest.importorskip("dagster_slack")
    from test_provider_ops_more import _convert_and_load, dag, task
    monkeypatch.setenv("SLACK_API_DEFAULT_TOKEN", "xoxb-real")
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", "SlackAPIPostOperator", args={"slack_conn_id": "slack_api_default", "channel": "#data", "text": "loaded", "username": "airflow", "icon_url": "http://i/x.png",
                                                                                            "blocks": [{"type": "divider"}]}, template_fields={}, callable_args={})], kind="once")]}
    rep, mod = _convert_and_load(tmp_path, ir, "p9_slack")
    res = dg.materialize([mod.t_t], resources=_generated_resources("p9_slack").resources, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    got = slack_web[0]
    body = _form(got["body"])
    assert got["path"] == "/chat.postMessage" and got["auth"] == "Bearer xoxb-real"
    assert body["channel"] == "#data" and body["text"] == "loaded" and body["username"] == "airflow" and body["icon_url"] == "http://i/x.png" and (json.loads(body["blocks"]) if isinstance(body["blocks"], str) else body["blocks"]) == [{"type": "divider"}]


def test_slack_api_post_registry_route_posts_through_the_real_slack_post_resource(tmp_path, monkeypatch, slack_web):
    pytest.importorskip("dagster_community_components")
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, _reg194_pkg, reg194  # noqa: F401
    if _reg194_pkg() is None:
        pytest.skip("registry 0.10.194 is not installed")
    a = {"slack_conn_id": "slk_api", "channel": "#data", "text": "hi", "username": "Airflow"}
    _convert(tmp_path, ir_of(op("s", "SlackAPIPostOperator", args=a, tf={"text": "hi", "channel": "#data"})), "pkg9_slka")
    monkeypatch.setenv("SLK_API_TOKEN", "xoxb-reg")
    _run(_load(tmp_path, "pkg9_slka"))
    got = slack_web[0]
    body = _form(got["body"])
    assert got["auth"] == "Bearer xoxb-reg" and body["channel"] == "#data" and body["text"] == "hi" and body["username"] == "Airflow"


# ---- Databricks: databricks-sdk's real WorkspaceClient / dagster-databricks' real client against a fake Jobs API ---------------------------------------------------------------

@pytest.fixture
def dbx_api():
    pytest.importorskip("databricks.sdk")
    seen = []

    class H(BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode()
            path, _, query = self.path.partition("?")
            seen.append({"method": self.command, "path": path, "query": query, "auth": self.headers.get("Authorization"), "body": json.loads(body) if body else None})
            if path.endswith("/jobs/list"):
                out = {"jobs": [{"job_id": 33, "settings": {"name": "nightly"}}]}
            elif path.endswith("/jobs/run-now"):
                out = {"run_id": 7}
            elif path.endswith("/jobs/runs/submit"):
                out = {"run_id": 8}
            elif path.endswith("/jobs/runs/get"):
                out = {"run_id": int(query.split("run_id=")[1].split("&")[0]), "run_page_url": "http://u", "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS", "state_message": ""}, "tasks": []}
            else:
                out = {}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _go

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen, srv.url = seen, f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


def test_databricks_shim_posts_and_waits_through_the_real_dagster_databricks_client(tmp_path, monkeypatch, dbx_api):
    pytest.importorskip("dagster_databricks")
    from test_provider_ops import op, ir_of
    from test_provider_ops_more import _convert_and_load
    monkeypatch.setenv("DATABRICKS_DEFAULT_HOST", dbx_api.url)
    monkeypatch.setenv("DATABRICKS_DEFAULT_TOKEN", "tok")
    now = op("now", "DatabricksRunNowOperator", args={"databricks_conn_id": "databricks_default", "polling_period_seconds": 1, "wait_for_termination": True,
                                                     "json": {"job_id": "77", "notebook_params": {"day": "{{ ds }}"}}}, tf={"json": {"job_id": "77", "notebook_params": {"day": "{{ ds }}"}}})
    sub = op("sub", "DatabricksSubmitRunOperator", args={"databricks_conn_id": "databricks_default", "wait_for_termination": False,
                                                        "json": {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}}}, tf={"json": {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}}})
    rep, mod = _convert_and_load(tmp_path, ir_of(now, sub), "p9_dbx")
    res = dg.materialize([mod.t_now, mod.t_sub], resources=_generated_resources("p9_dbx").resources, tags={"airflow/logical_date": "2026-07-02T05:00:00+00:00"}, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    posts = [(s["path"], s["body"]) for s in dbx_api.seen if s["method"] == "POST"]
    assert posts == [("/api/2.1/jobs/run-now", {"job_id": 77, "notebook_params": {"day": "2026-07-02"}}), ("/api/2.1/jobs/runs/submit", {"run_name": "adhoc", "notebook_task": {"notebook_path": "/Shared/x"}})]
    assert any(s["path"].endswith("/jobs/runs/get") and "run_id=7" in s["query"] for s in dbx_api.seen)           # it waited for the run-now run
    assert not any("run_id=8" in s["query"] for s in dbx_api.seen)                                                   # and not for the submit run
    assert all(s["auth"] == "Bearer tok" for s in dbx_api.seen if "well-known" not in s["path"])


def test_databricks_registry_route_runs_through_the_real_workspace_client(tmp_path, monkeypatch, dbx_api):
    pytest.importorskip("dagster_community_components")
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, _reg194_pkg, reg194  # noqa: F401
    if _reg194_pkg() is None:
        pytest.skip("registry 0.10.194 is not installed")
    ops = [op("byname", "DatabricksRunNowOperator", args={"databricks_conn_id": "dbx", "job_name": "nightly", "json": {}, "job_parameters": {"d": "1"}, "jar_params": ["a"], "spark_submit_params": ["--x"]}),
           op("sub", "DatabricksSubmitRunOperator", up=["byname"], args={"databricks_conn_id": "dbx", "json": {"run_name": "adhoc", "tasks": [{"task_key": "t", "notebook_task": {"notebook_path": "/n"}}]}})]
    _convert(tmp_path, ir_of(*ops), "pkg9_dbx")
    monkeypatch.setenv("DBX_HOST", dbx_api.url)
    monkeypatch.setenv("DBX_TOKEN", "tok")
    _run(_load(tmp_path, "pkg9_dbx"))
    posts = {s["path"].split("/jobs/")[1]: s["body"] for s in dbx_api.seen if s["method"] == "POST"}
    assert posts["run-now"]["job_id"] == 33 and posts["run-now"]["job_parameters"] == {"d": "1"} and posts["run-now"]["jar_params"] == ["a"] and posts["run-now"]["spark_submit_params"] == ["--x"]
    assert posts["runs/submit"]["run_name"] == "adhoc" and posts["runs/submit"]["tasks"][0]["notebook_task"] == {"notebook_path": "/n"}
    assert any(s["path"].endswith("/jobs/list") and "name=nightly" in s["query"] for s in dbx_api.seen)


# ---- Google Cloud: real storage / bigquery clients against local fakes of the REST APIs (the clients' own emulator-host switches) -----------------------------------------------

@pytest.fixture
def gcp_rest(monkeypatch):
    pytest.importorskip("google.cloud.storage")
    pytest.importorskip("google.cloud.bigquery")
    objects, seen = {}, []

    class H(BaseHTTPRequestHandler):
        def _reply(self, obj, code=200, raw=None):
            data = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if raw is None else "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self):
            path, _, query = self.path.partition("?")
            body = self._body()
            seen.append(("POST", path, query, body))
            if path.startswith("/upload/storage/v1/b/"):                       # multipart upload: metadata part, then the media part
                bucket = path.split("/")[5]
                parts = body.split(b"\r\n\r\n")
                meta = json.loads(parts[1].split(b"\r\n--")[0])
                payload = parts[2].rsplit(b"\r\n--", 1)[0]
                objects[(bucket, meta["name"])] = payload
                self._reply({"name": meta["name"], "bucket": bucket, "size": str(len(payload))})
            elif "/projects/" in path and path.endswith("/jobs"):
                cfg = json.loads(body)
                self._reply({"jobReference": {"projectId": path.split("/")[-2], "jobId": cfg.get("jobReference", {}).get("jobId", "job-9"), "location": "EU"},
                             "configuration": cfg["configuration"], "status": {"state": "DONE"}})
            else:
                self._reply({})

        def do_GET(self):
            path, _, query = self.path.partition("?")
            seen.append(("GET", path, query, b""))
            if path.startswith("/storage/v1/b/") and path.endswith("/o"):
                b = path.split("/")[4]
                self._reply({"items": [{"name": k[1], "bucket": b} for k in objects if k[0] == b]})
            elif path.startswith(("/storage/v1/b/", "/download/storage/v1/b/")) and "/o/" in path:
                from urllib.parse import unquote
                b, name = path.split("/b/")[1].split("/")[0], unquote(path.split("/o/", 1)[1])
                if (b, name) not in objects:
                    self._reply({"error": {"code": 404, "message": "not found"}}, 404)
                elif "alt=media" in query:
                    self._reply(None, raw=objects[(b, name)])
                else:
                    self._reply({"name": name, "bucket": b, "size": str(len(objects[(b, name)]))})
            elif "/queries/" in path:
                self._reply({"jobComplete": True, "jobReference": {"projectId": "proj", "jobId": path.split("/")[-1]}, "schema": {"fields": []}, "totalRows": "0", "rows": []})
            elif "/jobs/" in path:
                self._reply({"jobReference": {"projectId": "proj", "jobId": path.split("/")[-1]}, "status": {"state": "DONE"}, "configuration": {"query": {"query": "select 1"}}})
            else:
                self._reply({})

        def do_DELETE(self):
            from urllib.parse import unquote
            path = self.path.split("?")[0]
            seen.append(("DELETE", path, "", b""))
            objects.pop((path.split("/")[4], unquote(path.split("/o/", 1)[1])), None)
            self._reply({})

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", url)
    monkeypatch.setenv("BIGQUERY_EMULATOR_HOST", url)
    monkeypatch.setenv("GOOGLE_CLOUD_DEFAULT_PROJECT", "proj")
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    monkeypatch.setattr(google.auth, "default", lambda *a, **k: (AnonymousCredentials(), "proj"))                    # no credentials on this machine: the clients get anonymous ones
    yield types.SimpleNamespace(objects=objects, seen=seen)
    srv.shutdown()


def test_gcs_operators_run_through_the_real_gcs_resource_and_storage_client(tmp_path, gcp_rest):
    pytest.importorskip("dagster_gcp")
    from test_provider_ops_more import _convert_and_load, dag, task
    src = tmp_path / "up.txt"
    src.write_text("hello gcs")
    out = tmp_path / "dl.txt"
    mk = lambda i, ty, up, args, tf=None: task(i, ty, up=up, args={**args, "gcp_conn_id": "google_cloud_default"}, template_fields=tf or {}, callable_args={})
    t_up = mk("up", "LocalFilesystemToGCSOperator", [], {"src": str(src), "dst": "d/up.txt", "bucket": "bk", "mime_type": "text/plain"})
    t_ex = mk("ex", "GCSObjectExistenceSensor", ["up"], {"bucket": "bk", "object": "d/up.txt"})
    t_ls = mk("ls", "GCSListObjectsOperator", ["ex"], {"bucket": "bk", "prefix": "d/"})
    t_dl = mk("dl", "GCSToLocalFilesystemOperator", ["ls"], {"bucket": "bk", "object_name": "d/up.txt", "filename": str(out)})
    t_rm = mk("rm", "GCSDeleteObjectsOperator", ["dl"], {"bucket_name": "bk", "objects": ["d/up.txt"]})
    ir = {"airflow_version": "2.10.5", "dags": [dag([t_up, t_ex, t_ls, t_dl, t_rm], kind="once")]}
    rep, mod = _convert_and_load(tmp_path, ir, "p9_gcs")
    assets = [getattr(mod, n) for n in dir(mod) if n.startswith("t_")]
    res = dg.materialize(assets, resources=_generated_resources("p9_gcs").resources, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    assert out.read_text() == "hello gcs" and ("bk", "d/up.txt") not in gcp_rest.objects
    verbs = [(m, p.split("/")[-1]) for m, p, q, b in gcp_rest.seen]
    assert ("DELETE", "d%2Fup.txt") in verbs or any(m == "DELETE" for m, *_ in gcp_rest.seen)


def test_bigquery_query_job_runs_through_the_real_bigquery_resource_and_client(tmp_path, gcp_rest):
    pytest.importorskip("dagster_gcp")
    from test_provider_ops_more import _convert_and_load, dag, task
    conf = {"query": {"query": "select 1", "useLegacySql": False, "priority": "BATCH"}}
    t = task("t", "BigQueryInsertJobOperator", args={"configuration": conf, "location": "EU", "project_id": "proj", "gcp_conn_id": "google_cloud_default"}, template_fields={"configuration": conf}, callable_args={})
    rep, mod = _convert_and_load(tmp_path, {"airflow_version": "2.10.5", "dags": [dag([t], kind="once")]}, "p9_bq")
    res = dg.materialize([mod.t_t], resources=_generated_resources("p9_bq").resources, raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    jobs = [json.loads(b) for m, p, q, b in gcp_rest.seen if m == "POST" and p.endswith("/jobs")]
    assert jobs and jobs[0]["configuration"]["query"]["query"] == "select 1" and jobs[0]["configuration"]["query"]["useLegacySql"] is False and jobs[0]["configuration"]["query"]["priority"] == "BATCH"


# ---- Docker: docker-py's real client against a fake daemon speaking the Engine HTTP API on a TCP port ---------------------------------------------------------------------------

@pytest.fixture
def docker_daemon():
    pytest.importorskip("docker")
    import struct
    state = types.SimpleNamespace(created=[], pulled=[], removed=[], containers={}, exit_code=None, requests=[])

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, obj=None, code=200, raw=None, ctype="application/json"):
            data = raw if raw is not None else (b"" if obj is None else json.dumps(obj).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            path = "/" + self.path.split("?")[0].lstrip("/").split("/", 1)[-1] if self.path.startswith("/v1.") else self.path.split("?")[0]
            state.requests.append((self.command, path))
            if path == "/version":
                self._send({"ApiVersion": "1.45", "MinAPIVersion": "1.24", "Version": "fake"})
            elif path == "/containers/create":
                cmd = body.get("Cmd")
                if isinstance(cmd, str):
                    cmd = ["sh", "-c", cmd]
                env = dict(e.split("=", 1) for e in body.get("Env") or [])
                ent = body.get("Entrypoint")
                argv = (ent if isinstance(ent, list) else [ent] if ent else []) + list(cmd or [])
                code, out, err = _run_command(argv, env) if argv else (0, "", "")
                cid = f"c{len(state.containers) + 1}"
                state.containers[cid] = {"code": state.exit_code if state.exit_code is not None else code, "out": out + err, "tty": bool(body.get("Tty"))}
                state.created.append(body)
                self._send({"Id": cid, "Warnings": []}, 201)
            elif path.endswith("/start") or path.endswith("/stop"):
                self._send(None, 204)
            elif path.endswith("/wait"):
                self._send({"StatusCode": state.containers[path.split("/")[2]]["code"]})
            elif path.endswith("/logs"):
                c = state.containers[path.split("/")[2]]
                lines = [ln.encode() + b"\n" for ln in c["out"].splitlines()]               # the daemon sends one frame per write: here one per line
                raw = b"".join(lines) if c["tty"] else b"".join(struct.pack(">BxxxL", 1, len(ln)) + ln for ln in lines)
                self._send(raw=raw, ctype="application/vnd.docker.raw-stream")
            elif path.startswith("/containers/") and self.command == "DELETE":
                state.removed.append(path.split("/")[2])
                self._send(None, 204)
            elif path.startswith("/containers/") and path.endswith("/json"):
                cid = path.split("/")[2]
                self._send({"Id": cid, "State": {"Status": "exited", "ExitCode": state.containers[cid]["code"]}, "Config": {"Tty": state.containers[cid]["tty"]}})
            elif path.startswith("/images/") and path.endswith("/json"):
                self._send({"Id": "sha256:abc", "RepoTags": [path.split("/images/")[1][:-5]]})
            elif path == "/images/create":
                state.pulled.append(self.path)
                self._send(raw=b'{"status":"ok"}\n')
            else:
                self._send({"message": f"no route {path}"}, 404)
        do_GET = do_POST = do_DELETE = _go

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.url = f"tcp://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()


def test_docker_operator_shim_runs_through_the_real_docker_py_client(tmp_path, docker_daemon):
    from test_provider_ops_more import _convert_and_load, dag, task
    args = {"image": "busybox:1", "command": "echo one; echo last-line", "environment": {"A": "1"}, "network_mode": "host", "user": "nobody", "working_dir": "/tmp", "auto_remove": "success", "force_pull": True,
            "entrypoint": None, "docker_url": docker_daemon.url}
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("t", "DockerOperator", args=args, template_fields={"image": "busybox:1", "command": "echo one; echo last-line", "environment": {"A": "1"}}, callable_args={})], kind="once")]}
    rep, mod = _convert_and_load(tmp_path, ir, "p9_dock")
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:500] for e in res.get_step_failure_events()]
    body = docker_daemon.created[0]
    assert body["Image"] == "busybox:1" and body["Env"] == ["A=1"] and body["User"] == "nobody" and body["WorkingDir"] == "/tmp" and body["HostConfig"]["NetworkMode"] == "host"
    assert docker_daemon.pulled and docker_daemon.removed == ["c1"]
    docker_daemon.exit_code = 2
    res = dg.materialize([mod.t_t], raise_on_error=False)
    assert not res.success and "code 2" in " ".join(str(e.event_specific_data.error.message) for e in res.get_step_failure_events())


def test_task_docker_runs_through_pipes_docker_client_and_the_real_docker_py_client(tmp_path, docker_daemon, monkeypatch):
    pytest.importorskip("dagster_docker")
    monkeypatch.setenv("DOCKER_HOST", docker_daemon.url)
    ir, rep, pkg = convert(tmp_path, CONTAINERS)
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_in_docker], raise_on_error=False)
    assert res.success, [str(e.event_specific_data.error)[:600] for e in res.get_step_failure_events()]
    body = docker_daemon.created[0]
    assert body["Image"] == "python:3.11" and body["HostConfig"]["NetworkMode"] == "bridge" and "A=1" in body["Env"] and body["Cmd"][:2] == ["python3", "-c"]
    meta = {e.asset_key.to_user_string(): e.materialization.metadata for e in res.get_asset_materialization_events()}
    assert meta["d/in_docker"]["airflow/return_value"].value == 2


# ---- Dataproc and Pub/Sub: the real request types validate what the converter passes (no emulator is installed; the transport call is a recording fake) -------------------------------

def _reg_env():
    pytest.importorskip("dagster_community_components")
    from test_registry_resources import _reg194_pkg
    if _reg194_pkg() is None:
        pytest.skip("registry 0.10.194 is not installed")


def test_dataproc_arguments_are_real_dataproc_v1_messages_and_match_the_real_client_signatures(tmp_path, monkeypatch):
    dp = pytest.importorskip("google.cloud.dataproc_v1")
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, reg194  # noqa: F401
    from test_registry_resources_194 import _mod
    cfg = {"master_config": {"num_instances": 1, "machine_type_uri": "n2-standard-8"}, "worker_config": {"num_instances": 3, "machine_type_uri": "n2-standard-4", "disk_config": {"boot_disk_size_gb": 100}},
           "software_config": {"image_version": "2.1", "properties": {"spark:a": "b"}}, "gce_cluster_config": {"zone_uri": "us-central1-a"}}
    job = {"placement": {"cluster_name": "cl"}, "pyspark_job": {"main_python_file_uri": "gs://b/j.py", "args": ["1"]}, "labels": {"k": "v"}}
    base = {"project_id": "proj", "region": "us-central1", "gcp_conn_id": "gcp_p9"}
    ops = [op("mk", "DataprocCreateClusterOperator", args={**base, "cluster_name": "cl", "cluster_config": cfg}),
           op("job", "DataprocSubmitJobOperator", up=["mk"], args={**base, "job": job}),
           op("batch", "DataprocCreateBatchOperator", up=["job"], args={**base, "batch": {"pyspark_batch": {"main_python_file_uri": "gs://b/j.py"}}, "batch_id": "b-1"}),
           op("rm", "DataprocDeleteClusterOperator", up=["batch"], args={**base, "cluster_name": "cl"})]
    _convert(tmp_path, ir_of(*ops), "pkg9_dp")
    from google.api_core.exceptions import NotFound
    seen = {"cluster": [], "job": [], "batch": []}
    present = {"cl": False}
    st = lambda n: types.SimpleNamespace(name=n)

    class Op:
        def __init__(self, v=None):
            self.v = v

        def result(self, timeout=None):
            return self.v

    class Cluster:
        def get_cluster(self, **kw):
            _bind(dp.ClusterControllerClient.get_cluster, None, **kw)
            if not present[kw["cluster_name"]]:
                raise NotFound("no")
            return types.SimpleNamespace(status=types.SimpleNamespace(state=st("RUNNING")))

        def create_cluster(self, **kw):
            _bind(dp.ClusterControllerClient.create_cluster, None, **kw)
            seen["cluster"].append(dp.Cluster(kw["cluster"]))                                    # unknown field names raise here
            present[kw["cluster"]["cluster_name"]] = True
            return Op(types.SimpleNamespace(status=types.SimpleNamespace(state=st("RUNNING"))))

        def delete_cluster(self, **kw):
            _bind(dp.ClusterControllerClient.delete_cluster, None, **kw)
            return Op()

    class Job:
        def submit_job(self, **kw):
            _bind(dp.JobControllerClient.submit_job, None, **kw)
            seen["job"].append(dp.Job(kw["job"]))
            return types.SimpleNamespace(reference=types.SimpleNamespace(job_id="j1"), status=types.SimpleNamespace(state=st("DONE"), details=None), driver_output_resource_uri=None)

    class Batch:
        def create_batch(self, **kw):
            _bind(dp.BatchControllerClient.create_batch, None, **kw)
            assert isinstance(kw["batch"], dp.Batch)                                            # the resource built a real Batch from the converter's dict
            seen["batch"].append(kw["batch"])
            return Op(types.SimpleNamespace(state=st("SUCCEEDED"), state_message=""))
    DR = _mod("dataproc_resource").DataprocResource
    monkeypatch.setattr(DR, "_cluster_client", lambda self, region: Cluster())
    monkeypatch.setattr(DR, "_job_client", lambda self, region: Job())
    monkeypatch.setattr(DR, "_batch_client", lambda self, region: Batch())
    _run(_load(tmp_path, "pkg9_dp"))
    c = seen["cluster"][0]
    assert c.config.worker_config.num_instances == 3 and c.config.master_config.machine_type_uri == "n2-standard-8" and c.config.worker_config.disk_config.boot_disk_size_gb == 100
    assert c.config.software_config.image_version == "2.1" and c.config.gce_cluster_config.zone_uri == "us-central1-a"
    assert seen["job"][0].pyspark_job.main_python_file_uri == "gs://b/j.py" and seen["job"][0].labels["k"] == "v"
    assert seen["batch"][0].pyspark_batch.main_python_file_uri == "gs://b/j.py"


def test_pubsub_arguments_are_real_pubsub_v1_messages_and_match_the_real_client_signatures(tmp_path, monkeypatch):
    ps = pytest.importorskip("google.cloud.pubsub_v1")
    from google.pubsub_v1 import types as pt
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, reg194  # noqa: F401
    from test_registry_resources_194 import _mod
    base = {"project_id": "proj", "gcp_conn_id": "gcp_ps9"}
    ops = [op("t", "PubSubCreateTopicOperator", args={**base, "topic": "tp", "labels": {"a": "b"}, "fail_if_exists": False}),
           op("s", "PubSubCreateSubscriptionOperator", up=["t"], args={**base, "topic": "tp", "subscription": "sb", "ack_deadline_secs": 30, "push_config": {"pushEndpoint": "https://h/p"}}),
           op("p", "PubSubPublishMessageOperator", up=["s"], args={**base, "topic": "tp", "messages": [{"data": {"repr": "b'hello'"}, "attributes": {"k": "v"}}, {"data": {"repr": "b'two'"}}]}),
           op("ds", "PubSubDeleteSubscriptionOperator", up=["p"], args={**base, "subscription": "sb"}),
           op("dt", "PubSubDeleteTopicOperator", up=["ds"], args={**base, "topic": "tp"})]
    _convert(tmp_path, ir_of(*ops), "pkg9_ps")
    log = []

    class Pub(ps.PublisherClient.__mro__[0]):                                                   # the real client class, built without credentials or a channel
        def __init__(self):
            pass

        def create_topic(self, request):
            log.append(("topic", pt.Topic(request)))
            return types.SimpleNamespace(labels={"a": "b"})

        def publish(self, topic, data, **attrs):
            log.append(("publish", topic, data, attrs))
            return types.SimpleNamespace(result=lambda: "m-1")

        def delete_topic(self, request):
            log.append(("delete_topic", pt.DeleteTopicRequest(request)))

    class Sub(ps.SubscriberClient.__mro__[0]):
        def __init__(self):
            pass

        def create_subscription(self, request):
            log.append(("sub", pt.Subscription(request)))

        def delete_subscription(self, request):
            log.append(("delete_sub", pt.DeleteSubscriptionRequest(request)))
    # the real helper methods (topic_path, subscription_path) are inherited; the real publish() signature takes (topic, data, ordering_key="", retry, timeout, **attrs)
    _bind(ps.PublisherClient.publish, None, "projects/p/topics/t", b"x", k="v")
    PR = _mod("pubsub_resource").PubsubResource
    monkeypatch.setattr(PR, "_publisher_client", lambda self: Pub())
    monkeypatch.setattr(PR, "_subscriber_client", lambda self: Sub())
    _run(_load(tmp_path, "pkg9_ps"))
    kinds = {k: v for k, *v in log}
    assert kinds["topic"][0].labels["a"] == "b" and kinds["topic"][0].name == "projects/proj/topics/tp"
    s = kinds["sub"][0]
    assert s.ack_deadline_seconds == 30 and s.push_config.push_endpoint == "https://h/p" and s.topic == "projects/proj/topics/tp"
    assert [(c[2], c[3]) for c in log if c[0] == "publish"] == [(b"hello", {"k": "v"}), (b"two", {})]


# ---- the SQL splitter against sqlparse (what Airflow's `split_statements` uses) -------------------------------------------------------------------------------------------------

SPLIT_CASES = ["select 1; select ';' as x; -- c;\n select 2;", "select 'it''s;'; select 2", "insert into t values (1,'a;b');insert into t values (2,'z')", "create table a(i int);\n\n  create table b(i int);\n",
               "select 1;;select 2", "/* a;b */ select 1; select 2", 'select "a;b" from t; select 2', "drop table if exists x; create table x as select 1", "select 1 -- trailing; comment\n; select 2",
               "  ;select 1;  ", "select 1;\n-- only comment;\n", "update t set a = 'x;y' where b = 1; delete from t where a = ';'", "select 1; /* c1; */ select 2; /* c2 */", "select $1; select 2",
               "insert into t values ('a\n;b'); select 1", "select 1;\nselect 2;\r\nselect 3"]


@pytest.mark.parametrize("sql", SPLIT_CASES)
def test_the_converters_sql_splitter_agrees_with_sqlparse_on_the_statements_that_run(sql):
    sqlparse = pytest.importorskip("sqlparse")
    import registry_map as RM
    norm = lambda parts: [x for x in (sqlparse.format(p, strip_comments=True).strip().rstrip(";").strip() for p in parts) if x]       # comment-only pieces run as nothing
    mine = RM.split_sql(sql)
    assert mine is not None
    assert norm(mine) == norm(sqlparse.split(sql))


def test_the_splitter_refuses_what_it_cannot_split_exactly_and_the_shim_uses_sqlparse_itself():
    sqlparse = pytest.importorskip("sqlparse")
    import registry_map as RM
    assert RM.split_sql("select 1; select '\\'; select 2") is None          # a backslash escape: sqlparse reads it one way, other engines another, so the converter leaves it to the shim
    import provider_ops as P
    ns = {"dg": dg, "__name__": "x"}
    exec(P.PROVIDER_RUNTIME if hasattr(P, "PROVIDER_RUNTIME") else "", ns)
    assert ns["_split_sql"]("select 1; select 2;") == sqlparse.split("select 1; select 2;")


# ---- KubernetesPodOperator / DockerOperator -> registry asset components, loaded by the real component loader ---------------------------------------------------------------------

def _lit(v):
    """What dagster's component loader makes of a YAML value: it runs ast.literal_eval on every string."""
    import ast
    if isinstance(v, str):
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v
    if isinstance(v, dict):
        return {k: _lit(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_lit(x) for x in v]
    return v


def _docs(tmp_path, pkg, dag="d1"):
    import yaml
    docs = list(yaml.safe_load_all((tmp_path / pkg / "defs" / dag / "defs.yaml").read_text()))
    return [{**d, "attributes": _lit(d["attributes"])} for d in docs]


def _kpo_args(**k):
    a = {"image": "registry.example.com/crunch:1.2", "cmds": ["python", "-m", "crunch"], "arguments": ["--day", "2026-07-02"], "namespace": "batch",
         "env_vars": [{"repr": "{'name': 'MODE', 'value': 'full', 'value_from': None}"}], "service_account_name": "crunch-sa", "in_cluster": True,
         "labels": {"team": "data"}, "image_pull_policy": "IfNotPresent", "image_pull_secrets": [{"repr": "{'name': 'regcred'}"}],
         "container_resources": {"repr": "{'limits': {'cpu': '1', 'memory': '2Gi'}, 'requests': None, 'claims': None}"},
         "node_selector": {"pool": "batch"}, "tolerations": [{"repr": "{'key': 'dedicated', 'operator': 'Equal', 'value': 'batch', 'effect': 'NoSchedule', 'toleration_seconds': None}"}],
         "env_from": [{"repr": "{'config_map_ref': {'name': 'app-cfg', 'optional': None}, 'prefix': None, 'secret_ref': None}"}],
         "volumes": [], "volume_mounts": [], "do_xcom_push": False, "get_logs": True, "on_finish_action": "delete_pod", "kubernetes_conn_id": "kubernetes_default", "startup_timeout_seconds": 120, **k}
    return a


@pytest.fixture
def k8s_cluster_calls(monkeypatch):
    """execute_k8s_job builds the real Job; only the calls that need a cluster are replaced (the same stand-in the pass 7 tests use)."""
    from test_provider_ops import k8s
    return k8s.__wrapped__(monkeypatch) if hasattr(k8s, "__wrapped__") else None


def test_kubernetes_pod_operator_becomes_a_k8s_job_asset_that_the_real_loader_builds_and_runs(tmp_path, monkeypatch):
    pytest.importorskip("dagster_k8s")
    _reg_env()
    import yaml
    from test_provider_ops import op, ir_of, k8s as k8s_fx
    from test_registry_resources import _convert, _load, _run, reg194  # noqa: F401
    rep = _convert(tmp_path, ir_of(op("crunch", "KubernetesPodOperator", args=_kpo_args(), tf={})), "pkg9_kpo")["dags"]["d1"]
    assert rep["output"] == "components" and "dagster-k8s" in rep["requirements"]
    doc = _docs(tmp_path, "pkg9_kpo")[0]
    at = doc["attributes"]
    assert doc["type"] == "dagster_community_components.K8sJobAssetComponent" and at["namespace"] == "batch" and at["command"] == ["python", "-m", "crunch"] and at["args"] == ["--day", "2026-07-02"]
    assert at["env_vars"] == {"MODE": "full"} and at["cpu_limit"] == "1" and at["memory_limit"] == "2Gi" and at["node_selector"] == {"pool": "batch"} and at["labels"] == {"team": "data"}
    assert at["tolerations"][0]["key"] == "dedicated" and at["image_pull_secrets"] == ["regcred"] and at["env_from_config_maps"] == ["app-cfg"] and "kubeconfig_file" not in at and "use_pipes" not in at
    assert any("k8s_job_asset" in c["reason"] and "recording fake" in c["reason"] for c in rep["caveats"])
    # the registry's own component class validates every attribute (an invented field fails here) and builds the asset; the real execute_k8s_job runs it
    import dagster_k8s
    jobs = []
    pod = types.SimpleNamespace(metadata=types.SimpleNamespace(name="pod-1"))
    api = types.SimpleNamespace(batch_api=types.SimpleNamespace(create_namespaced_job=lambda ns, job: jobs.append((ns, job))), core_api=types.SimpleNamespace(read_namespaced_pod_log=None),
                                wait_for_job=lambda **k: None, wait_for_job_to_have_pods=lambda *a, **k: [pod], wait_for_pod=lambda *a, **k: None, wait_for_running_job_to_succeed=lambda **k: None,
                                get_pod_names_in_job=lambda **k: [], delete_job=lambda **k: None)
    from dagster_k8s.client import DagsterKubernetesClient
    import kubernetes
    monkeypatch.setattr(DagsterKubernetesClient, "production_client", staticmethod(lambda: api))
    monkeypatch.setattr(kubernetes.config, "load_incluster_config", lambda: jobs.append("incluster"))
    monkeypatch.setattr(kubernetes.watch, "Watch", type("W", (), {"stream": lambda s, *a, **k: iter(["a log line"]), "stop": lambda s: None}))
    _run(_load(tmp_path, "pkg9_kpo"))
    ns, job = next(j for j in jobs if isinstance(j, tuple))
    pod_spec, c = job.spec.template.spec, job.spec.template.spec.containers[0]
    assert "incluster" in jobs and ns == "batch" and c.image == "registry.example.com/crunch:1.2" and c.command == ["python", "-m", "crunch"] and c.args[-2:] == ["--day", "2026-07-02"]
    assert c.resources.limits == {"cpu": "1", "memory": "2Gi"} and c.image_pull_policy == "IfNotPresent" and pod_spec.service_account_name == "crunch-sa" and pod_spec.node_selector == {"pool": "batch"}
    assert pod_spec.tolerations[0].key == "dedicated" and pod_spec.image_pull_secrets[0].name == "regcred" and any(e.config_map_ref and e.config_map_ref.name == "app-cfg" for e in c.env_from)
    assert {"name": "MODE", "value": "full"} in [{"name": e.name, "value": e.value} for e in c.env] and job.spec.template.metadata.labels["team"] == "data"
    del k8s_fx


def test_kubernetes_pod_operator_with_a_kubeconfig_and_context_passes_both_to_the_component(tmp_path, monkeypatch):
    pytest.importorskip("dagster_k8s")
    _reg_env()
    import yaml
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, reg194  # noqa: F401
    a = _kpo_args(in_cluster=False, config_file="/etc/kube/config", cluster_context="prod")
    rep = _convert(tmp_path, ir_of(op("crunch", "KubernetesPodOperator", args=a, tf={})), "pkg9_kpo2")["dags"]["d1"]
    at = _docs(tmp_path, "pkg9_kpo2")[0]["attributes"]
    assert rep["output"] == "components" and at["kubeconfig_file"] == "/etc/kube/config" and at["kube_context"] == "prod"


@pytest.mark.parametrize("extra,why", [({"in_cluster": False}, "default kubeconfig"), ({"namespace": None}, "namespace"),
                                       ({"container_resources": {"repr": "{'limits': None, 'requests': {'cpu': '1'}, 'claims': None}"}}, "requests"),
                                       ({"env_from": [{"repr": "{'config_map_ref': None, 'prefix': 'P_', 'secret_ref': {'name': 's', 'optional': None}}"}]}, "prefix"),
                                       ({"labels": {"n": 1}}, "not a string")])
def test_kubernetes_pod_operator_keeps_the_shim_when_the_component_cannot_carry_an_argument_and_names_it(tmp_path, extra, why):
    pytest.importorskip("dagster_k8s")
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, reg194  # noqa: F401
    tf = {"arguments": extra["arguments"]} if "arguments" in extra else {}
    rep = _convert(tmp_path, ir_of(op("crunch", "KubernetesPodOperator", args=_kpo_args(**extra), tf=tf)), "pkg9_kpofb")["dags"]["d1"]
    reason = [c["reason"] for c in rep["caveats"] if "k8s_job_asset not used for this task" in c["reason"]]
    assert reason and why in reason[0], rep["caveats"]
    assert "k8s_pod_run(" in (tmp_path / "pkg9_kpofb" / "defs" / "d1_steps.py").read_text() and rep["output"] == "components"


# ---- DockerOperator -> docker_container_asset: entrypoint, container_kwargs and KEY=value env, run by the real execute_docker_container against the fake daemon ---------------------------

def test_docker_operator_becomes_a_docker_container_asset_with_entrypoint_and_container_kwargs(tmp_path, monkeypatch, docker_daemon):
    pytest.importorskip("dagster_docker")
    _reg_env()
    import yaml
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, reg194  # noqa: F401
    a = {"image": "busybox:1", "command": "echo one two", "entrypoint": "sh -c", "environment": {"A": "1", "B": "two words"}, "network_mode": "bridge", "user": "nobody", "working_dir": "/tmp", "tty": False,
         "mem_limit": "256m", "shm_size": "64m", "hostname": "box", "cpus": 2.0, "privileged": False}
    rep = _convert(tmp_path, ir_of(op("run", "DockerOperator", args=a, tf={})), "pkg9_dock")["dags"]["d1"]
    doc = _docs(tmp_path, "pkg9_dock")[0]
    at = doc["attributes"]
    assert rep["output"] == "components" and doc["type"].endswith("DockerContainerAssetComponent")
    assert at["entrypoint"] == ["sh", "-c"] and at["command"] == ["echo", "one", "two"] and at["env_vars"] == {"A": "1", "B": "two words"} and at["network"] == "bridge"
    assert at["container_kwargs"] == {"user": "nobody", "working_dir": "/tmp", "mem_limit": "256m", "shm_size": "64m", "hostname": "box", "cpu_quota": 200000, "cpu_period": 100000}
    assert "dagster-docker" in rep["requirements"]
    monkeypatch.setenv("DOCKER_HOST", docker_daemon.url)
    docker_daemon.exit_code = 0
    _run(_load(tmp_path, "pkg9_dock"))
    body = docker_daemon.created[0]
    assert body["Image"] == "busybox:1" and body["Entrypoint"] == ["sh", "-c"] and body["Cmd"] == ["echo", "one", "two"] and sorted(body["Env"]) == ["A=1", "B=two words"]       # KEY=value, not bare key names
    hc = body["HostConfig"]
    assert hc["NetworkMode"] == "bridge" and hc["Memory"] == 256 * 1024 * 1024 and hc["ShmSize"] == 64 * 1024 * 1024 and hc["CpuQuota"] == 200000 and body["User"] == "nobody" and body["WorkingDir"] == "/tmp" and body["Hostname"] == "box"


@pytest.mark.parametrize("extra,why", [({"container_name": "mine"}, "container_name"), ({"force_pull": True}, "force_pull"), ({"auto_remove": "success"}, "auto_remove"), ({"mounts": ["m"]}, "mounts"),
                                       ({"docker_url": "tcp://h:2375"}, "DOCKER_HOST")])
def test_docker_operator_keeps_the_shim_when_the_component_cannot_carry_an_argument_and_names_it(tmp_path, extra, why):
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, reg194  # noqa: F401
    a = {"image": "busybox:1", "command": "echo hi", "environment": {"A": "1"}, **extra}
    rep = _convert(tmp_path, ir_of(op("run", "DockerOperator", args=a, tf={k: v for k, v in extra.items() if k == "command"})), "pkg9_dockfb")["dags"]["d1"]
    reason = [c["reason"] for c in rep["caveats"] if "docker_container_asset not used for this task" in c["reason"]]
    stubbed = [c for c in rep["caveats"] if c["kind"] == "stub"]
    assert (reason and why in reason[0]) or (stubbed and why in stubbed[0]["reason"]), rep["caveats"]


def test_a_templated_container_argument_keeps_the_python_route_where_the_shim_renders_it(tmp_path):
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, reg194  # noqa: F401
    for i, (ty, args, tf) in enumerate([("KubernetesPodOperator", _kpo_args(arguments=["--day", "{{ ds }}"]), {"arguments": ["--day", "{{ ds }}"]}),
                                        ("DockerOperator", {"image": "busybox:1", "command": "echo {{ ds }}"}, {"command": "echo {{ ds }}"})]):
        sub = tmp_path / str(i)
        sub.mkdir()
        rep = _convert(sub, ir_of(op("t", ty, args=args, tf=tf)), "pkg9_tpl")["dags"]["d1"]
        assert rep["output"] == "python" and not any(c["kind"] == "stub" for c in rep["caveats"]), (ty, rep["caveats"])


def test_pipes_in_cluster_and_kubeconfig_are_not_combined(tmp_path):
    """The real PipesK8sClient refuses `load_incluster_config=True` together with a kubeconfig file or context; a KubernetesPodOperator with both (in_cluster wins in Airflow) must not emit both."""
    pytest.importorskip("dagster_k8s")
    src = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.kubernetes(image="python:3.11", in_cluster=True, config_file="/etc/kube/config", cluster_context="prod")
        def in_pod(x):
            return x
        in_pod(1)
    d()
'''
    ir, rep, pkg = convert(tmp_path, src)
    text = (pkg / "defs" / "d.py").read_text()
    assert "'load_incluster_config': True" in text and "kubeconfig_file" not in text.split("_AF_PIPES_in_pod")[1].split("\n")[0]


# ---- the component loader evaluates string attributes as Python literals (found with the real loader) --------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "1.5", "-3", "None", "True", "[1, 2]", "'q'", "01", "full", "{{ env.X }}", "two words", "", "1e3"])
def test_loader_safe_strings_survive_the_loaders_literal_eval_unchanged(value):
    import ast
    import convert_ir as C
    out = C._loader_safe(value)
    try:
        seen = ast.literal_eval(out)
    except (ValueError, SyntaxError):
        seen = out
    assert seen == value and isinstance(seen, str)


def test_a_numeric_string_in_a_shell_tasks_env_reaches_the_component_as_a_string(tmp_path):
    _reg_env()
    from test_provider_ops import op, ir_of
    from test_registry_resources import _convert, _load, _run, reg194  # noqa: F401
    t = op("sh", "BashOperator", args={"bash_command": "echo $N", "env": {"N": "1", "W": "full"}}, tf={"bash_command": "echo $N", "env": {"N": "1", "W": "full"}})
    rep = _convert(tmp_path, ir_of(t), "pkg9_sh")["dags"]["d1"]
    assert rep["output"] == "components"
    _run(_load(tmp_path, "pkg9_sh"))              # the real loader validates env_vars: Dict[str, str]
