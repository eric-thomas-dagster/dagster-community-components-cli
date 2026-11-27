"""Operators that run code in another environment (virtualenv, external interpreter, container) for convert_ir.py.

Dagster has no per-step virtualenv that is built when the step runs. Isolating dependencies is done with (1) code locations (each its own environment or
container; assets depend across them), (2) Dagster Pipes (`PipesSubprocessClient` runs a standalone script with a pre-built interpreter; it streams logs and
metadata back and takes its parameters through `extras`, without pickling), (3) a container per step (`PipesDockerClient`, `PipesK8sClient`, the k8s and ECS
executors). So:

* `PythonVirtualenvOperator`, `@task.virtualenv` and the branch variants (a virtualenv built from `requirements` at run time) have no equivalent. The callable
  runs in-process in the project's own environment, the `requirements` are added to `converted_requirements.txt` (pinned as given) and a loud caveat says that
  conflicting pins need a separate code location. Simple conflicts (the same package pinned incompatibly by two tasks, or against Dagster's own
  requirements) are detected and named;
* `ExternalPythonOperator`, `@task.external_python` and the branch variants (a pre-built interpreter, `python=`) become `PipesSubprocessClient`: a small wrapper
  script holds the user's function and reports its return value back; the arguments travel as `extras` (JSON only). The interpreter path is a constant with an
  environment override (`<TASK>_PYTHON`);
* `@task.docker` and `@task.kubernetes` (a function run inside an image) become `PipesDockerClient` / `PipesK8sClient` with the same wrapper script as the
  container command (the image needs Python and `dagster-pipes`, as Airflow's version needs Python in the image). `DockerOperator` and `KubernetesPodOperator`
  (an image that runs its own command) keep their existing forms (`provider_ops_more.py`, `provider_ops.py`).

The functions are pure on the IR (no Dagster, no Airflow import), so the mapping is testable without them. `plan` raises ValueError with the reason when a task
cannot be converted; convert_ir.py turns it into its usual marked stub.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

import provider_ops as P

VENV = {"PythonVirtualenvOperator", "_PythonVirtualenvDecoratedOperator"}
VENV_BRANCH = {"BranchPythonVirtualenvOperator", "_BranchPythonVirtualenvDecoratedOperator"}
EXTERNAL = {"ExternalPythonOperator", "_PythonExternalDecoratedOperator"}
EXTERNAL_BRANCH = {"BranchExternalPythonOperator", "_BranchExternalPythonDecoratedOperator"}
CONTAINER = {"_DockerDecoratedOperator": "docker", "_KubernetesDecoratedOperator": "k8s"}
PIPES = EXTERNAL | EXTERNAL_BRANCH | set(CONTAINER)             # run through a Pipes client
IN_PROCESS = VENV | VENV_BRANCH                                  # no equivalent: run in the project's environment


def kind_of(ty: str) -> str | None:
    if ty in IN_PROCESS:
        return "venv"
    if ty in EXTERNAL | EXTERNAL_BRANCH:
        return "external"
    return CONTAINER.get(ty)


# ---------------------------------------------------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------------------------------------------------
def parse_requirements(v: Any) -> tuple[list[str], list[str]]:
    """(requirement lines, lines that cannot be written into a requirements file as a pin: options, `-r file`, local paths)."""
    if v is None:
        return [], []
    lines = v.splitlines() if isinstance(v, str) else [str(x) for x in v]
    good: list[str] = []
    bad: list[str] = []
    for ln in lines:
        ln = ln.split(" #")[0].strip()
        if not ln or ln.startswith("#"):
            continue
        (bad if ln.startswith("-") or "/" in ln.split(";")[0] or "://" in ln else good).append(ln)
    return good, bad


def dagster_requirements() -> list[str]:
    """What the installed `dagster`, `dagster-shared` and `dagster-pipes` require (no extras); empty when they are not installed in the converter's environment."""
    try:
        from importlib.metadata import PackageNotFoundError, requires
        from packaging.requirements import Requirement
        out = []
        for dist in ("dagster", "dagster-shared", "dagster-pipes"):
            try:
                reqs = requires(dist) or []
            except PackageNotFoundError:
                continue
            for r in reqs:
                req = Requirement(r)
                if (req.marker is None or req.marker.evaluate({"extra": ""})) and not req.name.startswith("dagster"):
                    out.append(str(req))
        return out
    except Exception:       # noqa: BLE001  not installed, or no packaging: the check against Dagster is skipped
        return []


