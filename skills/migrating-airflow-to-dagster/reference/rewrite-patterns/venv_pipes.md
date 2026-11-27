# Virtualenv, external-interpreter and container operators

Dagster isolates dependencies at the code-location level (each location has its own virtualenv or container; assets depend across locations) or through Dagster Pipes. It has no per-step virtualenv built at run time.

| Airflow | Dagster | Status |
|---|---|---|
| `PythonVirtualenvOperator`, `@task.virtualenv`, `@task.branch_virtualenv` (pip install when the task runs) | **No direct equivalent; do not build a venv at run time.** Airflow's virtualenv task is a workaround for its shared worker environment. Run the function in the project's environment (`in_project_env` in `venv_pipes.py`) and add the operator's `requirements` to the project's dependencies. If they conflict with the project's or Dagster's, make the task its own code location (own virtualenv or container), or run it on a pre-built interpreter with Pipes (next row) | VERIFIED (the in-process asset runs); the conflict advice is a design statement, not run |
| `ExternalPythonOperator`, `@task.external_python` (a pre-built `python=`) | `PipesSubprocessClient` runs a standalone script with that interpreter (`external_python_task`): parameters in as `extras` (JSON, no pickling), logs and metadata streamed back while it runs, the return value comes back as a custom message. The interpreter path comes from an env var with a default; a missing path fails loudly | VERIFIED (run on Dagster 1.13.31 with a separate interpreter that has `dagster-pipes`) |
| `@task.docker`, `@task.kubernetes`, `DockerOperator` / `KubernetesPodOperator` running a function | Per-step image: `PipesDockerClient` (dagster-docker), `PipesK8sClient` (dagster-k8s), `PipesECSClient` (dagster-aws), same script and extras, the image needs Python and `dagster-pipes`. Or keep the step in-process and give it an image with executor step tags (`k8s_job_executor` / `ecs_executor`, tag `dagster-k8s/config` with `container_config.image`) | **UNVERIFIED**: no Docker daemon, cluster or ECS here, and `dagster-docker` / `dagster-k8s` are not installed in the test environment |
| An isolated worker queue (`queue=` / executor routing to a different image) | A code location per environment | design statement, not run |

## The external-interpreter form (run)

```python
res = pipes_subprocess_client.run(command=[resolve_interpreter(), str(SCRIPT)], context=context, extras={"n": 3, "label": "rows"})
```

`resolve_interpreter()` reads `EXTRACT_PYTHON` (the converter's `<TASK>_PYTHON`) and falls back to the operator's `python=` (here a default path). A missing interpreter raises `dg.Failure`: never swap in the current one. The script (`_venv_pipes_task.py`) imports only `dagster_pipes`:

```python
with open_dagster_pipes() as pipes:
    n = int(pipes.get_extra("n"))
    pipes.log.info(...)                                              # appears in the run's logs while the script runs
    pipes.report_asset_materialization(metadata={"rows": n})         # streamed metadata
    pipes.report_custom_message({"return_value": result})            # JSON only (Airflow pickled the return value)
```

The asset returns `dg.Output(value, metadata={..., "airflow/return_value": ...})`: an asset that opened a Pipes session must return the event itself, and the value is what a downstream asset reads. This is the shape the converter generates (`isolation_ops.af_pipes_run`): value as the asset value plus `airflow/return_value`; the interpreter override is `<TASK>_PYTHON`. Differences: the converter also passes `*args` / `**kwargs` as extras and the function's own name, and wraps the function unchanged; the pattern uses fixed extras and a small function. Arguments and the return value must be JSON; a callable that reads the Airflow task context (`ti`, `xcom_pull`) cannot run in another process: pass what it needs as extras.

## Container forms (UNVERIFIED, signatures as the converter uses them)

```python
from dagster_docker import PipesDockerClient
PipesDockerClient().run(context=context, image="repo/img:1", command=["python3", "-c", SCRIPT_TEXT], extras=extras, env={...}).get_results()

from dagster_k8s import PipesK8sClient
PipesK8sClient().run(context=context, image="repo/img:1", command=["python3", "-c", SCRIPT_TEXT], namespace="data", extras=extras, env={...})

@dg.asset(tags={"dagster-k8s/config": {"container_config": {"image": "repo/img:1"}}})      # with k8s_job_executor: one step, its own image
```

Not run here. The converter's generated calls were checked against a recording fake only. `PipesECSClient` has not been checked at all.

Decision record (virtualenv): `preserved` for an external interpreter or container (isolation kept) or `not preserved: <why>` for a virtualenv task run in the project's environment (name the requirements added and any conflict). The measurement sandbox has no pip or network, so the ORIGINAL virtualenv task fails there; the harness classifies such a DAG as baseline-failed (excluded from the shares).
