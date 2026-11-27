"""Converter pass 8 (reference/converter-triage.md, "Stage 2a, pass 8"): operators that run code in another environment.

Dagster has no per-step virtualenv built at run time. `PythonVirtualenvOperator` / `@task.virtualenv` run in-process in the project's environment (requirements added to
converted_requirements.txt, a loud caveat, conflict detection); `ExternalPythonOperator` / `@task.external_python` run through `PipesSubprocessClient` on the operator's
`python`; `@task.docker` / `@task.kubernetes` through `PipesDockerClient` / `PipesK8sClient`. Every test has real Airflow parse a small DAG, converts what the extractor
wrote and RUNS the emitted Dagster code; the "external" interpreter is the registry venv (it has dagster-pipes) so nothing is installed."""
import json
import os
import sys
import types
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import isolation_ops as IS  # noqa: E402
from test_converter_stage2a_v8 import SCRATCH, extract_and_convert, load_dag  # noqa: E402

_N = [0]


def external_python():
    py = SCRATCH / "venvs" / "reg194" / "bin" / "python"
    if not py.exists():
        pytest.skip("no second interpreter with dagster-pipes (venvs/reg194)")
    return str(py)


def convert(tmp_path, source, major=2, flags=()):
    _N[0] += 1
    irs, rep, pkg = extract_and_convert(tmp_path, source, major=major, pkg=f"venvpk{_N[0]}", flags=flags)
    return irs["d"], rep["d"], pkg


def mat_events(res):
    return {e.asset_key.to_user_string(): e.materialization.metadata for e in res.get_asset_materialization_events()}


def failure_text(res):
    return " ".join(str(e.event_specific_data.error.message) + str(e.event_specific_data.error.cause) for e in res.all_events if e.event_type_value == "STEP_FAILURE")


# ---- ExternalPythonOperator / @task.external_python: PipesSubprocessClient ------------------------------------------------------------------------------------------

EXTERNAL = '''
    import pendulum
    from airflow.decorators import dag, task
    from airflow.operators.python import ExternalPythonOperator

    PY = {py!r}

    def classic(a, b=2, templates_dict=None):
        import sys
        print("CLASSIC", a, b, templates_dict, "exe_is_external", sys.executable != {cur!r})
        return {{"sum": a + b, "t": templates_dict}}

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.external_python(python=PY)
        def deco(x, y=10):
            import sys
            print("DECO", x, y)
            return [x, y, sys.version_info[0]]

        @task
        def after(v):
            print("AFTER", v)

        classic_op = ExternalPythonOperator(task_id="classic_op", python_callable=classic, op_args=[1], op_kwargs={{"b": 5}}, python=PY,
                                            templates_dict={{"day": "{{{{ ds }}}}"}})
        after(deco(4, y=6))
        classic_op
    d()
'''


@pytest.mark.parametrize("major", [2, 3])
def test_external_python_runs_the_function_in_the_given_interpreter_through_pipes(tmp_path, capfd, major):
    py = external_python()
    ir, rep, pkg = convert(tmp_path, EXTERNAL.format(py=py, cur=sys.executable), major=major)
    assert {t["task_id"]: t["type"] for t in ir["tasks"] if t["task_id"] != "after"} == {"deco": "_PythonExternalDecoratedOperator", "classic_op": "ExternalPythonOperator"}
    assert rep["status"] == "converted" and sorted(rep["external_files"]) == ["external_python/d__classic_op.py", "external_python/d__deco.py"]
    assert not any(c["kind"] == "stub" for c in rep["caveats"])
    assert any("DECO_PYTHON" in n for n in rep["notes"]) and rep["isolated"]["deco"]["python"] == py and rep["isolated"]["deco"]["python_env"] == "DECO_PYTHON"
    script = (pkg / "external_python" / "d__deco.py").read_text()
    assert "open_dagster_pipes" in script and "def deco(x, y=10):" in script and "@task" not in script and "airflow" not in script.replace("Airflow task", "")
    mod = load_dag(pkg, "d")
    src = (pkg / "defs" / "d.py").read_text()
    assert "af_pipes_run(" in src and "dg.PipesSubprocessClient()" in (pkg / "runtime.py").read_text()          # the client is called from runtime.py
    res = dg.materialize([mod.t_deco, mod.t_classic_op, mod.t_after], raise_on_error=False)
    assert res.success
    out = capfd.readouterr().out
    assert "DECO 4 6" in out                                                      # args went through extras, the child's stdout reaches the run
    assert "CLASSIC 1 5 {'day': '" in out and "exe_is_external True" in out        # op_args, op_kwargs and a rendered templates_dict
    assert "AFTER [4, 6, 3]" in out                                               # the return value is the asset value the downstream task reads
    meta = mat_events(res)
    assert meta["d/deco"]["airflow/return_value"].value == [4, 6, 3]              # also recorded as metadata
    assert meta["d/classic_op"]["airflow/return_value"].value["sum"] == 6