def _candidates(specs: list[Any]) -> list[Any]:
    from packaging.version import InvalidVersion, Version
    seen: dict[str, Any] = {}
    for s in specs:
        for sp in s:
            try:
                v = Version(sp.version.replace(".*", ""))
            except InvalidVersion:
                continue
            rel = list(v.release) + [0, 0, 0]
            for cand in (v.release, (*rel[:2], rel[2] + 1), (rel[0], rel[1] + 1), (rel[0], rel[1] + 1, 0), (rel[0] + 1,), (rel[0] + 1, 0, 1)):
                seen[".".join(map(str, cand))] = Version(".".join(map(str, cand)))
            seen[str(v)] = v
    return list(seen.values())


def req_conflicts(entries: list[tuple[str, str]], base: list[str] | None = None) -> list[str]:
    """Messages for requirements that cannot all be installed together. `entries` are (label, requirement); `base` are requirements that always apply (Dagster's own).
    Simple conflicts only: two version specifiers of one package with no version that satisfies both, judged on the versions the specifiers themselves name
    and their neighbours (a package that is not pinned, or pinned the same way, never conflicts; markers and extras are not evaluated)."""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.utils import canonicalize_name
    except ImportError:
        return []
    parsed: list[tuple[str, str, Any, bool]] = []
    for label, text in entries:
        try:
            parsed.append((label, text, Requirement(text), False))
        except InvalidRequirement:
            continue
    for text in base or []:
        try:
            parsed.append(("Dagster", text, Requirement(text), True))
        except InvalidRequirement:
            continue
    by_pkg: dict[str, list[tuple[str, str, Any, bool]]] = {}
    for e in parsed:
        by_pkg.setdefault(canonicalize_name(e[2].name), []).append(e)
    out: list[str] = []
    for pkg, es in sorted(by_pkg.items()):
        if len({e[1] for e in es}) < 2 or not any(not e[3] for e in es):
            continue
        specs = [e[2].specifier for e in es if str(e[2].specifier)]
        if len(specs) < 2:
            continue
        cands = _candidates(specs)
        if any(all(s.contains(c, prereleases=True) for s in specs) for c in cands):
            continue
        # name the first incompatible pair, with a user-side requirement in it
        for i, a in enumerate(es):
            for b in es[i + 1:]:
                if a[3] and b[3]:
                    continue
                pair = [a[2].specifier, b[2].specifier]
                if str(a[2].specifier) and str(b[2].specifier) and not any(all(s.contains(c, prereleases=True) for s in pair) for c in _candidates(pair)):
                    where = lambda e: ("Dagster's own requirement" if e[3] else f"task {e[0]}")      # noqa: E731
                    out.append(f"`{a[1]}` ({where(a)}) and `{b[1]}` ({where(b)}) cannot be installed in one environment")
                    break
            else:
                continue
            break
        else:
            out.append(f"the requirements of {sorted({e[0] for e in es})} for `{pkg}` have no version in common")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# the plan for one task
# ---------------------------------------------------------------------------------------------------------------------
_ENV_USED: dict[str, tuple[str, str]] = {}


def reset() -> None:
    _ENV_USED.clear()


def python_env_name(dag_id: str, tid: str) -> str:
    """`<TASK>_PYTHON`; `<DAG>_<TASK>_PYTHON` when another DAG's task already took that name."""
    base = re.sub(r"\W", "_", tid).upper() + "_PYTHON"
    if _ENV_USED.get(base, (dag_id, tid)) != (dag_id, tid):
        base = re.sub(r"\W", "_", dag_id).upper() + "_" + base
    _ENV_USED[base] = (dag_id, tid)
    return base


