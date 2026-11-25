# Hosted Airflow: MWAA, Cloud Composer, Astro

What changes when the source is a managed Airflow: where the DAG files are, how to learn the Airflow version and the installed packages (the extractor runs the DAG files, so it needs both), how the REST API is reached, and what to export without copying a secret.

How to read this page: **cited** means a vendor page I read says it (link beside it, read in this session). **Not confirmed** means I did not find it in the pages I read: it is a pointer, check it. Nothing here was run against a real MWAA environment, Composer environment or Astro Deployment.

## What the extractor needs

`airflow_ir.py` imports every DAG file, as Airflow's parser does, so the environment it runs in needs:

1. **The same Airflow major and minor version** as the service (2.10 and 3.x differ in what a DAG file can import; an Airflow 2 environment cannot parse Airflow 3 `airflow.sdk` DAGs).
2. **The same provider packages and DAG-side dependencies** (anything the DAG files import at the top level, plus plugins). A DAG file that fails to import is reported in the IR's `import_errors`, not converted.
3. **Placeholders for Variables and Connections read at import time.** The managed service has them in its metadata database or secrets backend; the extractor does not. Export the *names* (below), set `AIRFLOW_VAR_<NAME>` and `AIRFLOW_CONN_<NAME>` to dummy values in the extractor's environment, and never put a real value there.
4. **Cosmos:** `astronomer-cosmos` and a dbt that can render the project (see [conversion.md](conversion.md), "Astronomer Cosmos").

Build the environment from the service's own package list and the Airflow constraints file for its Airflow and Python versions (`https://raw.githubusercontent.com/apache/airflow/constraints-<airflow version>/constraints-<python version>.txt`; this is the form MWAA documents and `pip` accepts anywhere). `scripts/tests/fixtures_cosmos/regenerate.sh` shows the pattern with Airflow 2.10.5.

## Amazon MWAA