def test_external_python_interpreter_is_an_environment_override_and_a_missing_one_fails_loudly(tmp_path, monkeypatch):
    ir, rep, pkg = convert(tmp_path, EXTERNAL.format(py="/no/such/python", cur=sys.executable))
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_deco], raise_on_error=False)
    assert not res.success and "not falling back to the current interpreter" in failure_text(res) and "DECO_PYTHON" in failure_text(res)
    monkeypatch.setenv("DECO_PYTHON", external_python())                            # the path is a constant with an override
    assert dg.materialize([mod.t_deco], raise_on_error=False).success


FAILING = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.external_python(python={py!r})
        def boom(x):
            raise ValueError("boom " + str(x))

        @task.external_python(python={py!r})
        def not_json():
            return {{1, 2}}

        @task.external_python(python={py!r})
        def takes(v):
            return v

        @task
        def make_set():
            return {{1, 2}}

        takes(make_set())
        boom(7)
        not_json()
    d()
'''


def test_external_python_failure_and_values_that_are_not_json_fail_with_a_message(tmp_path, capfd):
    ir, rep, pkg = convert(tmp_path, FAILING.format(py=external_python()))
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_boom], raise_on_error=False)
    err = capfd.readouterr().err
    assert not res.success and "External execution process failed" in failure_text(res) and "ValueError: boom 7" in err      # the step fails; the child's traceback is in the run's logs
    res = dg.materialize([mod.t_not_json], raise_on_error=False)
    assert not res.success and "not JSON-serialisable" in capfd.readouterr().err       # a return value that Pipes cannot carry (the child says so in the run's logs)
    res = dg.materialize([mod.t_make_set, mod.t_takes], raise_on_error=False)
    assert not res.success and "JSON only" in failure_text(res)                      # an argument that Pipes extras cannot carry


BRANCH_EXT = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.branch_external_python(python={py!r})
        def pick(n):
            return "big" if n > 3 else "small"

        @task
        def big(): print("BIG")
        @task
        def small(): print("SMALL")
        pick(5) >> [big(), small()]
    d()
'''


def test_branch_external_python_chooses_the_branch_in_the_graph_form(tmp_path, capfd):
    ir, rep, pkg = convert(tmp_path, BRANCH_EXT.format(py=external_python()))
    assert rep["status"] == "converted" and "external_python/d__pick.py" in rep["external_files"]
    mod = load_dag(pkg, "d")
    from test_converter_stage2a_v8 import run_assets
    res = run_assets(mod)
    assert res.success
    out = capfd.readouterr().out
    assert "BIG" in out and "SMALL" not in out


# ---- PythonVirtualenvOperator / @task.virtualenv: in-process, requirements joined, loud caveat --------------------------------------------------------------------

VENV = '''
    import pendulum
    from airflow.decorators import dag, task
    from airflow.operators.python import PythonVirtualenvOperator

    def classic(a):
        import os, sys
        print("VENV_CLASSIC", a, os.environ.get("VENV_FLAG"), sys.executable)
        return a * 2

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.virtualenv(requirements=["numpy~=1.26.0", "pandas==2.0.0"], system_site_packages=False, python_version="3.11")
        def deco(x):
            import sys
            return {"x": x, "exe": sys.executable}

        classic_op = PythonVirtualenvOperator(task_id="classic_op", python_callable=classic, op_args=[21], requirements=["tabulate>=0.9"], env_vars={"VENV_FLAG": "on"})

        @task
        def after(v): print("AFTER", v["x"])
        after(deco(3))
        classic_op
    d()
'''


def test_virtualenv_runs_in_process_adds_its_requirements_and_says_so_loudly(tmp_path, capfd):
    ir, rep, pkg = convert(tmp_path, VENV)
    tv = next(t for t in ir["tasks"] if t["task_id"] == "deco")
    assert tv["args"]["python_version"] == "3.11" and tv["args"]["system_site_packages"] is False and tv["args"]["requirements"] == ["numpy~=1.26.0", "pandas==2.0.0"]   # the extractor reads the decorator's settings
    assert rep["status"] == "converted" and not rep.get("external_files")
    reqs = (pkg / "converted_requirements.txt").read_text().split()
    assert {"numpy~=1.26.0", "pandas==2.0.0", "tabulate>=0.9"} <= set(reqs)             # pinned as given, in the project's requirements
    by_task = {c["task"]: c["reason"] for c in rep["caveats"]}
    for word in ("NO DYNAMIC PER-STEP VIRTUALENV", "in-process", "converted_requirements.txt", "separate Dagster code location", "system_site_packages=False", "python_version='3.11'"):
        assert word in by_task["deco"], word
    assert "tabulate>=0.9" in by_task["classic_op"]
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_deco, mod.t_classic_op, mod.t_after], raise_on_error=False)
    assert res.success
    out = capfd.readouterr().out
    assert f"VENV_CLASSIC 21 on {sys.executable}" in out                                 # in this interpreter, with the operator's env_vars
    assert os.environ.get("VENV_FLAG") is None                                           # and put back afterwards
    assert "AFTER 3" in out