def _a(t: dict[str, Any]) -> dict[str, Any]:
    return P._a(t)


def _plain(v: Any) -> bool:
    s = json.dumps(v, default=str)
    return "{{" not in s and "{%" not in s and "xcom_from" not in s and '"repr"' not in s


SCRIPT = '''"""Airflow task `{tid}` of DAG `{dag_id}` ({op}), written by convert_ir.py: the task's own function, unchanged, run by Dagster Pipes{where}.
Arguments arrive as Pipes extras (JSON), the return value goes back as a custom message that the Dagster asset records (Airflow pickles both)."""

{support}{code}


if __name__ == "__main__":
    import json

    from dagster_pipes import open_dagster_pipes

    with open_dagster_pipes() as pipes:
        args = pipes.get_extra("args") or []
        kwargs = pipes.get_extra("kwargs") or {{}}
        pipes.log.info("running {tid}")
        result = {fn}(*args, **kwargs)
        try:
            json.dumps(result)
        except TypeError as e:
            raise TypeError("the return value of {tid} is not JSON-serialisable (Dagster Pipes carries JSON): " + str(e)) from e
        pipes.report_custom_message({{"return_value": result}})
'''

DOCKER_CARRIED = {"image", "environment", "network_mode", "user", "working_dir", "python_command", "command"}
DOCKER_INTERNAL = {"mount_tmp_dir", "tmp_dir", "host_tmp_dir", "retrieve_output", "retrieve_output_path", "xcom_all", "serializer", "expect_airflow", "auto_remove",
                   "pip_install_options", "use_dill", "skip_on_exit_code", "string_args", "templates_dict", "op_args", "op_kwargs", "templates_exts", "show_return_value_in_logs"}
DOCKER_WATCH = ("mounts", "ports", "port_bindings", "privileged", "mem_limit", "cpus", "shm_size", "docker_conn_id", "docker_url", "dns", "dns_search", "cap_add", "extra_hosts",
                "device_requests", "container_name", "hostname", "domainname", "ipc_mode", "force_pull", "entrypoint", "tty", "tls_ca_cert", "tls_client_cert", "tls_client_key")
DOCKER_DEFAULT = {"cpus": 1.0, "docker_url": "unix://var/run/docker.sock", "privileged": False, "force_pull": False, "tty": False}
K8S_CARRIED = {"image", "namespace", "env_vars", "labels", "annotations", "in_cluster", "config_file", "cluster_context", "cmds", "arguments"}
K8S_WATCH = ("volumes", "volume_mounts", "secrets", "env_from", "configmaps", "image_pull_policy", "image_pull_secrets", "service_account_name", "node_selector", "tolerations",
             "affinity", "container_resources", "ports", "init_containers", "full_pod_spec", "pod_template_file", "pod_template_dict", "hostnetwork", "security_context",
             "container_security_context", "priority_class_name", "active_deadline_seconds", "kubernetes_conn_id", "host_aliases", "dns_config", "dnspolicy", "pod_runtime_info_envs")
K8S_DEFAULT = {"kubernetes_conn_id": "kubernetes_default"}


def _not_carried(a: dict[str, Any], names: tuple[str, ...], defaults: dict[str, Any]) -> list[str]:
    return [n for n in names if not P._empty(a.get(n)) and a.get(n) != defaults.get(n)]