| Need | How | Source |
|---|---|---|
| DAG files | They are objects in the environment's S3 bucket, which must be in the same Region as the environment, with versioning on and public access blocked. The bucket also holds `requirements.txt` and `plugins.zip`. The path of the DAG folder inside the bucket is part of the environment's configuration (the console's "DAG code in S3" pane links the bucket) | cited: [S3 bucket](https://docs.aws.amazon.com/mwaa/latest/userguide/mwaa-s3-bucket.html), [dependencies](https://docs.aws.amazon.com/mwaa/latest/userguide/working-dags-dependencies.html). The exact prefix and the `aws mwaa get-environment` field names (`DagS3Path`, `AirflowVersion`): **not confirmed** in the pages read |
| Airflow version | Chosen when the environment is created and kept until you upgrade it. Supported versions at the time of reading include 2.7.2, 2.8.1, 2.9.2, 2.10.1, 2.10.3, 2.11.0, 2.11.2 and 3.0.6, 3.2.1, 3.3.1, on Python 3.11 (up to 2.10.3) or 3.12 (2.11 and 3.x) | cited: [versions](https://docs.aws.amazon.com/mwaa/latest/userguide/airflow-versions.html). Where to read it for an existing environment (console details, `aws mwaa get-environment`): **not confirmed** |
| Packages | `requirements.txt` in the bucket, installed with `pip3 install -r` on the scheduler and every worker. From Airflow 2.7.2 the file must contain a `--constraint` line (MWAA adds one if missing). The base image's packages are listed in `aws-mwaa-docker-images`' `requirements.txt` on GitHub. The installed list shows in the scheduler's CloudWatch log stream `requirements_install_ip` | cited: [dependencies](https://docs.aws.amazon.com/mwaa/latest/userguide/working-dags-dependencies.html). Custom operators and hooks may also be in `plugins.zip` in the same bucket: extract it next to the DAGs for the extractor |
| REST API (Airflow 2.4.3 and later) | Two ways. `InvokeRestApi` (boto3 `mwaa.invoke_rest_api`, signed with AWS credentials; needs `airflow:InvokeRestApi`; 10 s timeout, 6 MB response limit). Or `create_web_login_token` (the token expires after 60 s) posted to `https://<host>/aws_mwaa/login`, which returns a session that lasts 12 h and is used against `https://<host>/api/v1`. Airflow 3: `/api/v2`, login at `/pluginsv2/aws_mwaa/login`, the token is in the `_token` cookie and is sent as `Authorization: Bearer` | cited: [REST API](https://docs.aws.amazon.com/mwaa/latest/userguide/access-mwaa-apache-airflow-rest-api.html) |
| CLI | `create_cli_token`, then `POST https://<host>/aws_mwaa/cli` with the command as the body; stdout and stderr come back base64-encoded. MWAA allows only a list of commands: `variables list`, `dags list` and `version` are there; **`connections list` and `connections export` are not** (only `connections add` and `delete`) | cited: [CLI reference](https://docs.aws.amazon.com/mwaa/latest/userguide/airflow-cli-command-reference.html) |
| Connections and Variables (names only) | Variables: the CLI's `variables list`, or REST `GET /variables`. Connections: REST `GET /connections` (the CLI cannot list them); keep only the connection ids. If the environment reads them from AWS Secrets Manager, the names are the secret names under the backend's prefix (**not confirmed**: depends on the configuration) | CLI limit cited as above; the REST paths are Airflow's own, see "Names only" |
| Known limits | `InvokeRestApi`: 10 s, 6 MB. Dependencies: aim for under 1 GB of libraries; an install that takes longer than ten minutes makes the environment roll back. Interactive web UI access is limited to the VPC for a private webserver, but `InvokeRestApi` works over the public internet with the right IAM permissions | cited |

Airlift on MWAA: `MwaaSessionAuthBackend` exists but uses the Airflow 2 login path; see [airlift.md](airlift.md).

## Google Cloud Composer (Composer 3)

| Need | How | Source |
|---|---|---|
| DAG files | A Cloud Storage bucket; the DAGs are `.py` files in its `/dags` folder. `gcloud composer environments describe ENV --location LOC --format="get(config.dagGcsPrefix)"` prints the folder's address. Copy it out with the Cloud Storage tools (the page documents `storage dags import` and `delete`, not a download) | cited: [manage DAGs](https://docs.cloud.google.com/composer/docs/composer-3/manage-dags); the copy command: **not confirmed** (`gcloud storage cp -r` on that prefix is the general way) |
| Airflow version | Part of the environment's image version. The field to read (`config.softwareConfig.imageVersion`): **not confirmed** in the pages read | |
| Packages | Custom packages: `gcloud composer environments describe ENV --location LOC --format="value(config.softwareConfig.pypiPackages)"`. Everything installed: `gcloud beta composer environments list-packages ENV --location LOC` (the result of `python -m pip list` on a worker; `--tree` adds `pipdeptree`). Composer does not resolve conflicts for you, and does not support system libraries. Preinstalled versions per Composer image version are on the image versions page (not read) | cited: [Python dependencies](https://docs.cloud.google.com/composer/docs/composer-3/install-python-dependencies) |
| REST API | Base URL: `config.airflowUri` (`https://<id>.composer.googleusercontent.com`, no trailing slash). Authenticate with Google credentials: `google.auth.default` with the `cloud-platform` scope in an `AuthorizedSession`. Airflow 3 environments use `/api/v2`, Airflow 2 uses `/api/v1`. A user created through the API gets the `Op` role by default; a `403` means the account lacks the Airflow role. Allow the caller's IP in the web server access control | cited: [Airflow REST API](https://docs.cloud.google.com/composer/docs/composer-3/access-airflow-api) |
| CLI | `gcloud composer environments run ENV --location LOC SUBCOMMAND -- ARGS`. Some commands are blocked; `kubectl` is not supported; each call consumes the `executeAirflowCommand` quota. The page shows `dags list` and `dags trigger`; whether `connections list` and `variables list` are allowed: **not confirmed** | cited: [Airflow CLI](https://docs.cloud.google.com/composer/docs/composer-3/access-airflow-cli) |
| Connections and Variables (names only) | REST (`/connections`, `/variables`), keeping only the ids and keys; or the CLI form if it is allowed. If a Secret Manager backend is configured, the names are the secret ids (**not confirmed**) | |
| Known limits | The API runs in the web server (heavy use may need more web server CPU and memory); CLI quota as above; a service account's email can be too long for Airflow versions before 2.3.0 (a documented workaround exists) | cited |

## Astronomer (Astro)

| Need | How | Source |
|---|---|---|
| DAG files | The source of truth is the Astro project in your repository. `astro deploy` builds everything except DAGs into an image, and deploys DAGs through a separate path (`astro deploy --dags` deploys only DAGs). The pages read describe no way to download deployed DAGs: use the repository | cited: [deploy code](https://www.astronomer.io/docs/astro/deploy-code) |
| Airflow version | Set by the Astro Runtime version of the Deployment (the Dockerfile's `FROM` line in the project). The Runtime-to-Airflow mapping is in Astro's Runtime documentation (not read) | **not confirmed** |
| Packages | The project's `requirements.txt` and `packages.txt`, baked into the image with the Dockerfile and plugins; a DAG-only deploy does not change them. To run the extractor with exactly the Deployment's packages, run it inside the project's image | cited in part: the deploy page says the image holds the Dockerfile, plugins and all Python and OS-level packages; the file names are **not confirmed** there |
| REST API | `https://<deployment url>/api/v2/...` for Airflow 3 (there is no `/api/v1`); the Deployment URL comes from `astro deployment inspect -n <name> --key metadata.airflow_api_url`, or the Airflow UI URL up to `/home`. Authenticate with a Deployment, Workspace or Organization API token as `Authorization: Bearer <token>`. The role of the token decides what it may do (a missing permission gives `403`). No rate limit is documented | cited: [Airflow API](https://www.astronomer.io/docs/astro/airflow-api) |
| Connections and Variables (names only) | REST `/connections` and `/variables` with the token, keeping only the ids and keys. The Astro CLI page for Airflow variables was not reachable (404): check `astro deployment --help` | CLI: **not confirmed** |
| Known limits | API updates ship with Airflow versions (a feature may need a Runtime upgrade); a DAG-only deploy skips dependency installation | cited |

## Names only: Connections and Variables

Never copy a value. A REST response for connections carries host, login and schema (the password is masked), and a variables response carries values: filter them out at the source.

```bash
# Airflow 2 (use /api/v2 on Airflow 3); BASE and AUTH come from the service's section above
curl -s -H "$AUTH" "$BASE/api/v1/connections?limit=1000" | jq -r '.connections[].connection_id' | sort > connection_names.txt
curl -s -H "$AUTH" "$BASE/api/v1/variables?limit=1000"   | jq -r '.variables[].key'               | sort > variable_names.txt
```

These are Airflow's own endpoints and field names, recalled rather than re-read for each version: check the response of your instance once. `scripts/connections_to_resources.py connection_names.txt` turns the connection list into a resources section and a `.env.example` (a names-only list has no types, so each becomes a placeholder resource until the types are known; an `airflow connections export` file gives the types and is read for nothing else). Use the two files to build the secrets map (Phase 5 of `SKILL.md`), to set the extractor's placeholders, and to cross-check the converter's report (`connections`, and the `AIRFLOW_VAR_*` variables the converted code reads).

## Differences that matter for the migration

- **The version decides the route.** On Airflow 3 (all three services offer it) the REST client of `dagster-airlift` cannot read the instance ([airlift.md](airlift.md)); the converter does not use the REST API for extraction, so it is unaffected.
- **Managed services pin dependencies.** The converted project does not inherit them: `converted_requirements.txt` lists what the generated code imports, and the Dagster deployment installs its own set.
- **Your scheduler settings do not travel.** Pools, `max_active_runs` and concurrency come out in `concurrency_limits.yaml`; the managed service's own worker counts, autoscaling and environment sizing are not DAG properties and are not converted.
- **Secrets backends.** A Secrets Manager or Secret Manager backend replaces Airflow's metadata database for Connections and Variables: map each name to the secret store Dagster+ or your deployment uses, never to a file.
