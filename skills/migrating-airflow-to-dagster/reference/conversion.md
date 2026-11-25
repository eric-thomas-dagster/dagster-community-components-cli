# Converting DAGs mechanically: runtime IR, then code generation

For the constructs that have a mechanical mapping, do not hand-convert DAG by DAG. Extract what Airflow actually built, then generate Dagster code and let the gates check it.

```
airflow_ir.py   (sandbox: runs the DAG files like Airflow's parser)  ->  ir.json
convert_ir.py   (deterministic)                                       ->  defs/<dag>.py, runtime.py, resources.py, sources.py, report.json
apply_conversion.py                                                    ->  manifest targets (converted) or deferrals (with the reason)
validate_defs.py / status.py                                           ->  the existing gates and completeness check
```

## Why start from the runtime view

Loops, factories, dag-factory YAML, Blueprints and dynamic DAG generation only exist once the code runs. Airflow's DagBag has already expanded them, so the IR holds every task with resolved arguments, dependencies, schedule, retries, pool, task groups and the callable's source. A loop-generated DAG is then as easy to convert as a plain one. The cost: `airflow_ir.py` executes the DAG files. Run it only on code you trust, in a sandbox, with the same Airflow version and providers as the project.

## What `convert_ir.py` converts

| Airflow | Dagster |
|---|---|
| `PythonOperator`, `@task` | asset running the same function; the function's source is copied together with exactly the imports, constants and helper functions it uses (nothing from Airflow) |
| `BashOperator`, `@task.bash` | asset running the command with `bash -c` |
| `EmptyOperator` | elided; its edges are rewired |
| values returned from one task and passed to another | asset parameters (the IO manager carries them) |
| `S3ListOperator`, `S3CopyObjectOperator`, `S3DeleteObjectsOperator` (explicit keys), `LocalFilesystemToS3Operator` | assets calling boto through the `aws_*` resource, mirroring what the hook calls |
| `CopyFromExternalStageToSnowflakeOperator`, `SnowflakeOperator`, `SQLExecuteQueryOperator` on a Snowflake connection | assets running the same SQL through the Snowflake resource; operator-level warehouse/database/role/schema become `USE` statements |
| `HttpOperator`, `HttpSensor`, SQL on other databases, `KubernetesPodOperator`, `EmailOperator`, `SlackWebhookOperator`, dbt Cloud, Airbyte, Fivetran and Databricks operators | steps that call `requests`, SQLAlchemy, `smtplib` or the Dagster integration library; see "Provider operators" below |
| one task with one outlet | the asset with that key (`s3://b/x` becomes `s3/b/x`); inlets are dependencies |
| tasks in a chain that name the same outlet | the last one produces the asset; the earlier ones are plain assets (note recorded) |
| schedule: none, `@once`, cron (trigger or data-interval timetable) | no schedule or `ScheduleDefinition` with the timezone |
| schedule: one asset, or any of several | `AutomationCondition.eager()` |
| schedule: all of several assets | per-asset `asset_matches(...newly_updated()).since_last_handled()` on the DAG's root tasks (verified: fires only after every upstream updated, not on a repeat update of one) |
| task groups, retries, retry delay, pool | key prefix, `RetryPolicy`, `pool` |
| Astronomer Cosmos `DbtDag`, `DbtTaskGroup` | one `dagster_dbt.DbtProjectComponent` (`defs.yaml`): an asset per dbt model, seed and snapshot, dbt tests as asset checks; see "Astronomer Cosmos" below |
| Cosmos per-task operators (`DbtRunLocalOperator`, `DbtBuildLocalOperator`, `DbtSeedLocalOperator`, `DbtSnapshotLocalOperator`, `DbtTestLocalOperator`, and the same operators in any execution mode) | one asset per operator that runs the same dbt command through `dagster_dbt.DbtCliResource` |

Connections become resources under the same key (`resources.py`); third-party imports the copied code needs are listed in `converted_requirements.txt`; assets a converted DAG reads but no converted DAG produces become `sources.py`.

## Translated, not stubbed