BRANCH_VENV = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.branch_virtualenv(requirements=["numpy~=1.24.4"])
        def pick():
            return "left"

        @task
        def left(): print("LEFT")
        @task
        def right(): print("RIGHT")
        pick() >> [left(), right()]
    d()
'''


def test_branch_virtualenv_branches_in_process_with_the_same_caveat(tmp_path, capfd):
    ir, rep, pkg = convert(tmp_path, BRANCH_VENV)
    assert rep["status"] == "converted" and "numpy~=1.24.4" in (pkg / "converted_requirements.txt").read_text()
    assert any(c["task"] == "pick" and "NO DYNAMIC PER-STEP VIRTUALENV" in c["reason"] for c in rep["caveats"])
    from test_converter_stage2a_v8 import run_assets
    res = run_assets(load_dag(pkg, "d"))
    assert res.success
    out = capfd.readouterr().out
    assert "LEFT" in out and "RIGHT" not in out


CONFLICT = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.virtualenv(requirements=["numpy~=1.24.4"])
        def old(): return 1

        @task.virtualenv(requirements=["numpy~=1.26.0", "protobuf<4"])
        def new(): return 2

        @task.virtualenv(requirements=["numpy~=1.24.4"])
        def same(): return 3
        old(); new(); same()
    d()
'''


def test_conflicting_requirements_across_tasks_and_against_dagster_are_named(tmp_path):
    ir, rep, pkg = convert(tmp_path, CONFLICT)
    msgs = [c["reason"] for c in rep["caveats"] if not c["task"]]
    both = [m for m in msgs if "numpy~=1.24.4" in m and "numpy~=1.26.0" in m]
    assert both and "d.old" in both[0] and "d.new" in both[0] and "separate Dagster code location" in both[0]
    assert not any("numpy~=1.24.4" in m and "d.same" in m and "d.old" in m for m in msgs)       # the same pin twice is no conflict
    dagster_side = [m for m in msgs if "Dagster's own requirement" in m]
    assert dagster_side and "protobuf<4" in dagster_side[0]


def test_conflicts_between_dags_are_reported_for_the_project(tmp_path):
    """Two DAGs, each fine alone, pin one package incompatibly: the project environment cannot hold both."""
    def ir_with(dag_id, pin):
        from test_convert_ir import dag as _dag, fn, task
        t = task("v", "PythonVirtualenvOperator", callable=fn("three"), op_args=[], op_kwargs={}, template_fields={"requirements": [pin]}, args={"system_site_packages": True, "inherit_env": True})
        return {**_dag([t], kind="once"), "dag_id": dag_id}
    import subprocess
    (tmp_path / "ir.json").write_text(json.dumps({"airflow_version": "x", "dags": [ir_with("a", "numpy~=1.24.4"), ir_with("b", "numpy~=1.26.0")]}))
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg" / "defs"), "--report", str(tmp_path / "r.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["dependency_conflicts"] and "a.v" in rep["dependency_conflicts"][0] and "b.v" in rep["dependency_conflicts"][0]
    for k in ("a", "b"):
        assert any("numpy~=1.24.4" in c["reason"] and "numpy~=1.26.0" in c["reason"] for c in rep["dags"][k]["caveats"])


def test_requirement_parsing_and_conflict_rules():
    assert IS.parse_requirements("numpy==1.0\n# c\n-r other.txt\nrequests>=2 # why") == (["numpy==1.0", "requests>=2"], ["-r other.txt"])
    assert IS.parse_requirements(["a", "./local"]) == (["a"], ["./local"])
    assert IS.req_conflicts([("t1", "x>=2"), ("t2", "x<2")])                           # nothing satisfies both
    assert not IS.req_conflicts([("t1", "x>=1"), ("t2", "x<3")])
    assert not IS.req_conflicts([("t1", "x==1.2"), ("t2", "x~=1.2")])
    assert not IS.req_conflicts([("t1", "x"), ("t2", "x==1")])                          # an unpinned package never conflicts
    assert IS.req_conflicts([("t1", "pydantic<1")], ["pydantic>=1.10,<3"])             # against a base requirement (Dagster's own)
    assert IS.dagster_requirements()                                                    # read from the installed dagster