def plan(t: dict[str, Any], tid: str, dag_id: str, code: str, sup: list[str], meta: dict[str, Any]) -> dict[str, Any]:
    """The plan for one isolated task: {kind, fn, requirements, env_vars, caveats, notes, spec, script}. `spec` and `script` are set for the Pipes forms:
    `script` is (path relative to the package, text), `spec` the constant the generated step hands to `af_pipes_run`. Raises ValueError when the task cannot be converted."""
    ty = t["type"]
    kind = kind_of(ty)
    a = _a(t)
    out: dict[str, Any] = {"kind": kind, "requirements": [], "env_vars": None, "caveats": [], "notes": [], "spec": None, "script": None, "extra_requirements": []}
    op = {"venv": "virtualenv task", "external": "external-Python task", "docker": "@task.docker", "k8s": "@task.kubernetes"}[kind]
    if kind == "venv":
        reqs, bad = parse_requirements((t.get("template_fields") or {}).get("requirements") if isinstance((t.get("template_fields") or {}), dict) else None)
        reqs_ir = a.get("requirements")
        if not reqs and reqs_ir:
            reqs, bad = parse_requirements(reqs_ir)
        out["requirements"] = reqs
        env = a.get("env_vars")
        out["env_vars"] = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) and env else None
        why = []
        if reqs:
            why.append(f"its requirements {reqs} were added to converted_requirements.txt (pinned as given) and are now dependencies of the whole project")
        if bad:
            why.append(f"these requirement lines cannot be pinned in a requirements file and were NOT added: {bad}")
        if a.get("system_site_packages") is False:
            why.append("system_site_packages=False: the isolation from the project's installed packages is not reproduced")
        pv = a.get("python_version")
        if pv:
            same = str(pv).split(".")[:2] == [str(sys.version_info[0]), str(sys.version_info[1])]
            why.append(f"python_version={pv!r}: the function now runs on the project's Python" + ("" if same else f" (this conversion ran on {sys.version_info[0]}.{sys.version_info[1]}); if it cannot run there it needs a separate code location or a Pipes subprocess on a pre-built interpreter of that version"))
        if a.get("pip_install_options") or a.get("index_urls"):
            why.append("pip_install_options / index_urls are not applied: the project's own installer settings decide where the requirements come from")
        if a.get("inherit_env") is False:
            why.append("inherit_env=False is not reproduced: the process environment is the project's")
        if reqs or bad or pv or a.get("system_site_packages") is False or a.get("pip_install_options") or a.get("index_urls"):
            out["caveats"].append(
                f"{ty}: DAGSTER HAS NO DYNAMIC PER-STEP VIRTUALENV (Airflow builds one with pip when the task runs), and this conversion cannot build one per task. The callable runs "
                "in-process in the project's own environment; " + "; ".join(why) + ". If these requirements conflict with other dependencies of the project or with Dagster's own, "
                "move the task to a separate Dagster code location (its own environment or container; assets can depend across code locations), or run the function on a "
                "pre-built interpreter with Dagster Pipes (PipesSubprocessClient, as ExternalPythonOperator converts)")
        else:
            out["notes"].append(f"{tid}: {ty} had no extra requirements; the callable runs in-process in the project's environment (no per-task virtualenv exists in Dagster)")
        return out

    # ---- the Pipes forms: the function becomes a standalone script ----
    if meta.get("needs_ctx") or meta.get("needs_ti") or meta.get("needs_xcom"):
        raise ValueError(f"{ty}: the callable reads the Airflow task context (ti, xcom_pull, context names), which a separate process cannot get: pass the values it needs as op_args, op_kwargs or templates_dict")
    if meta.get("async"):
        raise ValueError(f"{ty}: an async callable is not supported in a separate process")
    if meta.get("conds"):
        raise ValueError(f"{ty}: run_if / skip_if on a task run in a separate process is not supported")
    m = re.match(r"(?:async\s+)?def\s+(\w+)", code)
    fn = m.group(1) if m else "main"
    rel = f"external_python/{re.sub(r'[^0-9A-Za-z_]', '_', dag_id)}__{re.sub(r'[^0-9A-Za-z_]', '_', tid)}.py"
    where = {"external": " with a pre-built interpreter", "docker": " in a container", "k8s": " in a Kubernetes pod"}[kind]
    script = SCRIPT.format(tid=tid, dag_id=dag_id, op=ty, where=where, fn=fn, code=code.rstrip() + "\n", support="".join(s.rstrip() + "\n\n" for s in sup))
    out["script"] = (rel, script)
    name = f"{dag_id}.{tid}"
    if kind == "external":
        py = (t.get("template_fields") or {}).get("python") or a.get("python")
        env = a.get("env_vars")
        out["spec"] = {"kind": "external", "name": name, "script": rel, "python": py if isinstance(py, str) else None, "python_env": python_env_name(dag_id, tid),
                       "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) and env else {}}
        out["notes"].append(f"{tid}: runs with Dagster Pipes (PipesSubprocessClient) on the interpreter {py!r} from the Airflow DAG, overridable with the environment variable "
                            f"{out['spec']['python_env']}; that interpreter needs `dagster-pipes` (and the packages the function imports); the script is {rel}; arguments and the return value travel as JSON "
                            "(Airflow pickles them) and a value that is not JSON fails the step with a message naming it")
        if a.get("inherit_env") is False:
            out["caveats"].append(f"{ty}: inherit_env=False is not reproduced: the subprocess keeps the project's environment")
        if a.get("skip_on_exit_code"):
            out["caveats"].append(f"{ty}: skip_on_exit_code={a['skip_on_exit_code']} is not applied (a non-zero exit fails the step)")
        return out
    if kind == "docker":
        if not isinstance(a.get("image"), str):
            raise ValueError(f"{ty}: image is not a string")
        envd = a.get("environment") or {}
        cont = {k: a[k] for k in ("network_mode", "user", "working_dir") if a.get(k)}
        if not _plain([envd, cont, a.get("image")]) or not isinstance(envd, dict):
            raise ValueError(f"{ty}: the image or environment holds a template or a value that cannot be written as plain data")
        out["spec"] = {"kind": "docker", "name": name, "script": rel, "image": a["image"], "python": a.get("python_command") or "python3", "env": {str(k): str(v) for k, v in envd.items()}, "container_kwargs": cont}
        nc = _not_carried({k: a.get(k) for k in DOCKER_WATCH}, DOCKER_WATCH, DOCKER_DEFAULT)
        out["extra_requirements"] = ["dagster-docker"]
        out["notes"].append(f"{tid}: runs with Dagster Pipes (PipesDockerClient from dagster-docker) in {a['image']}; the image needs Python ({out['spec']['python']}) and `dagster-pipes`; the function is the container command")
        if nc:
            out["caveats"].append(f"{ty}: these DockerOperator settings are not carried to PipesDockerClient: {nc}")
        out["caveats"].append(f"{ty}: UNVERIFIED against a live Docker daemon: the generated call ran through the real PipesDockerClient and docker-py against a fake Docker Engine API (the container was a local subprocess), not a real container")
        return out
    # k8s
    if not isinstance(a.get("image"), str):
        raise ValueError(f"{ty}: image is not a string")
    try:
        envl = P._env_list(a.get("env_vars"))
        if any("value_from" in e for e in envl):
            raise ValueError("an env var reads from a secret, config map or field reference (value_from)")
        meta_k = {k: P.k8s_obj(a.get(k)) for k in ("labels", "annotations") if not P._empty(P.k8s_obj(a.get(k)))}
    except ValueError as e:
        raise ValueError(f"{ty}: {e}") from e
    if not _plain([envl, meta_k, a.get("image"), a.get("namespace")]):
        raise ValueError(f"{ty}: the image, namespace, environment or labels hold a template or a value that cannot be written as plain data")
    out["spec"] = {"kind": "k8s", "name": name, "script": rel, "image": a["image"], "namespace": a.get("namespace") or None, "python": "python3",
                   "env": {e["name"]: str(e.get("value", "")) for e in envl}, "base_pod_meta": meta_k or None,
                   "client": ({"load_incluster_config": True} if a.get("in_cluster") else         # the real PipesK8sClient refuses an in-cluster config together with a kubeconfig file or context
                              {k: v for k, v in {"kubeconfig_file": a.get("config_file") or None, "kube_context": a.get("cluster_context") or None}.items() if v is not None})}
    nc = _not_carried({k: a.get(k) for k in K8S_WATCH}, K8S_WATCH, K8S_DEFAULT)
    out["extra_requirements"] = ["dagster-k8s"]
    out["notes"].append(f"{tid}: runs with Dagster Pipes (PipesK8sClient from dagster-k8s) in {a['image']}; the image needs Python and `dagster-pipes`; the function is the container command")
    if nc:
        out["caveats"].append(f"{ty}: these KubernetesPodOperator settings are not carried to PipesK8sClient: {nc}")
    out["caveats"].append(f"{ty}: UNVERIFIED against a live Kubernetes cluster: the generated call ran through the real PipesK8sClient and kubernetes client against a fake Kubernetes API server (the pod was a local subprocess), not a real pod")
    return out


RUNTIME = '''

# ---- code run in another environment (isolation_ops.py) ----------------------------------------------------------------------------------------------
def af_with_env(fn, env_vars):
    """A virtualenv task's `env_vars`, set for the call (the callable runs in-process, so the variables are put back afterwards)."""
    import functools
    import os

    @functools.wraps(fn)
    def run(*args, **kwargs):
        old = {k: os.environ.get(k) for k in env_vars}
        os.environ.update({k: str(v) for k, v in env_vars.items()})
        try:
            return fn(*args, **kwargs)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return run


def af_typed_result(fn):
    """The op of a graph form that ran a Pipes session: Dagster then wants the op's result as an Output, whatever the body did with the value (branch, guard, skip)."""
    import functools

    @functools.wraps(fn)
    def run(context, *args, **kwargs):
        return dg.Output(fn(context, *args, **kwargs))
    return run


def af_pipes_run(context, spec, args, kwargs, keep=True, op=False):
    """Run a task's function in another environment through Dagster Pipes and hand back its return value.
    external: PipesSubprocessClient with a pre-built interpreter (the Airflow `python`, or the environment variable `spec['python_env']`); docker: PipesDockerClient;
    k8s: PipesK8sClient. The arguments go as Pipes `extras` and must be JSON; the function's return value comes back as a custom message and is recorded as the
    asset's value (`keep`) and as the metadata `airflow/return_value`. A missing interpreter fails loudly: it never falls back to the current one."""
    import os
    import shutil
    name = spec["name"]
    try:
        extras = json.loads(json.dumps({"args": list(args), "kwargs": dict(kwargs)}))
    except TypeError as e:
        raise dg.Failure(description=f"{name}: the arguments cannot be passed to the other environment: Dagster Pipes extras are JSON only ({e}); Airflow pickled them") from e
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), spec["script"])
    if spec["kind"] == "external":
        py = os.environ.get(spec["python_env"]) or spec.get("python")
        if not py or not (os.path.exists(py) or shutil.which(py)):
            raise dg.Failure(description=f"the interpreter for {name} ({py!r}) is missing: build it (with dagster-pipes and the function's packages) and set {spec['python_env']}; not falling back to the current interpreter")
        res = dg.PipesSubprocessClient().run(command=[py, path], context=context, extras=extras, env=spec.get("env") or None)
    else:
        with open(path) as f:
            command = [spec["python"], "-c", f.read()]
        if spec["kind"] == "docker":
            from dagster_docker import PipesDockerClient
            res = PipesDockerClient().run(context=context, image=spec["image"], command=command, env=spec.get("env") or None, extras=extras,
                                          container_kwargs=spec.get("container_kwargs") or None)
        else:
            from dagster_k8s import PipesK8sClient
            res = PipesK8sClient(**(spec.get("client") or {})).run(context=context, image=spec["image"], command=command, namespace=spec.get("namespace"),
                                                                    env=spec.get("env") or None, extras=extras, base_pod_meta=spec.get("base_pod_meta"))
    msgs = [m for m in res.get_custom_messages() if isinstance(m, dict) and "return_value" in m]
    if not msgs:
        raise dg.Failure(description=f"{name} ended without reporting a return value (the wrapper script reports it as the last step)")
    value = msgs[-1]["return_value"]
    meta = {} if value is None else {"airflow/return_value": (dg.MetadataValue.json(value) if isinstance(value, (dict, list)) else value)}
    if op:                                                  # a graph form: the op returns the value (af_typed_result wraps it), the metadata goes on its output
        if meta:
            context.add_output_metadata(meta)
        return value
    return dg.Output(value, metadata=meta) if keep else dg.MaterializeResult(metadata=meta)      # an asset that opened a Pipes session must return the event itself
'''
