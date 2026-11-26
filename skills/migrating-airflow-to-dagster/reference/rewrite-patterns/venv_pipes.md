# Virtualenv / external-interpreter operators

Native: Dagster Pipes (`venv_pipes.py`, executed): `PipesSubprocessClient` runs the code with the venv's python; it reports back with `dagster_pipes`. **Declare the requirements and fail loudly when the interpreter is
missing**: `require_interpreter` raises `dg.Failure`; never swap in the current interpreter silently. Alternatives: a separate code location with its own environment; plain Python only when the venv just pins versions
that are already installed (then say so). The measurement sandbox has no pip or network, so the ORIGINAL fails on its virtualenv task there too; the harness classifies such a DAG as baseline-failed (excluded from the
shares), and a rewrite that fails loudly for a missing interpreter is correct. Header: `<task>: replaced by asset `<key>` (Pipes, interpreter from TASK_VENV_PYTHON)`. Decision record (virtualenv): `preserved` (isolation
and requirements declared) or `not preserved: <why>`. Not tested: building a real venv (no network here); tested: the Pipes run in a subprocess and the loud failure for a missing interpreter.
