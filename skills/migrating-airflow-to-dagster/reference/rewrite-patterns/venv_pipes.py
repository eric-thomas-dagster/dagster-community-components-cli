"""Pattern: PythonVirtualenvOperator / ExternalPythonOperator / @task.virtualenv / branch variants.
NATIVE: Dagster Pipes: `PipesSubprocessClient` runs the code in an interpreter you name (a venv's python) and the external code reports back with `dagster_pipes` (verified: a subprocess run materializes the asset
with the metadata it reports). DECLARE the requirements (a list in the module or a requirements file next to it) and FAIL LOUDLY when the interpreter is missing: never silently run the code in the current
interpreter. Alternatives: a separate code location with its own environment; plain Python only when the venv merely pins versions already installed. Building the venv at run time needs pip and a network, which
the measurement sandbox does not have: the original fails there too and its baseline is classified baseline-failed.
"""
import os
import sys
import textwrap
from pathlib import Path

import dagster as dg

REQUIREMENTS = ["numpy~=1.26.0"]                                        # the operator's `requirements`, declared
VENV_PYTHON = os.environ.get("TASK_VENV_PYTHON")                        # the interpreter of the prepared environment


def require_interpreter(path: str | None) -> str:
    if not path or not Path(path).exists():
        raise dg.Failure(description=f"the interpreter for this task's virtualenv ({path!r}) is missing; build it from {REQUIREMENTS} and set TASK_VENV_PYTHON: not falling back to the current interpreter")
    return path


EXTERNAL = Path(__file__).with_name("_venv_pipes_task.py")


@dg.asset
def venv_task(context: dg.AssetExecutionContext, pipes_subprocess_client: dg.PipesSubprocessClient):
    python = require_interpreter(VENV_PYTHON)
    return pipes_subprocess_client.run(command=[python, str(EXTERNAL)], context=context).get_materialize_result()


defs = dg.Definitions(assets=[venv_task], resources={"pipes_subprocess_client": dg.PipesSubprocessClient()})