- **`PythonVirtualenvOperator`, `ExternalPythonOperator`, their decorators and the branch variants:** the callable (and the support code it needs) runs in its own interpreter through `af_isolated`: the given `python`, or a virtualenv built once from `requirements` (`uv` when present, else `venv` + `pip`) and cached; arguments and the result are pickled, as Airflow does. An external `python` path from the Airflow DAG is carried over and noted in the report.
- **`TriggerDagRunOperator` without `wait_for_completion`:** the run is created and executed in the background without waiting (the same split the registry's `trigger_job_asset` `wait: false` uses).
- **Task `sla`:** the step records an `sla_breach` observation on its asset when it finishes later than the SLA after the scheduled tick (`dagster/scheduled_execution_time`; the run start for a manual run). The task is not stopped, as in Airflow.
- **`.map(f)` that raises `AirflowSkipException`:** that item is left out, as Airflow skips that mapped instance.

## Provider operators (HTTP, SQL, Kubernetes, email, Slack, dbt Cloud, Airbyte, Fivetran, Databricks)

These used to become stubs. They are now translated by `scripts/provider_ops.py` into a step that calls an official Dagster integration library or plain Python. Order of preference per operator: a registry component or integration component expressed as YAML (the `--output components` forms below), then an integration library call, then inline Python, then a loud stub. A mapped (`.expand`) provider operator, and an operator argument the mapping cannot carry, make a loud stub with the reason named; they are never ignored. The argument checks use the operator's real constructor arguments (the IR also records which arguments hold a function, such as `response_check`, so those are refused rather than dropped; `airflow_ir.py` now writes them under `callable_args`).

| Airflow operator | Becomes | Environment the step reads | Refused (stub) | Stated difference (degraded caveat) |
|---|---|---|---|---|
| `HttpOperator`, `SimpleHttpOperator` | `requests`, following `HttpHook.run`: GET sends `data` as query parameters, other methods as the body, the response text is the value, an HTTP error status fails the step unless `check_response` is false | `<CONN>_BASE_URL`, optional `<CONN>_AUTH_HEADER`, `<CONN>_LOGIN` and `<CONN>_PASSWORD`, `<CONN>_HEADERS` | `response_check`, `response_filter`, `pagination_function`, `auth_type`, `request_kwargs`, `retry_args`, `extra_options` other than `timeout`, `verify`, `check_response`, `allow_redirects` | `deferrable=True` is ignored; `tcp_keep_alive*` are not applied |
| `HttpSensor` | A polling step (the same loop as the other sensors): a request, true once it succeeds; `404` or any `response_error_codes_allowlist` code means not yet, any other error fails | as above | `response_check` and the options above | `deferrable=True` is ignored |
| `SQLExecuteQueryOperator` on a connection that is not Snowflake, `PostgresOperator`, `MySqlOperator`, `MsSqlOperator`, `SqliteOperator` | SQLAlchemy `exec_driver_sql` over one connection: every statement gets `parameters`, one transaction committed at the end (or each statement committed with `autocommit=True`), the rows of the last statement that returned any are the value (as lists, which is what Airflow's XCom holds) | `<CONN>_URL`, a SQLAlchemy URL (the driver package is yours to install) | `handler` other than the default, `output_processor`, `database`, `hook_params`, `return_last=False` with several statements, `parameters` that are not a dict or list | none beyond the connection (the URL and driver replace the connection type) |
| `KubernetesPodOperator` | `dagster-k8s` `execute_k8s_job`: the image as a Kubernetes Job, logs streamed into the run log, a failed Job fails the step | none (kubeconfig, or in-cluster config with `in_cluster=True`) | `secrets`, `ports`, `init_containers`, `full_pod_spec`, `pod_template_file`/`pod_template_dict`, `skip_on_exit_code`, `do_xcom_push=True`, pod and container security contexts, host and DNS settings, env vars from `value_from`, unreadable Kubernetes objects | UNVERIFIED against a live cluster (below); a succeeded Job is not deleted; `on_finish_action`, `get_logs=False`, `kubernetes_conn_id`, `startup_timeout_seconds`, `active_deadline_seconds` are not applied as Airflow applies them |
| `EmailOperator` | `smtplib`, following `airflow.utils.email.send_email_smtp`: one HTML part, files attached, Bcc kept out of the headers | `<CONN>_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_STARTTLS`, `_SSL`, `_FROM` | `html_content`, `files` or `custom_headers` that are not plain values | none beyond the connection |
| `SlackWebhookOperator` | `requests.post` of the JSON payload (text, channel, username, icons, blocks, attachments) to the webhook URL; any status but 200 fails | `<CONN>_WEBHOOK_URL` | `proxy` | none |
| `DbtCloudRunJobOperator` | `dagster-dbt` `DbtCloudClientResource`, created with `disable_schedule_on_trigger=False` (Dagster's default would switch off the job's schedule, which the operator never does); `run_job` with `cause`, `steps_override`, `schema_override` and `additional_run_config`, then `poll_run` unless `wait_for_termination` is false; the run id is the value | `<CONN>_API_TOKEN`, `<CONN>_ACCOUNT_ID` (resource in `resources.py`) | `reuse_existing_run`, `retry_from_failure` | UNVERIFIED against a live account; `account_id` comes from the environment, not the argument |
| `AirbyteTriggerSyncOperator` | `dagster-airbyte` `AirbyteResource.sync_and_poll` (or `start_sync` when `asynchronous`); the job id is the value | `<CONN>_HOST`, `_PORT`, `_USER`, `_PASSWORD` | none | UNVERIFIED against a live server |
| `FivetranOperator` (`airflow-provider-fivetran`) | `dagster-fivetran` `FivetranResource.sync_and_poll` (the resource sets the connector to manual scheduling, as the operator does) | `<CONN>_API_KEY`, `<CONN>_API_SECRET` | `schedule_type` other than `manual` | UNVERIFIED against a live account |
| `DatabricksRunNowOperator`, `DatabricksSubmitRunOperator` | `dagster-databricks` client: the operator's JSON body POSTed to `/api/2.1/jobs/run-now` or `/runs/submit`, then `wait_for_run_to_complete` unless `wait_for_termination` is false; the run id is the value | `<CONN>_HOST`, `<CONN>_TOKEN` (resource in `resources.py`) | `job_name` without a `job_id`, `repair_run`, `cancel_previous_runs` | UNVERIFIED against a live workspace |

`<CONN>` is the Airflow connection id upper-cased with non-alphanumeric characters as `_` (`partner_api` becomes `PARTNER_API`). No connection value is ever copied; `report.json` lists each connection with the variable names it needs under `connections`. The packages each mapping needs (`requests`, `sqlalchemy`, `dagster-k8s`, `dagster-dbt`, `dagster-airbyte`, `dagster-fivetran`, `dagster-databricks`) go into `converted_requirements.txt`.

`PipesK8sClient` is not used for `KubernetesPodOperator`: the registry's `k8s_job_asset` README says a container that does not speak the Pipes protocol hangs under it, and a pod operator's image usually does not. `execute_k8s_job` runs any image.

### How each mapping was checked

| Mapping | Checked by running | Not checked |
|---|---|---|
| HTTP operator and sensor | converted DAG run against a local HTTP server: URL join, query and body, headers, error statuses, polling through two 404s | TLS, proxies, authentication beyond the header and basic auth the environment supplies |
| SQL | converted DAG run against SQLite through SQLAlchemy: parameters, one transaction rolled back on error, autocommit, rows passed to a downstream task | any other database or driver, parameter styles other than the driver's own |
| Email | converted DAG run against a local SMTP server: recipients, rendered subject, HTML part, attachment, Bcc | STARTTLS, SMTP login |
| Slack webhook | converted DAG run against a local HTTP server: JSON payload, error status | the real Slack API |
| Kubernetes pod | the real `execute_k8s_job` ran and built the real `V1Job` from the converted arguments (image, command, args, env, resources, volumes, service account, node selector, labels, env-from); the calls to the cluster were replaced | any cluster: pod scheduling, log streaming, the wait, cleanup |
| dbt Cloud, Airbyte, Fivetran, Databricks | converted DAG run with a recording fake in place of the client; every call checked against the real library's method signature | the vendor services; the response shapes the code reads (`id` of a dbt Cloud run, `job_details["job"]["id"]` of Airbyte, `run_id` of Databricks) come from the libraries' source and the vendors' API docs |

Each mapping that was not run against its real service carries a `degraded` caveat that says so (`UNVERIFIED against a live ...`), so it shows in the report, and with `--caveats off` it defers the DAG.

## Astronomer Cosmos (`DbtDag`, `DbtTaskGroup`, the dbt operators)

Cosmos renders a dbt project as Airflow tasks, so the IR holds one task per model (`<model>.run`, `<model>.test`) and per seed. `airflow_ir.py` records the Cosmos settings of each task (`cosmos` block) and, per DAG, each `DbtDag` / `DbtTaskGroup` with its `ProjectConfig`, `RenderConfig` and `ExecutionConfig` values and the dbt nodes it rendered (`cosmos_groups`). The extraction environment needs `astronomer-cosmos` and whatever Cosmos needs to render the project: dbt itself when `load_method` is `dbt_ls` or `automatic` (the default `InvocationMode.DBT_RUNNER` needs dbt-core in the same environment as Airflow; `SUBPROCESS` needs a dbt executable), or a `manifest.json` for `load_method=dbt_manifest`. `cosmos_ops.py` then does the mapping; `convert_ir.py` wires the result into the DAG.

| Cosmos | Becomes | Stated difference |
|---|---|---|
| `DbtDag`, `DbtTaskGroup` (any execution mode: local, virtualenv, docker, kubernetes, aws_eks, aws_ecs, azure_container_instance, gcp_cloud_run_job, watcher, airflow_async) | **one** `dagster_dbt.DbtProjectComponent` in `defs/<dag>_dbt[_<group>]/defs.yaml`. Every model, seed and snapshot is an asset with its dbt lineage; every dbt test is an asset check. Model-level lineage is better than Cosmos's (Cosmos only had tasks) | the component runs one `dbt build` for the whole selection instead of one Airflow task per model; the graph of a run in Dagster is one step per run, with per-model results in the run and asset views |
| `RenderConfig.select`, `exclude`, `selector` | the component's `select`, `exclude`, `selector` (lists are space-joined, which dbt reads as a union) | passed to dbt as written; Cosmos's own task selection and dbt's should agree, but check that `dg list defs` shows the same node count as the extracted DAG (the report lists the keys) |
| `test_behavior` `after_each` (the default), `build` | `cli_args: [build]` (each model's tests run right after it, the same ordering) | none |
| `test_behavior=after_all` | `cli_args: [build]` | **degraded caveat**: tests now run right after each model, so a failing test stops the models downstream of it, which `after_all` did not |
| `test_behavior=none` | `build` with `resource_type:test` added to `exclude` | none |
| `ProjectConfig.dbt_vars`, operator `vars` | `--vars {...}` in `cli_args` | |
| operator `full_refresh`, `warn_error`, `quiet`, `fail_fast`, `no_version_check`, `dbt_cmd_flags`, `dbt_cmd_global_flags`, `indirect_selection` | the same flags in `cli_args` (global flags are passed after the command, which dbt accepts) | |
| `ProjectConfig.dbt_project_path` | `project.project_dir`, relative to the component: `../../<--dbt-dir>/<project directory name>` | the converter does **not** copy the dbt project: the report's `dbt.projects` says where it came from and where it must go |
| `ProfileConfig(profiles_yml_filepath=...)` inside the dbt project | used as it is (dbt finds `profiles.yml` in the project directory) | the file is **not read**: check it for inline credentials |
| `ProfileConfig(profiles_yml_filepath=...)` outside the project | not copied (it may hold credentials); the component points at `dbt_profiles/<profile>/` and a degraded caveat tells you to put a `profiles.yml` there that reads secrets with `env_var()` | |
| `ProfileConfig(profile_mapping=...)` | a generated `dbt_profiles/<profile>/profiles.yml` whose every connection field is `{{ env_var('<CONN>_<FIELD>') }}`; non-secret `profile_args` (schema, threads) are written, secret-looking ones also become environment variables. Names only in `report.json` under `dbt.projects.<name>.environment`; no connection value is read | the fields come from the mapping's class (`airflow_param_mapping`), not from Airflow's connection-parsing code: check them against the dbt adapter's documentation |
| `ProjectConfig.env_vars`, operator `env` | the variable **names** in the report; values are never copied. Set them in the Dagster deployment | |
| operator `retries` (`default_args` or `operator_args`) | the job carries the tag `dagster/max_retries=<N>` (a real Dagster tag): a failed run is re-launched up to N times from the failed step. The dbt component has no retry-policy field (its `op` takes `name`, `tags`, `description`, `pool`, `backfill_policy`), so run retries are the wiring; they must be enabled on the deployment (`run_retries.enabled` in dagster.yaml; off by default in open source). A note, not a caveat | **behaviour change, an improvement**: Cosmos retried one task per model; here one `dbt build` is the unit, and `dbt retry` (a dbt command, not wired by the converter) re-runs only the models that failed |
| operator `pool` | `op.pool` of the component when all tasks share one pool (also listed in `concurrency_limits.yaml`) | several pools: none applied (caveat) |
| operator `callback`, task callbacks | not converted (degraded caveat) | |
| `emit_datasets` | not converted (note): the dbt assets have their own keys; a DAG scheduled on the Cosmos datasets needs an automation condition on them | |
| `seed_rendering_behavior`, `source_rendering_behavior` other than the defaults | degraded caveat (seeds always built; source freshness not run) | |
| tasks before or after the group | stay as ordinary assets. A task **after** the group depends on the group's sink dbt assets; a task **before** it becomes an extra dependency of the group's root dbt assets (written in `post_processing`: Dagster replaces an asset's `deps` there, so each root lists its dbt parents again) | the dependency is on assets, not tasks |
| two Cosmos groups (or two DAGs) over the same dbt nodes | the second is keyed under `by_<dag>[_<group>]/` (one producer per asset), with a caveat | |
| Cosmos per-task operator on its own (`DbtRunLocalOperator`, ...) | one asset `<dag>/<task>` running `dbt <command> --select ...` through `DbtCliResource` | **degraded caveat**: the operator holds only a `select` string, never the nodes it ran, so there is no model-level lineage; use `DbtDag` / `DbtTaskGroup` (or build a dbt component by hand) for that |
| `DbtTestLocalOperator` on its own | the same step running `dbt test` | its results are not asset checks of the models |
| Execution mode: docker, kubernetes, aws_eks, aws_ecs, azure_container_instance, gcp_cloud_run_job, virtualenv | **converted exactly as local mode**: the same component or the same `DbtCliResource` asset. Cosmos offers these modes only because dbt's dependencies clash with Airflow's; in Dagster dbt runs in the code location's own environment, so the clash does not exist. One informational note (not a caveat; the unit is not marked for review): "was <mode> in Cosmos; in Dagster dbt runs in the code location's environment, so pin dbt-core and the adapter in the project's dependencies; image-only dependencies (anything baked into the container) must be added to the environment". Pod and container settings (`image`, `namespace`, `secrets`, `resources`, `volumes`, `node_selector`, ...) are dropped from the output and their **names** listed in the note (values are never read into the IR); `py_requirements` of the virtualenv mode go into `converted_requirements.txt` | the environment dbt runs in |
| Watcher and airflow_async modes | treated the same way: they are execution-engine variants of the same dbt run (a producer runs dbt once and consumers watch it; the compiled SQL runs through Airflow's async operators), so the group becomes the same one `dbt build`. Decision, **documentation only**: not run, these classes were not in the extraction environment | the Airflow-side engine disappears |
| a container whose DAG overrides the entrypoint or command with something that is not dbt (`entrypoint`, `cmds` whose first word is not `dbt`) | **loud stub** naming the command: it cannot be expressed as a dbt command | |
| the dbt project | **used unchanged**; the report (`dbt.projects.<name>`) says `used_unchanged: true` and lists in `converter_changes` only what the converter actually did: the generated `profiles.yml` (env_var based) and the key prefix when two groups share nodes | |
| `RenderConfig.node_converters`, `DbtRunOperationLocalOperator`, docs, `ls`, `deps`, `source`, `compile`, `clone` operators | loud stub (or a deferral with `--caveats off`) | no dbt component form |

The group becomes assets only when Cosmos's own rendering is faithful to dbt's graph. A DAG that combines a Cosmos group with dynamic mapping, branching, partitions or an asset schedule is deferred (not converted yet).

**How it was checked.** `tests/fixtures_cosmos/ir.json` is a real IR: `airflow_ir.py` run on `tests/fixtures_cosmos/dags/cosmos_shop.py` with Airflow 2.10.5 and astronomer-cosmos 1.15.1 (a `DbtDag`, a `DbtTaskGroup` between a Python task and a Bash task, and four per-task operators over a small dbt project). The tests convert it, load the result with `dagster.components.load_defs` (the dbt component refreshes its manifest with a real `dbt parse`), compare every asset's dependencies, and execute the jobs: `dbt seed`, `run`, `build --vars` and `test` run through the generated steps, and `dbt build` through the component, against DuckDB, with the three dbt tests of the `DbtDag` passing as asset checks (six checks are defined across the two groups). **Container modes** were checked on a synthetic edit of that IR (every Cosmos task set to kubernetes, docker and the other modes, with pod settings named): the conversion and the component load were run, no container was. **Documentation only, not run:** other warehouses and dbt adapters, `profile_mapping` classes (the generated `profiles.yml` was checked for structure and for the absence of values, not against Snowflake, BigQuery or any real connection), `dbt deps` / packages, `load_method=dbt_manifest` and `custom` extraction, Cosmos 1.x versions other than 1.15.1, Airflow 3 with Cosmos, the watcher and airflow_async modes, a real Cosmos container operator, Cosmos source rendering, and a real Airflow schedule.

## Choosing granularity: task or product

`convert_ir.py --granularity task` (the default) writes one asset per Airflow task: the faithful form, with every task's outcome and value comparable to Airflow's. `--granularity product` aims at one asset per **data product**, with the steps that only help to produce it inside that asset. The decision is deterministic, made from the IR (outlets, inlets, operator type, retries and the other settings, downstream consumers), and written per task, with the reason, to `report.json` under `granularity.decisions`.

| Role | Rule (first match wins) | Becomes |
|---|---|---|
| glue | `EmptyOperator` and the other no-effect types | not an asset: elided, its edges are rewired |
| product | the task has outlets or inlets (its output is persisted under an asset key) | its own asset |
| product | an external or special step: S3, Snowflake, provider operators, sensors, triggers, branches, mapped tasks, a stub | its own asset |
| product | no downstream task consumes it (a leaf), or two or more do (a fan-out) | its own asset |
| product | it has its own retries, timeout, pool, callback, SLA or trigger rule, or uses the Airflow context, XCom pulls by task id, async code or skip logic | its own asset: **its own restart point** |
| step | a Python or Bash task whose value feeds exactly one downstream Python or Bash task and none of the above applies | **plain Python inside that consumer's asset** (a chain folds into its final consumer) |

Not applied (the DAG converts at task granularity, and the report says why): graph-form DAGs (branching, mapping, setup/teardown), partitioned DAGs, asset-triggered DAGs, runtime-partitioned DAGs and Cosmos DAGs.

**Restart behavior.** A plain-Python step restarts **from the beginning of the asset**: if the consumer fails, or retries, the folded steps run again with it; they have no run record or retry of their own, and Airflow's per-task duration and retry history have no equivalent. A step with `retries`, a timeout or a pool is never folded, so it keeps its own retry policy and restart point. A failed op in a graph-form asset (the form used for branching and mapping) retries and restarts as an op, as before. The third form the design allows, ops inside a per-product `@graph_multi_asset` for steps that each deserve a restart point, is **not generated**: `convert_ir.py` keeps such a step as its own asset instead.

**Not implemented, and why.** *Dataframe or SQL chains as one registry pipeline component* (`polars_pipeline`, `pandas_pipeline`, `warehouse_pipeline`, `pyspark_pipeline`): these components take a declarative list of operations (`filter`, `with_columns`, `group_by`, `join`, `sql`, ...), while the IR holds arbitrary Python callables and SQL strings, which cannot be turned into those operations deterministically; this is a job for the rewrite route (`dag_spec.py` and `rewrite_prompt.py`), where a person or model chooses the component, reads its `schema.json` and writes the YAML. *Notification, wait and cleanup tasks as sensors, hooks or run-status sensors*: only `EmptyOperator` is turned into glue here; email, Slack and sensor tasks stay assets and are listed with their role in the report.

Asset keys change under product granularity (a folded step has none). `report.json` maps every Airflow task to a key anyway (`task_keys`: a folded step maps to its consumer's asset, and `folded` lists them); `apply_conversion.py` writes a folded step's unit target as `{assets: [consumer key], folded_into: ...}` and expands the consumer's dependencies through it; the parity comparison checks a folded step's outcome only (it has no value of its own). Generated tests and checks follow the product form.

**Default.** Task granularity stays the default; product is opt-in. **Measured:** on the 11 quick DAGs of the release-matrix suite (Airflow 2.10.5 against Dagster, the repo's parity harness) both granularities gave 10 identical outcomes and the same 1 difference (a timestamp); 3 of the 11 had a step folded. The corpus was not re-run and only 3 folds were exercised, so product is not the default. A before/after pair is in `examples/granularity/`.

## Generated tests and structural asset checks

For every converted DAG that becomes a Python module, `convert_ir.py` writes `tests/test_<dag>.py` (in the Dagster project root; `generated_tests` in the report) and may add asset checks to the module (`asset_checks`; totals in `report.json` under `generated`).

| Test | What it asserts |
|---|---|
| load | the module imports and its definitions load (`Definitions.validate_loadable`) |
| shape | every asset key, its dependencies, pool and retry policy equal what the IR said (the expectations are written into the test, so a hand edit that drifts from the Airflow DAG fails) |
| job and schedule | the job name; the schedule's cron and timezone |
| checks | the module has exactly the generated structural checks |
| materialize | for a DAG with no external system: the whole DAG runs (`execute_in_process` on the generated assets), every asset materializes and every structural check passes. A DAG with an external system (cloud storage, a warehouse, HTTP, SQL, a sensor, a container, a triggered DAG, dbt), a loud stub, partitions or assets read from outside is a **visible skip with the reason**, never a fake pass. A DAG with shell tasks runs them only with `CONVERTED_TESTS_RUN_SHELL=1` |

Returned values are compared with Airflow's by the parity harness (it needs Airflow); the generated tests check outcomes and the structural checks.

Asset checks come only from what the source shows, are `blocking=False`, and say in their description and metadata (`kind: structural`) that they are **structural, not business, checks** (API: `@dg.asset_check(asset=..., blocking=False)` returning `dg.AssetCheckResult`; the checked asset's value is loaded through the IO manager by a parameter):

| Check | Generated for | Severity |
|---|---|---|
| `returned_value_present` | a task whose return value is consumed by a downstream task | ERROR if the value is `None`; WARN if it is an empty list, dict, string or DataFrame |
| `s3_object_exists` | `S3CreateObjectOperator`, `LocalFilesystemToS3Operator`, `S3CopyObjectOperator` with a literal (untemplated) destination bucket and key | ERROR if `head_object` fails |

Not generated, on purpose: a check that a SQL asset "ran without error" (a failure already fails the step, so the check would be vacuous), and any business check. dbt tests already become asset checks of the dbt models (see "Astronomer Cosmos").

## Connections export: `connections_to_resources.py`

`airflow connections export --format json` (or the names-only list from [hosted-airflow.md](hosted-airflow.md)) goes in; a resources section and a `.env.example` come out, **with variable names only**:

```bash
python3 scripts/connections_to_resources.py connections.json --out-dir connections_out    # resources_connections.py, .env.example, connections_summary.{md,json}
```

**No value is ever written, printed or logged.** From each record the script keeps two strings, the connection id and `conn_type`, and drops the rest (passwords, tokens, `extra` JSON with private keys and secret keys, hosts, logins, URIs) before anything is produced; an unreadable file gives an error that names no content. A test feeds a fixture full of fake secrets (every string value, and every piece of every `extra`) and asserts that none appears in the output, the summary or the printed text. Delete the export afterwards: it holds secrets.

| Airflow `conn_type` | Becomes | Variables |
|---|---|---|
| `aws`, `s3` | `dagster_aws.s3.S3Resource()` (boto3's default credential chain) | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_DEFAULT_REGION` (set what your deployment needs) |
| `snowflake` | `dagster_snowflake.SnowflakeResource(account, user, password)` | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (the converter's own names) |
| `gcpbigquery`, `bigquery` | `dagster_gcp.BigQueryResource(project=...)` | `<CONN>_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS` |
| `google_cloud_platform` | `dagster_gcp.GCSResource(project=...)` | the same |
| `slack` | `dagster_slack.SlackResource(token=...)` | `<CONN>_TOKEN` |
| `ssh`, `sftp` | `dagster_ssh.SSHResource(remote_host, remote_port, username, password)` | `<CONN>_HOST`, `_PORT`, `_USER`, `_PASSWORD` (use `key_file` for key authentication) |
| `databricks`, `dbt_cloud`, `airbyte`, `fivetran` | the resource the converter already writes for the operator | `<CONN>_HOST` and `_TOKEN`; `_API_TOKEN` and `_ACCOUNT_ID`; `_HOST`, `_PORT`, `_USER`, `_PASSWORD`; `_API_KEY` and `_API_SECRET` |
| `postgres`, `mysql`, `mssql`, `sqlite`, `redshift` | no resource object: the converted SQL step reads one SQLAlchemy URL | `<CONN>_URL` |
| `http` | read in the step | `<CONN>_BASE_URL`, optional `_AUTH_HEADER`, `_LOGIN`, `_PASSWORD`, `_HEADERS` |
| `slackwebhook` | read in the step | `<CONN>_WEBHOOK_URL` |
| `smtp`, `email` | read in the step | `<CONN>_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_STARTTLS`, `_SSL`, `_FROM` |
| `kubernetes` | none: `dagster-k8s` uses the kubeconfig or in-cluster config | `KUBECONFIG` |
| anything else (`wasb`/Azure, `mongo`, a names-only list) | a documented `UnmappedConnection` placeholder resource with a TODO, listed in the summary | `<CONN>_HOST`, `_PORT`, `_LOGIN`, `_PASSWORD`, `_SCHEMA`, `_EXTRA` |

The table is `scripts/connections.py`, shared with `convert_ir.py`: the converter writes its `resources.py` entries (S3, Snowflake, dbt Cloud, Airbyte, Fivetran, Databricks) from the same table, so both files use the same keys, constructors and variable names (a test compares them, and `report.json` `connections` lists the names for S3 and Snowflake as it already did for the other kinds). The classes and constructor fields were checked by importing the libraries; the registry components that fit are named in the summary (`dagster-community-components` 0.10.188 manifest: `postgres_resource`, `mysql_resource`, `mssql_resource`, `redshift_resource`, `bigquery_resource`, `snowflake_resource`, `aws_s3_resource`, `http_resource`, `slack_resource`, `sftp_resource`, `databricks_resource`, `dbt_cloud_resource`, `airbyte_resource`, `fivetran_resource`, `smtp_resource`; there is none for `ssh`, `kubernetes`, a Google Cloud connection or sqlite, and `azure_openai_resource` is not an Azure storage resource). Not run against any real connection or service.

## More provider operators (Slack API, S3, GCS, SSH, Docker, Lambda, BigQuery, Spark)

Added after measuring which operators were still stubbed (below). Each is translated by `scripts/provider_ops_more.py` into a step on an official Dagster integration or a plain SDK, with the operator's arguments carried over; **an argument the mapping cannot carry is a loud, specific stub** (`<Operator> <argument> is set (not carried over)`), never ignored. An argument left at its Airflow default is not a refusal (the IR records every constructor argument, defaults included). Every mapping below was run against a recording fake, a validator or a stand-in, **not against the real service**, and carries a degraded `UNVERIFIED` caveat that says which.

| Airflow operator | Becomes | Refused (stub) | Checked by running | Stated difference |
|---|---|---|---|---|
| `SlackAPIPostOperator` | `dagster-slack` `SlackResource.get_client().chat_postMessage` | `api_params`, `method`, `proxy`, `base_url`, `timeout`, `retry_handlers` | a fake client; the keyword names were compared with `slack_sdk.WebClient.chat_postMessage`'s signature | UNVERIFIED against a workspace |
| `S3KeySensor` | polling step on the aws resource (`head_object`; wildcard through `list_objects_v2`; a list of keys must all exist) | `check_fn`, `metadata_keys`, `use_regex` | a fake S3 client | `deferrable` ignored |
| `S3ListPrefixesOperator`, `S3CreateBucketOperator`, `S3DeleteBucketOperator` | boto3 calls on the aws resource (list common prefixes; create unless it exists, with the location constraint outside us-east-1; delete, emptying first with `force_delete`) | none | a fake S3 client | |
| `LocalFilesystemToGCSOperator`, `GCSToLocalFilesystemOperator`, `GCSListObjectsOperator`, `GCSDeleteObjectsOperator`, `GCSObjectExistenceSensor`, `GCSCreateBucketOperator`, `GCSDeleteBucketOperator` | `google-cloud-storage` client through `dagster-gcp` `GCSResource` | `impersonation_chain`, `user_project`, `chunk_size`, `encoding`, `retry`, `metadata`, `deferrable`, `gzip`; create-bucket `resource`, `labels`, `storage_class`, ...; delete with both or neither of `objects` and `prefix` | a fake GCS client | |
| `SSHOperator` | `dagster-ssh` `SSHResource.get_connection()`, `exec_command`; a non-zero exit fails | `environment`, `remote_host`, `conn_timeout`, a non-default `banner_timeout`, a command that is not a string | a fake connection | the value is stdout as text (Airflow pushes it base64-encoded); `get_pty` not applied |
| `DockerOperator` | the `docker` SDK (`containers.run`, wait, logs, remove); a non-zero exit fails; the value is the last log line | `mounts`, TLS settings, `device_requests`, `cap_add`, `ports`, `mem_limit`, `cpus` other than the default, `container_name`, `hostname`, ... (full list in `provider_ops_more.py`) | a fake `docker` module | `auto_remove` is remove-after-run for `success` and `force` |
| `LambdaInvokeFunctionOperator` | boto3 `lambda.invoke`; a function error fails; the value is the parsed response payload | `botocore_config`, `verify`, `deferrable`, `client_context` | **botocore's Stubber validated the request against the real service model**, and the DAG ran against the stubbed client | credentials and region come from boto3's default chain (the connection is not read) |
| `BigQueryInsertJobOperator` (query jobs only) | `dagster-gcp` `BigQueryResource.get_client()`, `client.query(sql, job_config=QueryJobConfig(...))`, `result()`; the value is the job id | any configuration that is not a plain query job (load, copy, extract), query settings beyond `useLegacySql` and `priority`, `deferrable` | a fake client; the job config is a real `QueryJobConfig` | |
| `SparkSubmitOperator` | `spark-submit` built from the operator's arguments (`--master`, `--conf`, the resource flags, `--jars`, ..., application and its arguments) and run as a subprocess | `keytab`, `principal`, `proxy_user`, `use_krb5ccache`, `properties_file` | **a stand-in `spark-submit` executable that records its arguments was run** | the master and deploy mode come from `<CONN>_MASTER` and `<CONN>_DEPLOY_MODE`, not the connection; the driver status is not polled |

**Registry components.** `--output components` writes a registry component, instead of Python, for four of these when the arguments fit (the component's fields are strict: a wrong field name is rejected, and a test validates every generated document with the registry's own class): `LambdaInvokeFunctionOperator` to `lambda_invoke_asset` (needs `region_name`; the payload must be a JSON object), `SSHOperator` to `ssh_asset` (host, user and password read from `<CONN>_HOST`, `_USER`, `_PASSWORD`), `DockerOperator` to `docker_container_asset` (image, command, env vars, network only), `BigQueryInsertJobOperator` to `bigquery_query_asset`. Their validation tiers in the `dagster-community-components` 0.10.188 manifest: `docker_container_asset` and `bigquery_query_asset` are `live`; `lambda_invoke_asset` and `ssh_asset` are `code` (code-reviewed, not run against a live system), and the report carries a degraded caveat saying so. The other operators have no registry component that can carry their arguments: Slack API post and the GCS and S3 calls are not components that return a value or a list; `msteams_message_send` needs an upstream DataFrame; `spark_livy_batch_asset` and `spark_k8s_operator_asset` are Livy and Kubernetes, not `spark-submit`.

**Measured.** On the 334 `example_*.py` DAG files of Apache Airflow's provider system tests (`providers/{google,amazon,ssh,sftp,docker,slack,apache/spark,snowflake,postgres,http,microsoft/azure,mysql,smtp}/tests/system`, `main` at the time of writing), counting each operator once per file that uses it: 1,797 operator uses of 956 distinct operator types. Operators the converter maps: **before 80 uses (4.5 %), 14 types; now 410 uses (22.8 %), 31 types.** Most of the gain is S3 and GCS bucket create and delete (275 uses), which the system tests use as scaffolding; these files are provider test DAGs, not typical pipelines, so read the numbers as coverage of the operator surface, not of an estate. On the 146 DAG files of eight Astronomer example repositories (mostly decorated tasks and custom operators) 137 of 173 operator uses were mapped before and 140 now. Still unmapped at the top of the system-test list: `BigQueryCreateEmptyDataset`/`DeleteDataset`/`CreateTable`, `Dataproc*` (cluster and job), `GCSToGCS`, `GCSSynchronizeBuckets`, Compute Engine, GKE, EKS, RDS, `RedshiftData`, `Athena`, `Beam`/`Dataflow`, `BigQueryValueCheck`, `GCSToBigQuery`, Pub/Sub, Cloud SQL. The ranking and the registry check are in [component-wishlist.md](component-wishlist.md). The corpus clones the earlier measurement used were not available, so the 292-DAG numbers in "Measured results" below are the earlier ones and were not re-run.

## Settings that map or degrade

- `retry_exponential_backoff` becomes `Backoff.EXPONENTIAL`; a named XCom of a `multiple_outputs` task reads that key of the returned dict.
- Not enforced here, so a `degraded` caveat (the DAG converts and the report says what is missing): `depends_on_past`, `wait_for_downstream`, `max_active_tis_per_dagrun`, `pool_slots`, `execution_timeout` on a Python task, `fail_stop`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`, task callbacks that cannot be attached, and `reset_dag_run` / `logical_date` on a trigger.
- Two DAGs that produce the same asset: the second DAG's output keeps its own asset under `<key>/by_<dag>`, with a caveat.
- Asset schedules with nested AND/OR become the same combination of automation conditions. A graph-form DAG (mapping, branching, setup/teardown) triggered by assets runs on an automation condition on those assets; a graph asset cannot declare dependencies, so that lineage edge is not drawn.

## Also translated

- **Mapped provider operators** (S3 copy, delete, list, upload; Snowflake COPY INTO and SQL) in graph DAGs: the step merges the operator's fixed arguments with each expanded item at run time.
- **Async callables** run with `asyncio.run`; `templates_dict` is rendered and passed when the callable takes it; `task_instance_key_str` and `literal()` strings work.
- **Odd schedules**: `ContinuousTimetable` becomes a schedule every minute and `AfterWorkdayTimetable` a weekday cron, each with a degraded caveat. `catchup=True` converts to a schedule from now on, with a caveat, unless `--caveats off`.
- **Unresolved asset aliases** convert without a trigger, with a caveat; unordered writers of one asset each keep their own asset.

## Task-group loops (Airflow 3.4 `group.loop(max_iterations=, until=)`)

A loop group (its Python tasks plus the gate) becomes one task. Its callable runs the group's tasks in order once per iteration, handing each task the `loop` object (`loop.index` from 1, `loop.previous` the preceding iteration's result, `loop.result` for the `until` callable), and consults `until` after each iteration: a `LoopDecision`, a bool or a `(stop, reason)` pair. With no `until` the body runs exactly `max_iterations` times; with one, running out of iterations before it says stop fails the step, as in Airflow. Tasks after the loop depend on the one asset. A loop whose body has a mapped task, a non-Python task, or a task fed from outside the group stays a stub. Checked on Airflow's own examples: Newton's square root converges in the same iterations, a fixed 3-iteration loop returns 3, and Airflow and Dagster both succeed.

## Runtime-partitioned assets (Airflow 3.3 `PartitionedAtRuntime`)

A producer that records its partition keys while it runs converts to its own asset (`<dag>/<task>`) plus a dynamically partitioned catalog asset at the outlet's key: `outlet_events[asset].add_partitions(key or keys)` registers each key as a dynamic partition and reports a materialization for it (duplicates collapse), and a run launched with the tag `airflow/partition_key` records that key when the step succeeds. A consumer with the identity mapping gets the same dynamic partition set and an eager automation condition. A reference to an asset by name (`Asset.ref(name=...)`) resolves to the producer's key.

## Still deferred (8)

Consumers of runtime-partitioned assets with a rollup, fan-out, product or allowed-key mapping (5 DAGs, Airflow 3.3 only), a timetable with a mapper per asset (1), a mapped task that combines several argument shapes (1), and a mapped task whose fixed arguments are not plain values (1).

## `--output components`: registry components instead of generated Python

`convert_ir.py --output components` writes `defs/<dag>/defs.yaml` for each DAG whose every task has a component form, and keeps generated Python for the rest (the report's `output` field says which). The YAML holds one component per task (documents separated by `---`) and a `CronScheduleComponent` for the schedule:

| Airflow | Component |
|---|---|
| `BashOperator` | `ShellCommandAssetComponent` (retries, timeout, env, cwd, `skip_on_exit_code: 99`) |
| `PythonOperator`, `@task` | `PythonCallableAssetComponent` (the callable is copied to `callables/<dag>.py`) |
| S3 copy, delete (key list), list, upload, create object | the matching `S3...AssetComponent` with `resource_key` set to the connection id |
| `SQLExecuteQueryOperator` (not Snowflake), `PostgresOperator`, `MySqlOperator`, `MsSqlOperator`, `SqliteOperator` | `SqlAssetComponent` with `connection_env` (the `<CONN>_URL` variable), one statement per `sql_statements` entry. Only when the statements are plain text, `parameters` is a dict, nothing is split, and with `autocommit` there is a single statement: the component runs everything in one transaction and returns no rows |
| `BashSensor` with `retry_exit_code`, `TimeSensor` | `WaitUntilAssetComponent` (`shell_exit_zero` with `retry_exit_code`, or `time_of_day` in the DAG's timezone), `poll_seconds` and `timeout_seconds` from the sensor. Not `BashSensor` without `retry_exit_code` (Airflow keeps polling on any failure, the component fails at once), not a sensor with `soft_fail` (the component does not skip what follows), not `TimeDeltaSensor`, `FileSensor` or `PythonSensor` (reference time, directory and callable semantics differ) |
| `EmptyOperator` | none: dependencies are rewired through it |

A DAG stays Python when any task is templated (the component YAML has its own Jinja), consumes another task's value, is mapped, gated, or a branch, has outlets or inlets, a pool, an SLA, callbacks or a caveat, or when its schedule is anything but a cron or none. Asset keys in this form are single-part, `<dag>__<task>`, because the components name their asset with one string. A DAG without a schedule gets no job. On the example corpus 18 DAGs convert this way and load and run in a Dagster project; the rest stay Python. The SQL and wait components were validated by building each generated document with the registry's own component class (a wrong field name fails) and materializing it, against SQLite for the SQL. `KubernetesPodOperator` has no component form: `k8s_job_asset` has no `args`, labels, node selector, tolerations, env-from or kubeconfig fields, runs only with in-cluster config, and names every Job from the first 32 characters of the run id, so two pod steps in one run would collide (see the component wishlist). Snowflake, triggers, and anything that needs `post_processing` are the next candidates.

## Caveats: convert the shape, mark what is not translated

By default (`--caveats raise`) a DAG with one untranslatable task keeps its shape and the task is written as a stub: an asset in its place whose body calls `af_caveat(reason)`, with an `# AIRFLOW-CAVEAT:` comment. It applies to a callable that uses the Airflow API, an unresolved import or an async function, an operator with no conversion yet (a provider operator that is not in the table below, a provider operator with an argument the mapping cannot carry, human-in-the-loop operators), `templates_dict`, and templates that use names the runtime does not rebuild. The report lists each one under the DAG's `caveats`, and `apply_conversion.py` leaves that task's unit `deferred` with the reason.

Two kinds of caveat appear in the report: `stub` (the task body is not translated) and `degraded` (translated, with a known difference: for example the settings listed in the table below, a dropped `ExternalTaskSensor` wait that the converter cannot express, or DAG-level callbacks that were not attached). Each unconverted operator names the registry component that covers the same work (`OPERATOR_COMPONENTS` and `HINTS` in `convert_ir.py`), in the stub message and in the report's `operators` section.

- `--caveats raise` (default): the stub fails when it runs, with the reason in the message, so nothing succeeds by accident.
- `--caveats skip`: the stub returns nothing and the rest of the DAG runs. Use it to see the shape run end to end; read the caveat list before trusting any output.
- `--caveats off`: no stubs and no degraded caveats; the whole DAG is deferred with the reason instead.

Whole-DAG problems still defer the DAG: producer conflicts, schedule shapes, a SubDAG, a partitioned DAG with steps that are not converted, a custom operator class that cannot be copied.

## What it defers, always with the reason

A whole DAG is deferred only when part of it cannot be expressed at all (a partial conversion would silently change the graph): an asset produced by unordered tasks or by two DAGs in a way it cannot split, a SubDAG, a schedule shape with no mapping (a timetable with a mapper per asset, custom timetables), `catchup=True` (unless `--catchup ignore`), a custom operator class that cannot be copied, an operator argument that is not a plain value, the mapped or partitioned shapes under "Still deferred", and any callable whose arguments cannot be wired (with `--caveats off`). Jinja templates, task context, dynamic mapping, trigger rules, setup/teardown and callbacks are translated (see above and the settings table below), not deferred. Everything else that cannot be translated is a loud stub or a `degraded` caveat (next section) unless `--caveats off`, which turns each of them into a deferral of the whole DAG. A converter bug shows up as a deferral named `CONVERTER ERROR`, never a crash or a silent skip.

## Rules this encodes (see also factories.md and dynamic-mapping.md)

1. One producer per asset. Chained writers of one outlet collapse to the last; unordered writers need an owner decision.
2. A task that reads and writes the same asset cannot depend on itself; the self-dependency is dropped and noted.
3. Multi-asset AND triggers need a per-asset condition, not `eager()` (which fires on any update).
4. Data passing is the IO manager's job. Check what your deployment's default IO manager does when steps run in different processes.

## Existing integrations and community components

Prefer an official `dagster-*` integration or a registry component over hand-written helpers (the dagster-expert skill's rule). `convert_ir.py` writes an `operators` section into `report.json`: every operator type in the IR with its task count, whether the converter maps it, and the integration or component that fits. Component ids below were found by reading the installed `dagster-community-components` package (each `schema.json` and `README.md`); confirm with `dagster-component info <id>` before relying on them.

| Airflow | Official integration | Registry component | What the converter does |
|---|---|---|---|
| `BashOperator`, `@task.bash` | none usable: `dagster-shell` on PyPI is stale (0.25.13, pins `dagster==1.9.13`) | `shell_command_asset` | Python by default; the component with `--output components` |
| S3 operators | `dagster-aws` `S3Resource` | `s3_*_asset`, `aws_s3_resource` | `S3Resource` calls by default; the components with `--output components` |
| Snowflake operators | `dagster-snowflake` `SnowflakeResource` | `snowflake_resource`, `snowflake_sql_asset`, `snowflake_copy_into_asset` | `SnowflakeResource` calls (the registry assets are not emitted: no `USE` overrides or statement splitting) |
| `KubernetesPodOperator` | `dagster-k8s` `execute_k8s_job` | `k8s_job_asset` | `execute_k8s_job`; see the component wishlist for what `k8s_job_asset` lacks |
| HTTP operators and sensor | none | `http_resource`, `http_external_asset`, `http_poll_sensor`, `http_webhook_job` | `requests` (no component returns a response body or holds a step open) |
| SQL on other databases | none | `sql_asset` (`connection_env`) | SQLAlchemy call; the component with `--output components` when the DAG has nothing the component cannot carry |
| Email, Slack webhook | none | `smtp_send_asset`, `slack_message_send`, `slack_notification_job` | `smtplib`, `requests` (the components need a DataFrame or are jobs) |
| Cosmos `DbtDag`, `DbtTaskGroup` | `dagster-dbt` `DbtProjectComponent` (official; the registry's `EnrichedDbtProjectComponent` is a drop-in superset) | `EnrichedDbtProjectComponent`, `DbtRunJobComponent` | the official component as `defs.yaml`; see "Astronomer Cosmos" |
| dbt Cloud, Airbyte, Fivetran, Databricks | `dagster-dbt`, `dagster-airbyte`, `dagster-fivetran`, `dagster-databricks` | `dbt_cloud_trigger_job`, `airbyte_sync_trigger_job`, `fivetran_sync_trigger_job`, `databricks_asset_bundle` | integration resource calls (the registry jobs are jobs, not steps in a DAG) |
| `TriggerDagRunOperator` | none | `asset_to_job_trigger_sensor`, `trigger_job_asset` | the runtime's own launch (an asset dependency or automation condition is usually better) |
| `PythonVirtualenvOperator`, `ExternalPythonOperator` | Dagster Pipes (`PipesSubprocessClient`) | none | `af_isolated` in the runtime |
| time and task sensors | none | `wait_until_asset` | polling step in the runtime; `wait_until_asset` with `--output components` |

Operators with no mapping yet are listed in [component-wishlist.md](component-wishlist.md) with what is missing.

## Compatibility shim: S3Hook

Callables that construct `S3Hook` themselves cannot reach a Dagster resource. The generated `runtime.py` includes an `S3Hook` shim for the commonly used methods (`load_file`, `load_string`, `load_bytes`, `check_for_key`, `list_keys`, `delete_objects`, `copy_object`). It was checked against Airflow's real hook with a recording fake boto client: 11 of 11 scenarios identical (calls, return values and errors). It uses the default AWS credential chain, and `read_key` (boto's resource API), compression, encryption and ACL options raise `NotImplementedError` instead of behaving differently. A converted DAG that uses it carries a `NOTE:` in its module.

## Task and DAG settings: translated, degraded or deferred, never ignored

Loading a converted project proves nothing about behavior. Running both sides (below) found settings the converter had silently dropped, so every setting that changes what happens is now handled explicitly. "Degraded" is a caveat in the report (the DAG converts and the report says what is not enforced); with `--caveats off` each degraded setting defers the DAG.

| Setting | Handling |
|---|---|
| `retries`, `retry_delay`, `retry_exponential_backoff`, `pool`, task groups | translated (`RetryPolicy` with `Backoff.EXPONENTIAL`, `pool`, key prefix) |
| `execution_timeout` on a shell task | translated (`subprocess` timeout; the task fails, as in Airflow) |
| `execution_timeout` on a Python task | degraded (a callable cannot be interrupted safely) |
| `trigger_rule` other than `all_success` | translated in the graph form (Airflow's own rule table); a rule the table does not have defers the DAG |
| `sla` on a task | translated (an `sla_breach` observation; the task is not stopped, as in Airflow) |
| `depends_on_past`, `wait_for_downstream` | degraded (not enforced) |
| `max_active_tis_per_dagrun`, `pool_slots != 1` | degraded (not enforced; one slot is used) |
| `max_active_tis_per_dag` | translated to a Dagster pool of that size (the limit goes in `concurrency_limits.yaml`); defers with a named pool or a value that is not a positive number |
| task callbacks (`on_failure_callback`, `on_success_callback`, `on_retry_callback`) | translated with `@on_hooks` in the asset form; degraded in the graph form |
| other hooks (`pre_execute=`, `post_execute=`, `@task.run_if`, `skip_if`, `on_skipped_callback`, `on_execute_callback`) | degraded (Airflow keeps the hook in `_pre_execute_hook`; the default no-op methods are not hooks) |
| DAG `fail_stop=True` | degraded (Airflow cancels running tasks on the first failure; Dagster lets independent steps finish) |
| DAG `max_active_runs` / `max_active_tasks` other than 16 | translated to a run-queue tag limit and a per-run executor limit written to `concurrency_limits.yaml` for the deployment to apply; a value that is not a positive number defers |
| DAG `dagrun_timeout` | translated to a `dagster/max_runtime` tag (enforced only with run monitoring); defers when not a number of seconds |
| DAG `max_consecutive_failed_dag_runs`, `render_template_as_native_obj` | degraded |

Detection traps this exposed: Airflow 2's decorated mapped tasks are `DecoratedMappedOperator`, a subclass of `MappedOperator`, so test the class hierarchy, not the class name; and `pre_execute` / `post_execute` exist on every operator as no-op methods, so only an override or a stored hook counts.

## Execution parity

`examples/runtime_conversion/parity/` runs each eligible converted DAG twice: with Airflow's own `dag.test()` in a sandbox, and by materializing the converted assets in Dagster. It compares every task's outcome and, where Dagster stores a value, the value passed between tasks. Eligible means no external resources and no network or long sleeps in the commands. Pitfalls in the harness itself:

- Resolve every output path before changing into the temp working directory the DAGs run in.
- A per-DAG time limit that is shorter than the DAG's total sleeps interrupts both sides at different points and looks like a behavior difference; set the limit above the DAG's real duration.
- Airflow 3's `dag.test()` needs the DAG serialized into the database first (`sync_bag_to_db`) and a migrated database; Airflow 2 needs `initdb()`.

## Measured results

Corpus: 292 DAGs from 14 public repos (heavy on teaching repos for exactly the constructs that need a design decision, so do not read the conversion rate as typical).

- **Conversion:** 284 of 292 DAGs convert (59 of them with at least one stubbed task) (`--caveats off` defers every DAG that would need a stub or a degraded setting; the report counts each kind) (partitioned DAGs are not in the parity comparison below: they need a partition to run). The rest are deferred, each with its reasons in `report.json`. Every converted repo loads in a real Dagster project (`load_check2.py`). Concurrency is mapped to Dagster concepts and written to a generated `concurrency_limits.yaml` that the deployment must apply: `max_active_runs` becomes a run-queue tag limit, a named pool or `max_active_tis_per_dag` becomes a Dagster pool (limit set with `dagster instance concurrency set`), and `max_active_tasks` becomes a per-run executor limit (exact only when runs are limited to one at a time). `pool_slots` above 1 and `max_active_tis_per_dagrun` are degraded caveats (not enforced). These mappings are checked by loading and by unit tests; the pool and queue limits were not exercised under a running daemon.
- **Execution parity** (Airflow's own `dag.test()` against Dagster materialization, on converted DAGs with no external resources and no network or long sleeps): 117 DAGs, 115 compared and 2 not comparable (Airflow itself failed in the harness: a deadline-alert serialization error, and a param with no default). Of the 115, 106 had identical outcomes. The other 9 differ only in values that cannot match: random picks (five, one of them a random dog picture from a web API), timestamps (two), file listing times (one) and a tuple that Airflow's XCom stores as a tagged dict (one). Covered: branches and short-circuits, trigger rules, setup/teardown, mapped tasks, polling sensors, clock branches and triggered DAGs, in Airflow 2.10 and 3.4.
- **Every converted DAG run in Dagster** (all 284, in-process with an ephemeral instance; partitioned DAGs given a partition key): 194 ran clean; 48 stopped at a marked caveat stub (the loud `AIRFLOW-CAVEAT` failure, as designed); 26 failed for reasons outside the converter (DAGs that fail on purpose, a network call, a data file or a package the example assumes, a failed upstream in a failure-path demo); 16 were only loaded (they need AWS or Snowflake credentials). This pass found and fixed: `templates_dict` read through `**kwargs`, `self` for `@asset` functions, `outlet_events`/`inlet_events` keyed by a URI string and `inlet_events.for_asset(...)`, the background `TriggerDagRunOperator` launch, a trigger whose target DAG is not in the converted set (now a stub), and loop-body callables that are left unexpanded (now stubs, as are callables that take `asset_state_store`).
- **Passed values:** 435 task outcomes and 165 returned values were compared; 155 of the values were identical, and the 10 that differ are the cases above. For a mapped task Airflow records one instance and Dagster the collected list, so that check passes if Airflow's value appears in the list (a loose rule). Skipped, failed and branch markers are not compared as values. Nothing here ran against real S3 or Snowflake. Schedules were checked structurally (cron, timezone), not by waiting for ticks. The failure paths (a task that fails, a failed setup or teardown) are covered by unit tests, not by comparing against Airflow running a failing DAG.
- **Bugs the comparison found that loading did not** (all fixed and covered by tests): decorated mapped tasks misdetected, `execution_timeout` ignored, `fail_stop` ignored, `@task.run_if` / `skip_if` hooks ignored, a harness timeout that looked like a behavior difference, and (found in the second pass) unsupported `macros.*` calls, `dag_run` attributes the runtime does not provide, and `templates_dict`, all of which were accepted at convert time and failed at run time; and, in the third pass, a shell task's output (the last stdout line is its XCom, parsed as JSON on Airflow 3), a pull of a task id that does not exist (Airflow returns None), and `do_xcom_push=False`; and, in the fourth pass, weekday branches given as numbers, and short-circuits that keep downstream trigger rules (`ignore_downstream_trigger_rules=False`). The incremental runner hashes a DAG's generated module, so a change to the shared runtime needs `--all` or the affected DAGs forced. Expect the first run on your own repo to find more; run the comparison.
