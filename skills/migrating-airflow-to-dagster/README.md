# airflow-to-dagster

Migrate Apache Airflow DAGs to Dagster. The skill (`SKILL.md`) is the method: pick a tier (observe, bridge or native), inventory the Airflow project, convert what has a mechanical mapping by code, hand-convert the rest, then check behavior against Airflow before cutover. The scripts in `scripts/` do the deterministic parts: a static scanner, a runtime extractor, a converter that writes Dagster code, a manifest with a completeness gate, and parity harnesses.

**Two routes into tier 3.** The tiers are where the work runs (observe, bridge, native); inside native there are two routes for how the code gets written: the deterministic **converter** (the mechanical bulk, the default) and the **rewrite** route, a generated spec and prompt from which Claude writes idiomatic Dagster, gated like the converter's output (for DAGs the converter stubbed or deferred, redesign-heavy DAGs and pilots). It is expected to be more idiomatic and to cover more operators; the evidence is in [reference/rewrite-route.md](reference/rewrite-route.md), and it costs model effort per DAG.

**Status: scripts, not a packaged CLI.** There is no `pyproject.toml` and no installed command; each step is a Python script run from `scripts/`. Conversion covers Python, Bash, S3 and Snowflake tasks, the provider operators in the table below (HTTP, SQL, Kubernetes pods, email, Slack webhook, dbt Cloud, Airbyte, Fivetran, Databricks), plus the scheduling, branching, mapping and partition constructs. Astronomer Cosmos DAGs (`DbtDag`, `DbtTaskGroup`, the dbt operators) convert to Dagster's dbt component. Other provider operators become loud stubs. The skill also plans the migration in waves before conversion and writes a cutover checklist per DAG after it. Nothing here has run against a live Airflow deployment, real S3, Snowflake, Kubernetes, dbt Cloud, Airbyte, Fivetran or Databricks; each provider mapping says how far it was checked in [reference/conversion.md](reference/conversion.md).

## Quick start

```bash
# 1. Inventory (read-only; never imports DAG code)
python3 scripts/inventory.py path/to/airflow_repo --out manifest.json

# 2. Extract what Airflow actually built. EXECUTES the DAG files: sandbox, trusted code,
#    same Airflow version and providers as the project.
python3 scripts/airflow_ir.py path/to/dags --out ir.json [--pythonpath path/to/project_root]
python3 scripts/airflow_truth.py path/to/dags --out truth.json
python3 scripts/reconcile.py manifest.json truth.json --out manifest.json

# 2b. Plan in waves (heuristic; deterministic). Run again with --ir and --report after step 3.
python3 scripts/plan.py manifest.json --ir ir.json --out-dir plan

# 3. Convert, then record the outcome in the manifest
python3 scripts/convert_ir.py ir.json --out-dir my_project/src/my_pkg/defs --report report.json
python3 scripts/apply_conversion.py manifest.json ir.json report.json --out manifest.json

# 4. Gates and completeness
python3 scripts/validate_defs.py my_project --manifest manifest.json --advance
python3 scripts/status.py summary manifest.json            # exits nonzero while any unit is pending
python3 scripts/status.py defer manifest.json A031 --reason "custom timetable"

# 4b. Connections: names only, never a value (delete the export afterwards)
python3 scripts/connections_to_resources.py connections.json --out-dir connections_out

# 4c. The rewrite route for a DAG the converter stubbed or deferred (see SKILL.md, "When to use the rewrite route")
python3 scripts/dag_spec.py ir.json --report report.json --manifest manifest.json --out-dir rewrite
python3 scripts/rewrite_prompt.py rewrite/<dag>.spec.json --converter-module defs/<dag>.py --out-dir rewrite   # give rewrite/<dag>.prompt.md to Claude
#   --converter-project <converter output dir>: the converter's project as read-only reference (default --converter-mode reference: rewrite from the original, copy only logic it verified, its stubs are the hard parts); --converter-mode start improves it instead (default off)
python3 scripts/accept_rewrite.py --spec rewrite/<dag>.spec.json --project . --manifest manifest.json

# 5. Cutover checklists, one per converted DAG (writes markdown; pauses and deletes nothing)
python3 scripts/cutover.py manifest.json report.json --ir ir.json --parity-state parity_state --out-dir cutover
```

