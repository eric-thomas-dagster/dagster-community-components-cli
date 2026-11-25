# Airlift: observe, then proxy, side by side with Airflow

`dagster-airlift` lets Dagster and Airflow run side by side. This page is what was checked against `dagster-airlift` 0.29.31 (with `dagster` 1.13.31 and, for the Airflow side, Apache Airflow 2.10.5) and what is documentation only. Each claim is labelled **verified** (run in a scratch project), **source** (read in the installed package, not run) or **documentation** (a vendor page, cited).

Stages, as this skill uses the names (see [tiers.md](tiers.md) for when to pick each):

| Stage | What it does | Changes Airflow? |
|---|---|---|
| **Peer** | Dagster reads an Airflow instance's REST API and shows each DAG as an asset with its run history | No |
| **Observe** | Peer, plus a mapping of Airflow tasks to Dagster assets, so runs of those tasks are recorded as materializations of the assets | No |
| **Proxy** | A task in Airflow is replaced by an operator that runs the mapped Dagster assets instead; Airflow still schedules and shows it | Yes (the DAG file) |

None of these retires Airflow. They give lineage and a task-by-task path; the native rewrite (this skill's converter) is how the Airflow code goes away.

## Install

- **Dagster side:** `pip install dagster-airlift` (extras defined by the package: `core`, `mwaa` for the MWAA backend, `in-airflow`, `test`, `tutorial`) (**verified**: package metadata). The component needs `dagster-dg-cli` only for `dg`.
- **Airflow side (proxy only):** `pip install "dagster-airlift[in-airflow]"` in the environment the Airflow scheduler and workers use (**verified**: it installed beside Airflow 2.10.5). `dagster_airlift.in_airflow` refuses to import without Airflow (**verified**).

## Peer and observe: `AirflowInstanceComponent`

Scaffold it (**verified**; the scaffolder needs both `name` and `auth_type`, and it only supports `basic_auth`: the MWAA branch raises `Unsupported auth type`, **source**):

```bash
dg scaffold defs dagster_airlift.core.components.AirflowInstanceComponent prod_airflow \
  --json-params '{"name": "prod_airflow", "auth_type": "basic_auth"}'
```

The registered type is `dagster_airlift.core.components.AirflowInstanceComponent` (`dg list components` shows it; the short form `dagster_airlift.AirflowInstanceComponent` is not registered, **verified**). The scaffold writes:

```yaml
type: dagster_airlift.core.components.AirflowInstanceComponent

attributes:
  name: prod_airflow
  auth:
    type: basic_auth
    webserver_url: '{{ env("AIRFLOW_WEBSERVER_URL") }}'
    username: '{{ env("AIRFLOW_USERNAME") }}'
    password: '{{ env("AIRFLOW_PASSWORD") }}'
```

Fields of `attributes` (from the component's JSON schema, **verified**): `name` and `auth` (required); `filter`; `mappings`; `source_code_retrieval_enabled`; `defs_state`. `auth` is `basic_auth` (`webserver_url`, `username`, `password`) or `mwaa` (`env_name` required; `region_name`, `profile_name`, `aws_account_id`, `aws_access_key_id`, `aws_secret_access_key`; the resolver also passes `aws_session_token`, **source**). `filter` takes `dag_id_ilike`, `airflow_tags`, `retrieve_datasets`, `dataset_uri_ilike`.

Task-to-asset `mappings` (this is the observe step), loaded in the scratch project and checked to produce the two assets:

```yaml
attributes:
  name: prod_airflow
  auth: { ... }
  source_code_retrieval_enabled: false
  mappings:
    - dag_id: orders
      task_mappings:
        - task_id: extract
          assets:
            - spec:
                key: raw/orders
        - task_id: load
          assets:
            - spec:
                key: warehouse/orders
                description: orders loaded into the warehouse
```

- `assets: - spec: {key: ...}` creates an asset for the task (**verified**; `spec` takes the usual asset spec fields, and a bare string is rejected, **verified**). A `dag_id` entry may carry `assets` of its own (a DAG-level mapping, **source**).
- `assets: - by_key: <key>` points the task at an asset that already exists in Dagster. The component looks for it in **the component's own subdirectories**, not the project at large (**source**: `defs_from_subdirs`); a key that is not found raises `Asset with key ... not found in definitions` (**verified**). Putting a Python asset module in a subfolder of the component did not load in my scratch project (`No component found for loc`), so the working layout for `by_key` is **not verified**: use `spec` unless you have shown the other form loads in your project.
- The component is state-backed: it fetches the Airflow state (DAGs, tasks, datasets) and stores it; in `dg dev` it refreshes on load, in a deployment run `dg utils refresh-defs-state` at build time (**verified** for loading with `DAGSTER_IS_DEV_CLI=1`; the CLI command exists, **verified**). It also adds a monitoring sensor named `<name>__airflow_monitoring_job_sensor` (**verified**) that turns Airflow runs into materializations.
- What was run: the YAML above, against a small stand-in for Airflow's REST API (`scripts/tests/fixtures_airlift/stub_airflow.py`: two DAGs, three tasks, no runs). That shows the component loads and builds the mapped assets and the sensor. It does **not** show that run history, task-instance reads, or timing translate correctly against a real Airflow: **not verified**.

### Authentication backends

| Service | Backend | Status |
|---|---|---|
| Basic auth (self-managed Airflow with the basic auth API backend enabled) | `auth: {type: basic_auth, ...}`, or `AirflowBasicAuthBackend(webserver_url, username, password)` | loaded (**verified** with the stand-in; not against a real server) |
| MWAA | `auth: {type: mwaa, env_name: ...}` or `MwaaSessionAuthBackend(mwaa_client, env_name)` (needs `dagster-airlift[mwaa]` and `boto3`); it calls `create_web_login_token`, posts the token to `https://<host>/aws_mwaa/login` and uses the `session` cookie (**source**). MWAA's REST API needs Airflow 2.4.3 or later ([MWAA: REST API](https://docs.aws.amazon.com/mwaa/latest/userguide/access-mwaa-apache-airflow-rest-api.html), **documentation**) | **source** only; not run against MWAA |
| Cloud Composer, Astro, anything else | subclass `AirflowAuthBackend` and implement `get_session()` (a `requests.Session` that is already authenticated) and `get_webserver_url()` (**source**); then build `AirflowInstance(auth_backend=..., name=...)` in Python (the YAML component only takes `basic_auth` and `mwaa` in this version, **source**) | skeleton below: **not run** |

```python
import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession
from dagster_airlift.core import AirflowAuthBackend


class ComposerAuthBackend(AirflowAuthBackend):
    """Cloud Composer: Google credentials with the cloud-platform scope; the base URL is config.airflowUri."""
    def __init__(self, airflow_uri: str):
        self._uri = airflow_uri.rstrip("/")
    def get_session(self) -> requests.Session:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(creds)
    def get_webserver_url(self) -> str:
        return self._uri


class AstroAuthBackend(AirflowAuthBackend):
    """Astro: a Deployment, Workspace or Organization API token as a Bearer header; the base URL is the Deployment URL."""
    def __init__(self, deployment_url: str, token: str):
        self._url, self._token = deployment_url.rstrip("/"), token
    def get_session(self) -> requests.Session:
        s = requests.Session()
        s.headers["Authorization"] = f"Bearer {self._token}"
        return s
    def get_webserver_url(self) -> str:
        return self._url
```

The Composer facts (Google credentials through `AuthorizedSession`, `config.airflowUri` as the base URL, a new API user getting the `Op` role) are from [Composer: access the Airflow REST API](https://docs.cloud.google.com/composer/docs/composer-3/access-airflow-api); the Astro facts (token types, `Authorization: Bearer`, the Deployment URL from `astro deployment inspect -n <name> --key metadata.airflow_api_url`) from [Astro: Airflow API](https://www.astronomer.io/docs/astro/airflow-api). The registry (`dagster-community-components` 0.10.187) already has `composer_airflow_auth_backend` (`ComposerAirflowAuthBackendResourceComponent`, field `airflow_url`) that returns such a backend; its README says Composer's API needs an OIDC ID token whose audience is the Airflow URL, which is a different mechanism from the `AuthorizedSession` the Composer 3 page shows, so test both against your environment (I ran neither). The registry also has `airlift_dag_assets` (mirror a DAG as assets via dagster-airlift; not tried). Read tokens and secrets from the environment, never from a file in the repo. See [hosted-airflow.md](hosted-airflow.md).

## Proxy

1. **Scaffold the proxied state before adding the proxy call.** `dagster-airlift proxy scaffold` loads the DAG folder with Airflow's DagBag, so a DAG file that already calls `proxying_to_dagster` on a missing `proxied_state` folder makes the scaffold fail with `Error parsing proxied state yaml` (**verified**; it is the order of the two steps, not a bug in either). Run it with `AIRFLOW_HOME` and `AIRFLOW__CORE__DAGS_FOLDER` set as for the scheduler: it writes `<dags folder>/proxied_state/<dag_id>.yaml`, every task `proxied: false` (**verified**):

   ```yaml
   tasks:
   - id: extract
     proxied: false
   - id: load
     proxied: false
   ```

2. **Add the call at the bottom of each DAG file** (or once in a shared module that receives `globals()`):

   ```python
   from pathlib import Path
   from dagster_airlift.in_airflow import proxying_to_dagster
   from dagster_airlift.in_airflow.proxied_state import load_proxied_state_from_yaml

   proxying_to_dagster(global_vars=globals(), proxied_state=load_proxied_state_from_yaml(Path(__file__).parent / "proxied_state"))
   ```

   With every task `proxied: false` the DAG is unchanged (**verified**: the tasks stayed `PythonOperator`).

3. **Flip one task.** Set `proxied: true` for it. Airflow then builds `DefaultProxyTaskToDagsterOperator` in its place (**verified**). The operator reads the Dagster URL from the environment variable `DAGSTER_URL` (**source**; a custom operator class can read another, as the docstring of `proxying_to_dagster` shows), queries Dagster's `<DAGSTER_URL>/graphql` for the asset nodes whose metadata (`dagster-airlift/task-mapping`) names this DAG and task, launches a run of them and waits for it (**source**), so the Airflow workers must be able to reach Dagster over the network and, on Dagster+, send an API token (override `get_dagster_session`). The call from Airflow to a running Dagster was **not run**. A whole DAG can be proxied with a top-level `proxied: true` in the DAG's YAML instead of `tasks:` (the two keys are mutually exclusive, **source**).

4. **Roll back** by setting `proxied: false` again (**verified**: the task is a `PythonOperator` again on the next parse). The Airflow code is still there, which is the point of this stage; keep it until the cutover checklist says otherwise ([cutover-checklist.md](cutover-checklist.md)).

Two things to settle before flipping a task: the Dagster assets it proxies to must produce what the Airflow task produced (use the parity harness, [parity.md](parity.md)), and the Airflow schedule stays authoritative, so a Dagster schedule on the same assets would double-run them.

## Airflow 3

**This version of `dagster-airlift` cannot read an Airflow 3 instance.** The evidence:

- `AirflowInstance.get_api_url()` returns `<webserver>/api/v1` (**source**, and asserted by `tests/test_airlift.py`); every call the component makes goes through it.
- Airflow 3 replaces `/api/v1` with `/api/v2` ([Upgrading to Airflow 3](https://airflow.apache.org/docs/apache-airflow/stable/installation/upgrading_to_airflow3.html), **documentation**: "REST API (`/api/v1`) replaced ... use the modern FastAPI-based stable `/api/v2`"); Astro, Composer and MWAA each say the same for their Airflow 3 environments ([Astro](https://www.astronomer.io/docs/astro/airflow-api), [Composer](https://docs.cloud.google.com/composer/docs/composer-3/access-airflow-api), [MWAA](https://docs.aws.amazon.com/mwaa/latest/userguide/access-mwaa-apache-airflow-rest-api.html)).
- The MWAA backend logs in at `/aws_mwaa/login` and reads a `session` cookie (**source**); MWAA documents `/pluginsv2/aws_mwaa/login` and a `_token` bearer for Airflow 3 (**documentation**).

This is inferred from the source and the vendors' documentation. It was not run against an Airflow 3 server, and a newer `dagster-airlift` may add `/api/v2` support: check the release notes and re-run `tests/test_airlift.py`, which fails when the hard-coded path changes. The proxy side was not tried on Airflow 3 either (it imports `airflow.models.BaseOperator` and `DAG`).

**For an Airflow 3 source, use this skill's converter**, whose extractor (`airflow_ir.py`) runs the DAG files instead of calling the REST API, and was run on Airflow 2.2 to 3.4 ([compatibility.md](compatibility.md)). The cost of that route is that nothing observes Airflow's own run history while you migrate: use the parity harness and a side-by-side run instead.

## Pick the stage

- You need lineage and run visibility this week, and Airflow is 2.x: **peer**, then **observe** with `spec` mappings for the DAGs that matter.
- You need to move execution of some tasks to Dagster before their Airflow code is rewritten (for example a team that owns the assets already): **proxy**, one task at a time, rollback ready.
- You are migrating for good and the DAGs convert: skip to the converter ([conversion.md](conversion.md)); use peer or observe only to compare run history during the side-by-side window.
- Airflow 3: the converter.