# ---- @task.docker / @task.kubernetes: Pipes container clients --------------------------------------------------------------------------------------------------------

CONTAINERS = '''
    import pendulum
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.docker(image="python:3.11", environment={"A": "1"}, network_mode="bridge", mem_limit="1g")
        def in_docker(x):
            return x + 1

        @task.kubernetes(image="python:3.11", namespace="jobs", env_vars={"B": "2"}, labels={"team": "x"}, node_selector={"disk": "ssd"})
        def in_pod(x):
            return x * 2

        in_docker(1); in_pod(2)
    d()
'''


@pytest.fixture
def fake_pipes_clients(monkeypatch):
    """Recording stand-ins for dagster-docker and dagster-k8s (neither is installed, and there is no daemon or cluster)."""
    import importlib.util
    if importlib.util.find_spec("dagster_docker") and importlib.util.find_spec("dagster_k8s"):
        pytest.skip("dagster-docker and dagster-k8s are installed: test_converter_pass9.py runs this against the real PipesDockerClient / PipesK8sClient")
    calls = []

    class Fake:
        def __init__(self, **kw):
            self.init = kw

        def run(self, **kw):
            calls.append((type(self).__name__, self.init, kw))
            return types.SimpleNamespace(get_custom_messages=lambda: ({"return_value": 41},))

    docker_mod, k8s_mod = types.ModuleType("dagster_docker"), types.ModuleType("dagster_k8s")
    docker_mod.PipesDockerClient = type("PipesDockerClient", (Fake,), {})
    k8s_mod.PipesK8sClient = type("PipesK8sClient", (Fake,), {})
    monkeypatch.setitem(sys.modules, "dagster_docker", docker_mod)
    monkeypatch.setitem(sys.modules, "dagster_k8s", k8s_mod)
    return calls


def test_task_docker_and_task_kubernetes_become_pipes_container_clients(tmp_path, fake_pipes_clients):
    ir, rep, pkg = convert(tmp_path, CONTAINERS)
    assert rep["status"] == "converted" and not any(c["kind"] == "stub" for c in rep["caveats"])
    assert {"dagster-docker", "dagster-k8s"} <= set((pkg / "converted_requirements.txt").read_text().split())
    reasons = " ".join(c["reason"] for c in rep["caveats"])
    assert "mem_limit" in reasons and "node_selector" in reasons and "UNVERIFIED" in reasons                    # what is not carried is named
    mod = load_dag(pkg, "d")
    res = dg.materialize([mod.t_in_docker, mod.t_in_pod], raise_on_error=False)
    assert res.success
    by = {c[0]: c for c in fake_pipes_clients}
    _, _, dk = by["PipesDockerClient"]
    assert dk["image"] == "python:3.11" and dk["env"] == {"A": "1"} and dk["container_kwargs"] == {"network_mode": "bridge"} and dk["extras"] == {"args": [1], "kwargs": {}}
    assert dk["command"][:2] == ["python3", "-c"] and "def in_docker(x):" in dk["command"][2] and "open_dagster_pipes" in dk["command"][2]
    _, _, k8 = by["PipesK8sClient"]
    assert k8["image"] == "python:3.11" and k8["namespace"] == "jobs" and k8["env"] == {"B": "2"} and k8["base_pod_meta"] == {"labels": {"team": "x"}} and k8["extras"] == {"args": [2], "kwargs": {}}
    assert res.asset_value(dg.AssetKey(["d", "in_docker"])) in (None, 41)


# ---- the container operators that already had a form keep it ----------------------------------------------------------------------------------------------------------

OPERATORS = '''
    import pendulum
    from airflow.decorators import dag
    from airflow.providers.docker.operators.docker import DockerOperator
    from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        DockerOperator(task_id="dk", image="alpine:3", command="echo hi", environment={"A": "1"})
        KubernetesPodOperator(task_id="kp", name="p", image="alpine:3", cmds=["echo"], arguments=["hi"], namespace="default")
    d()
'''


def test_docker_and_pod_operators_keep_their_shims(tmp_path):
    ir, rep, pkg = convert(tmp_path, OPERATORS)
    src = (pkg / "defs" / "d.py").read_text()
    assert "docker_run('alpine:3'" in src and "k8s_pod_run(context, image='alpine:3'" in src and "af_pipes_run" not in src
    assert not rep.get("external_files") and not any(c["kind"] == "stub" for c in rep["caveats"])