Create the target project with `uvx create-dagster project <name> --uv-sync` first (the skill relies on `dagster-expert` for the Dagster side). `convert_ir.py --help` lists the options: `--caveats {off,raise,skip}`, `--catchup {defer,ignore}`, `--mapping {graph,partitions}`, `--output {python,components}`, `--dataset-keys {path,script-orchestrator}`, `--granularity {product,task}` (product is the default; task is the one-asset-per-task fallback), `--partition-start`, `--return-all`, `--dbt-dir` (where the dbt projects of Cosmos DAGs are expected, default `dbt`).

## What gets converted

Details and the full list are in [reference/conversion.md](reference/conversion.md) and [reference/mapping.md](reference/mapping.md).

| Airflow | Dagster |
|---|---|
| `PythonOperator`, `@task` | Asset running the same function; its source is copied with only the imports, constants and helpers it uses |
| `BashOperator`, `@task.bash` | Asset running the command with `bash -c` |
| `EmptyOperator` | Elided; edges rewired |
| Value passed between tasks | Asset parameter (the IO manager carries it) |
| S3 list, copy, delete (explicit keys), upload operators; `S3Hook` in callables | Assets calling boto through the `aws_*` resource; a mirrored `S3Hook` subset in `runtime.py` |
| Snowflake COPY INTO, `SnowflakeOperator`, `SQLExecuteQueryOperator` on a Snowflake connection | Assets running the SQL through the Snowflake resource |
| `HttpOperator`, `SimpleHttpOperator`, `HttpSensor` | `requests` step (body is the value); sensor as a polling step. Connection from `<CONN>_BASE_URL` etc. Run against a local HTTP server |
| `SQLExecuteQueryOperator` (not Snowflake), `PostgresOperator`, `MySqlOperator`, `MsSqlOperator`, `SqliteOperator` | SQLAlchemy step (`<CONN>_URL`); with `--output components` a call on `sql_url_resource` (0.10.194; the converter splits statements). Run against SQLite |
| `KubernetesPodOperator` | `dagster-k8s` `execute_k8s_job`. Job built by the real library, cluster calls faked; **not run on a cluster** |
| `EmailOperator`, `SlackWebhookOperator` | `smtplib` and `requests` steps. Run against a local SMTP and HTTP server. With `--output components`: `smtp_resource` and `slack_resource` (0.10.194), run against the same stand-ins |
| `SlackAPIPostOperator`, `S3KeySensor`, `S3ListPrefixesOperator`, S3 bucket create and delete, the GCS operators (upload, download, list, delete, existence sensor, bucket create and delete), `SSHOperator`, `DockerOperator`, `LambdaInvokeFunctionOperator`, `BigQueryInsertJobOperator` (query jobs), `SparkSubmitOperator` | Steps on `dagster-slack`, boto3, `dagster-gcp`, `dagster-ssh`, the `docker` SDK, boto3 Lambda and `spark-submit`; `--output components` writes `lambda_invoke_asset`, `ssh_asset`, `docker_container_asset` and `bigquery_query_asset` where the arguments fit. Run against recording fakes, botocore's Stubber and a stand-in `spark-submit`, **not the real services** (a degraded caveat says so); unsupported arguments are loud stubs; see [reference/conversion.md](reference/conversion.md) |
| `DbtCloudRunJobOperator`, `AirbyteTriggerSyncOperator`, `FivetranOperator`, `DatabricksRunNowOperator`, `DatabricksSubmitRunOperator` | `dagster-dbt`, `dagster-airbyte`, `dagster-fivetran`, `dagster-databricks` resources in `resources.py`. Recording fakes and signature checks only; **not run against the services** (a degraded caveat says so) |
| `BashSensor` (with `retry_exit_code`), `TimeSensor` | `wait_until_asset` with `--output components` |
| With `--output components` only (resource-first, `reference/conversion.md`): `HttpSensor` (`http_resource.poke`), `SSHOperator`, `LambdaInvokeFunctionOperator`, `SlackAPIPostOperator`, `DatabricksSubmitRunOperator` and run-now by `job_name`, every `SparkSubmitOperator` flag, `EmrAddStepsOperator`, `EmrCreateJobFlowOperator`, `EmrTerminateJobFlowOperator`, the four Dataproc operators, the five Pub/Sub operators, `AthenaOperator`, `RedshiftDataOperator`, `BigQueryCreateEmptyDatasetOperator`, `BigQueryDeleteDatasetOperator`, `BigQueryCreateEmptyTableOperator`, `BigQueryDeleteTableOperator`, `BigQueryValueCheckOperator` | registry resources of `dagster-community-components` 0.10.194 called in plain Python from an asset; each was loaded by dagster's component loader and run against a local stand-in, a botocore Stubber or a recording fake client (no live service). An argument a resource cannot carry keeps the plain-Python shim (or, for operators that have no shim, the Python route) and a caveat names the gap. dbt Cloud, Fivetran and Airbyte stay on their integration libraries on purpose (the reasons are in `reference/conversion.md`) |
| Outlets and inlets | Asset keys and dependencies |
| Schedule: none, `@once`, cron | No schedule or `ScheduleDefinition` with the timezone |
| Schedule: one asset, any of several, all of several | `AutomationCondition.eager()`; all-of uses a per-asset `newly_updated().since_last_handled()` condition |
| Task groups, retries, retry delay, backoff, pool | Key prefix, `RetryPolicy`, `pool` |
| Connections | Resources under the same key (`resources.py`) |
| Branch, short-circuit, mapped tasks (`.expand`), setup/teardown, trigger rules | One graph-backed multi-asset with an op and an asset per task, every Airflow edge declared as an asset dependency (default), or independently runnable partitions with `--mapping partitions`; see [reference/dynamic-mapping.md](reference/dynamic-mapping.md) |
| `ExternalPythonOperator`, `@task.external_python`, `@task.docker`, `@task.kubernetes` | Dagster Pipes: `PipesSubprocessClient` on the operator's `python` (env override `<TASK>_PYTHON`), `PipesDockerClient` / `PipesK8sClient`; the function becomes a generated script, arguments go as `extras` (JSON), the return value comes back as the asset value |
| `PythonVirtualenvOperator`, `@task.virtualenv` (and branch variants) | No Dagster equivalent (no per-step virtualenv at run time): the callable runs in-process in the project's environment, its `requirements` join `converted_requirements.txt`, a loud caveat names code locations / Pipes as the isolation tools, simple pin conflicts are detected |
| `TriggerDagRunOperator` (no `wait_for_completion`), task `sla`, `ExternalTaskSensor` and time sensors (some forms) | Translated, with caveats where behavior differs; see [reference/conversion.md](reference/conversion.md) |
| `CronPartitionTimetable`, `PartitionedAssetTimetable` with identity and start-of-period mappers | Partitioned assets and per-partition automation; see [reference/partitions.md](reference/partitions.md) |
| Cosmos `DbtDag`, `DbtTaskGroup` (any execution mode: local, virtualenv, docker, kubernetes, aws_eks, aws_ecs, azure_container_instance, gcp_cloud_run_job) | **One** `dagster_dbt.DbtProjectComponent` (`defs.yaml`): an asset per dbt model, seed and snapshot with the dbt lineage, tests as asset checks, `select`/`exclude`/`selector`, `--vars`, an env-var based `profiles.yml` for `ProfileConfig` (no secret or profile file is copied); tasks before and after the group stay asset dependencies. Run against dbt and DuckDB. The dbt project is used unchanged; container modes convert like local mode with an informational note; task retries become run retries; `node_converters`, a container that runs something other than dbt, and non-build operators are loud stubs; see [reference/conversion.md](reference/conversion.md) |
| Cosmos per-task operators (`DbtRunLocalOperator`, `DbtBuildLocalOperator`, `DbtSeedLocalOperator`, `DbtSnapshotLocalOperator`, `DbtTestLocalOperator`) | One asset per operator running the same dbt command through `DbtCliResource` (no model-level lineage: the operator only holds a `select`) |
| dag-factory, Blueprint and loop-generated DAGs | Converted from the runtime IR like any DAG; factory handling in [reference/factories.md](reference/factories.md) |

