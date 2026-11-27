"""Pattern: PythonVirtualenvOperator / @task.virtualenv (NO equivalent) and ExternalPythonOperator / @task.external_python (PipesSubprocessClient).
1. @task.virtualenv builds an environment with pip WHEN THE TASK RUNS: a workaround for Airflow's one shared worker environment. Dagster has no per-step virtualenv. Run the function in the project's own
   environment and add the operator's `requirements` to the project's dependencies; a conflict means a separate code location (its own virtualenv or container; assets depend across code locations). Do NOT
   build a venv at run time. (`in_project_env` below: the plain form, with the requirements declared next to it.)
2. @task.external_python names a PRE-BUILT interpreter: `PipesSubprocessClient` runs a standalone script with that interpreter, passes parameters as `extras` (JSON, no pickling) and streams logs and metadata
   back while it runs. `external_python_task` below; the interpreter comes from `<TASK>_PYTHON` with a default, a missing one FAILS LOUDLY (never the current interpreter).
3. Containers (per-step image): PipesDockerClient / PipesK8sClient / PipesECSClient, or executor step tags: see venv_pipes.md (NOT run: those clients are not installed here).
Converter, same shape: the value comes back as the asset value (`dg.Output`) and as the metadata `airflow/return_value`; the interpreter override is `<TASK>_PYTHON` (`EXTRACT_PYTHON` here).
"""
import json
import os
import shutil
from pathlib import Path

import dagster as dg

PROJECT_REQUIREMENTS = ["numpy~=1.26.0"]         # a virtualenv operator's `requirements`: they belong in the project's dependencies (pyproject / requirements), not in a venv built at run time
PYTHON_ENV = "EXTRACT_PYTHON"                    # `<TASK>_PYTHON`
DEFAULT_PYTHON = "/opt/venvs/analytics/bin/python"                       # the operator's `python=`: a pre-built environment that has dagster-pipes and the function's packages
SCRIPT = Path(__file__).with_name("_venv_pipes_task.py")


def resolve_interpreter(env_var: str = PYTHON_ENV, default: str | None = DEFAULT_PYTHON) -> str:
    path = os.environ.get(env_var) or default
    if not path or not (Path(path).exists() or shutil.which(path)):
        raise dg.Failure(description=f"the interpreter ({path!r}) is missing: build it (with dagster-pipes and the function's packages) and set {env_var}; not falling back to the current interpreter")
    return path


@dg.asset
def in_project_env() -> dict:
    """@task.virtualenv: no equivalent. The function runs here, in the project's environment; its requirements are PROJECT_REQUIREMENTS (declare them in the project's dependencies)."""
    return {"count": 3, "total": 3}


@dg.asset
def external_python_task(context: dg.AssetExecutionContext, pipes_subprocess_client: dg.PipesSubprocessClient):
    """@task.external_python: the function runs in another interpreter; extras in, logs and metadata streamed, the return value out."""
    extras = json.loads(json.dumps({"n": 3, "label": "rows"}))           # JSON only
    res = pipes_subprocess_client.run(command=[resolve_interpreter(), str(SCRIPT)], context=context, extras=extras)
    msgs = [m for m in res.get_custom_messages() if isinstance(m, dict) and "return_value" in m]
    if not msgs:
        raise dg.Failure(description="the task ended without reporting a return value")
    value = msgs[-1]["return_value"]
    reported = {k: v for r in res.get_results() if isinstance(r, dg.MaterializeResult) for k, v in (r.metadata or {}).items()}      # what the script streamed
    return dg.Output(value, metadata={**reported, "airflow/return_value": dg.MetadataValue.json(value) if isinstance(value, (dict, list)) else value})


defs = dg.Definitions(assets=[in_project_env, external_python_task], resources={"pipes_subprocess_client": dg.PipesSubprocessClient()})