Generated output per project: `tests/test_<dag>.py` (load, shape, schedule and, for DAGs without external systems, a materialize run; structural asset checks are added to the modules, counted in `report.json` under `generated`), `defs/<dag>.py`, `runtime.py` (shims copied into the project), `resources.py`, `sources.py` (assets a DAG reads but no converted DAG produces), `converted_requirements.txt`, `concurrency_limits.yaml` (limits the deployment must apply), and the `report.json`. `report.json` marks each DAG written as registry components with `converted_with_component`, `component_types` and `resource_types`. With `--output components` (resource-first, registry 0.10.194), DAGs whose tasks all have a form below are written as `defs/<dag>/defs.yaml` (asset components only for Bash, Python callable, Docker, `BashSensor`/`TimeSensor`, cron schedule), `defs/<dag>_steps.py` (service-calling operators as assets that call a resource in plain Python) and `defs/registry_resources/defs.yaml` (`http_resource`, `databricks_jobs_resource`, `spark_submit_resource`, `s3_bucket_resource`, `gcs_bucket_resource`); the report adds `resource_types`. The rest stay Python.

Connections move with `connections_to_resources.py` (names only). A callable that calls `Connection.get(...)` or `BaseHook.get_connection(...)` keeps working through an environment shim that reads `AIRFLOW_CONN_<ID>` (loud, naming the variable, when it is missing). Mapped task groups (`@task_group.expand`), helpers that close over plain DAG-function locals, `ObjectStoragePath` arguments and module-level paths (a `pathlib` / `universal_pathlib` shim, built on first use) and user-defined mapped operators convert; deferrable operators and triggers run in place as plain Python (no triggerer: the step waits); `NEVER_EXPIRE`, the async task state store, `task.render_template`, a callable that takes the whole `context` (with `var` and `conn`) and `templates_exts` files are shims or conversions; Airflow metadata and session calls (`DagRun`, `TaskInstance`, `provide_session`) stay loud stubs that name the Dagster equivalent. Not converted by code: Variables, `default_args` beyond retries, alerts and callbacks, and deployment (the cutover checklist lists each one that applies to a DAG). Those are the manual "platform layer" step in `SKILL.md`. Pass 5 (see `reference/converter-triage.md`): DAG-factory closure variables are resolved per generated DAG (the extractor records each plain literal the callable reads from the factory function), `Dataset` / `Asset` objects in callables are plain classes (`uri`, `name`, `extra`), `asset_state_store` is a JSON-file shim per asset, task callbacks (`on_execute` too, functions and notifier instances) run through `@on_hooks` / the step, and a DAG's `on_success_callback` / `on_failure_callback` become run-status sensors (`sla_miss_callback` is reported, not converted); `FanOutMapper` / `RollupMapper` over runtime partitions stay deferred with a message saying what is missing. The behaviour harness reports a route that only "matches" a failing baseline because its failing step is a loud stub as `stub-matches-failure` (never a success) and recomputes each DAG's stratum from the current converter report.

## When something cannot be converted

- **Loud stubs.** With `--caveats raise` (default) a task that cannot be translated (Airflow API in the callable, unresolved import, unsupported operator or template) keeps its place as an asset whose body raises `AIRFLOW-CAVEAT: <reason>`. `skip` returns nothing instead; `off` defers the whole DAG.
- **Two kinds of caveat in `report.json`:** `stub` (not translated) and `degraded` (translated, with a stated difference, e.g. `depends_on_past` (a failing check, not a wait, in the asset form; not enforced elsewhere), `pool_slots`, `fail_stop` not enforced).
- **Deferrals.** Whole-DAG problems (producer conflicts, some schedule shapes, SubDAGs, a custom operator class that cannot be copied) defer the DAG with every reason. A converter bug surfaces as a deferral named `CONVERTER ERROR`, not a crash.
- **Operator report.** `report.json` has an `operators` section: each operator type, its task count, whether it is mapped, and the integration or registry component that fits.
- **Manifest.** `apply_conversion.py` writes a `target` per converted unit and defers the rest with the reason. A unit whose task is a `stub` caveat stays `deferred`; a `degraded` caveat does not defer the unit (read the report).

## Validation and parity

1. `validate_defs.py`: `dg list defs --json` loads the project, and every unit's target (assets, deps, jobs, schedule crons) exists and matches. A unit with no target is reported UNPLANNED. `--advance` marks passing MECH units complete; it does not prove behavior.
2. `status.py summary`: exits nonzero while any unit is neither `complete` nor `deferred` with a reason.
3. Parity ([reference/parity.md](reference/parity.md)): `parity_incremental.py CONFIG.json` runs each converted DAG with Airflow's `dag.test()` and with `dg.materialize` in separate interpreters and compares overall outcome, each task's outcome and returned values (`scripts/parity/`). It only re-runs DAGs whose generated module hash changed. [examples/three_dags/](examples/three_dags/) has function-level and automation-condition parity checks.

Measured results are recorded in [reference/conversion.md](reference/conversion.md) and [reference/compatibility.md](reference/compatibility.md): on a corpus of 292 DAGs from 14 public repos, 284 convert (59 with at least one stubbed task); parity on 117 eligible DAGs found identical outcomes on 106 of 115 comparable ones (the rest differ in random or time-based values). The corpus leans toward teaching repos, so do not read the rate as typical. The converter was also checked from Airflow 2.2 to 3.4 ([reference/compatibility.md](reference/compatibility.md)); behavior comparison runs from 2.5 on.

## Added in the latest passes

- **Virtualenv, external-Python and container tasks.** Dagster has no per-step virtualenv built at run time (Airflow's `@task.virtualenv` works around a shared worker environment). Dependencies are isolated per code location, or with Dagster Pipes. The converter runs `PythonVirtualenvOperator` / `@task.virtualenv` in the project's environment, adds its `requirements` to `converted_requirements.txt` and writes a loud caveat (`scripts/isolation_ops.py`). `ExternalPythonOperator` / `@task.external_python` become `PipesSubprocessClient` on the operator's interpreter (env override `<TASK>_PYTHON`), arguments as `extras`, the return value as the asset value and the metadata `airflow/return_value`. `@task.docker` / `@task.kubernetes` become `PipesDockerClient` / `PipesK8sClient`, checked against recording fakes only: **not run on a daemon or cluster**. The rewrite pattern `reference/rewrite-patterns/venv_pipes.md` says the same; its in-process and external-interpreter forms were run, its container forms were not.
- **Resource-first mapping.** An operator that calls a service becomes plain Python inside an asset that uses a resource: a registry `*_resource` of dagster-community-components 0.10.194 where one fits (`reference/registry-resources-0.10.194.md`, `scripts/registry_map.py`), otherwise an integration resource from `resources.py`. Deliberately not mapped to the registry components: Fivetran (`fivetran_sync_trigger_job`: endpoint unconfirmed), dbt Cloud (`dbt_cloud_resource` switches the job's schedule off on every trigger) and Airbyte (`airbyte_sync_trigger_job`: self-hosted basic auth only, does not wait by default). Reasons are in `reference/conversion.md`.
- **Product granularity** is the converter's default (`--granularity product`): one asset per data product, step tasks folded into the asset they feed; `--granularity task` is the one-asset-per-task fallback.
- **Reference mode for the rewrite route.** `rewrite_prompt.py --converter-project DIR` gives the worker the converter's project as read-only reference by default (`--converter-mode reference`: rewrite from the original and the spec, copy only verified logic; the converter's stubs are the hard parts); `--converter-mode start` uses it as the starting point instead. The evaluation harness compares the arms ("arm C" is the reference-mode arm; see `reference/rewrite-route-evaluation-runbook.md`).
- **Verdict snapshot regression guard.** `scripts/verdict_snapshot.py save|check` compares a measurement run against `reference/verdict-snapshot.json` (every measured DAG's verdict, thin or substantive, and a digest of the converter's report) and exits 1 on a regression.
- **Call and command trace (being built).** What exists in the tree: `scripts/parity/call_trace.py` (records, inside the sandbox, the arguments, return values and exceptions of the user's own Python functions and the commands run) and `scripts/parity/trace_compare.py` (compares the two sides' traces: paired by name or body fingerprint, nondeterministic functions reduced to call counts), with `sandbox.py`, `harness_cfg.py` and `behavior_compare.py` wired to them (`EVAL_TRACE=0` turns it off) and `eval_kit.py` shipping both files. It is uncommitted work with tests (`test_eval_trace.py`); it has not been used to produce a published measurement.

## Results (read these honestly)

- **Converter, 242 DAGs with a usable Airflow baseline** (`reference/verdict-snapshot.json`; the full re-measure is `fullrun-report.md` in the evaluation work directory, not in this repository): 200 strict equivalent (119 substantive, 81 thin: the DAG matched on outcome, states and structure but wrote nothing observable), 5 equivalent-with-differences, 16 matches-baseline-failure, 2 stub-matches-failure, 3 different, 6 no-output, 10 baseline-failed. **All 242 are training data**: stage 1, stage 2a, fresh1 and fresh2 were each read to drive converter fixes, so these are not generalisation numbers.
- **Held-out figures measured before tuning:** fresh1 45/60 and fresh2 59/82 strict equivalent. No held-out set remains; a new sample from an unread corpus is needed for a generalisation estimate.
- **Rewrite route:** the numbers are from stage 2a (58 evaluated DAGs) with the previous prompt: 66% strict equivalent (38/58) and 88% in either form (51/58) after re-scoring (`stage2a-report-rescored.md` in the evaluation work directory). They are **unmeasured for the current prompt** (resource-first rule, reference mode, pattern changes).
- The 292-DAG conversion and parity figures under "Validation and parity" are older and from a different corpus pass.

## Layout

- `SKILL.md`: the workflow (phases 0 to 7), hard rules, the tier choice.
- `scripts/`: `rewrite_patterns.py` (pattern detection, prompt injection, decision-record parser), `rewrite_resources.py` (resource-first rule, registry-resource table per used operator family, registry-search checklist), `inventory.py`, `airflow_ir.py`, `provider_ops.py` (provider operator mappings), `registry_map.py` (registry resources per operator family), `isolation_ops.py` (virtualenv, external-Python and container tasks), `cosmos_ops.py` (Cosmos mapping), `plan.py` (wave plan), `cutover.py` (cutover checklists), `dag_spec.py`, `rewrite_prompt.py` and `accept_rewrite.py` (the rewrite route), `run_evals.py` (skill-level evals), `connections.py` and `connections_to_resources.py` (Airflow connections to resources and variable names, no values), `airflow_truth.py`, `reconcile.py`, `convert_ir.py`, `apply_conversion.py`, `validate_defs.py`, `status.py`, `parity_incremental.py`, `parity/` (both sides and the comparer; `sandbox.py`, `run_airflow.py`, `run_dagster.py` and `behavior_compare.py` are the behaviour comparison; `call_trace.py` and `trace_compare.py` the call and command trace), `eval_corpus.py`, `eval_baseline.py`, `eval_sample.py`, `eval_kit.py`, `eval_gates.py`, `eval_behavior.py` and `eval_report.py` (the evaluation harness for the two routes), `verdict_snapshot.py` (regression guard: `check` a measurement against `reference/verdict-snapshot.json`), `tests/`.
- `reference/`: `tiers.md`, `mapping.md`, `conversion.md`, `parity.md` (also the harness burn-down and the regression guard), `factories.md`, `dynamic-mapping.md`, `partitions.md`, `compatibility.md`, `component-wishlist.md`, `rewrite-route.md` (the rewrite route and its first, small, not-cold measurement), `rewrite-route-evaluation-runbook.md` and `rewrite-route-evaluation.md` (how to measure both routes against real Airflow, and the generated report), `airlift.md` (peer, observe, proxy; the Airflow 3 limitation), `hosted-airflow.md` (MWAA, Composer, Astro), `cutover-checklist.md`.
- `examples/`: `three_dags/` (a completed trial with equivalence rows), `s3_to_snowflake_factories/`, `dagfactory_family/`, `runtime_conversion/` (converted repo and parity harness), `migration_plan/` (a wave plan and cutover checklists), `granularity/` (task versus product output for one DAG), `rewrite/` (the rewrite route on two DAGs and a sample of eight), `evals/` (fixtures and pass criteria; `python3 scripts/run_evals.py`; `corpus_repos.json` and `corpus/` for the evaluation corpus), `airflow_release_matrix/` (18 one-construct DAG files run across Airflow releases).

## Tests

```bash
unset VIRTUAL_ENV
uv run --no-project --with pytest --with pyyaml --with dagster --with dagster-aws \
  --with dagster-snowflake --with jinja2 --with croniter --with boto3 --with requests \
  --with sqlalchemy --with dagster-k8s --with dagster-dbt --with dagster-airbyte \
  --with dagster-databricks --with dagster-fivetran --with dagster-community-components \
  --with moto --with dbt-duckdb --with duckdb --with dagster-airlift --with dagster-gcp --with dagster-slack --with dagster-ssh --with docker \
  python -m pytest -q scripts/tests
```

617 tests are collected (`pytest --collect-only` under the evaluation work directory's Dagster 1.13.31 virtualenv; the count moves as tests are added). The full suite was not re-run when this line was written; the rewrite-pattern, resource-first and rewrite-route test files (70 tests) pass there with `EVAL_AIRFLOW_PYTHON` set to an Airflow 2.10 interpreter. Tests that need an optional package or Airflow skip when it is missing. `test_rewrite_patterns.py` executes every pattern's code on Dagster, including the external-interpreter Pipes run (set `EVAL_EXTERNAL_PYTHON` to another interpreter that has `dagster-pipes`; it defaults to the test interpreter).

## Limitations

- `airflow_ir.py` and `airflow_truth.py` execute DAG code. Run them only on trusted code in a sandbox.
- Operators convert if they are Python, Bash, S3, Snowflake or in the provider tables above. Others (for example Dataproc, EMR, Glue, Athena, Redshift data, Dataflow, BigQuery dataset and table operators, `GCSToGCS`) become stubs (or defer the DAG with `--caveats off`); the report names the integration or component that fits, and [reference/component-wishlist.md](reference/component-wishlist.md) lists what is missing. A provider operator with an argument the mapping cannot carry (a `response_check` function, `full_pod_spec`, ...) is a stub too, with the reason. Mapped (`.expand`) provider operators other than S3 and Snowflake are stubs.
- A DAG is converted whole or stubbed per task; the converter does not partially convert a graph silently.
- Not enforced in Dagster (reported as degraded or deferred): `depends_on_past` in the graph and partitioned forms (in the asset form it is the library's step-event guard: the step fails when the task's previous run failed, where Airflow waits), `wait_for_downstream`, `max_active_tis_per_dagrun`, `pool_slots` above 1, `execution_timeout` on a Python task, `fail_stop`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`.
- `catchup=True` defers the DAG unless `--catchup ignore`, which runs from now on and does not replay history.
- Eight corpus DAGs remain deferred (consumers of runtime-partitioned assets with rollup, fan-out, product or allowed-key mappings; a timetable with a mapper per asset; some mapped-task argument shapes; a mapped task group over `.map`/`.zip`/`.concat`, or one that contains a branch, sensor, setup/teardown or mapped task, stays a loud stub).
- Parity covers DAGs with no external systems. Operator-based tasks, sensors on external systems, durations, failure behavior and data volume still need a side-by-side run on the same logical window.
- Cosmos: `node_converters`, a container whose entrypoint is not dbt, `run-operation`, docs and other non-build operators are loud stubs; Watcher and airflow_async modes are converted as engine variants (documentation only, not run); Cosmos task retries become the `dagster/max_retries` run-retry tag (the dbt component has no retry-policy field; run retries must be enabled on the deployment); `test_behavior=after_all` becomes `dbt build` (tests run per model, stated as a caveat); the dbt project and its profile secrets are not copied; verified only against DuckDB and Cosmos 1.15.1 on Airflow 2.10.5 ([reference/conversion.md](reference/conversion.md)).
- `dagster-airlift` 0.29.31 reads only Airflow 2.x's `/api/v1` REST API; for an Airflow 3 source use the converter ([reference/airlift.md](reference/airlift.md)).
- Airflow's run history does not migrate.
