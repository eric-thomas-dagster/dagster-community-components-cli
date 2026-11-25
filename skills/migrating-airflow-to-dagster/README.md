# airflow-to-dagster

Migrate Apache Airflow DAGs to Dagster. The skill (`SKILL.md`) is the method: pick a tier (observe, bridge or native), inventory the Airflow project, convert what has a mechanical mapping by code, hand-convert the rest, then check behavior against Airflow before cutover. The scripts in `scripts/` do the deterministic parts: a static scanner, a runtime extractor, a converter that writes Dagster code, a manifest with a completeness gate, and parity harnesses.

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

# 5. Cutover checklists, one per converted DAG (writes markdown; pauses and deletes nothing)
python3 scripts/cutover.py manifest.json report.json --ir ir.json --parity-state parity_state --out-dir cutover
```

Create the target project with `uvx create-dagster project <name> --uv-sync` first (the skill relies on `dagster-expert` for the Dagster side). `convert_ir.py --help` lists the options: `--caveats {off,raise,skip}`, `--catchup {defer,ignore}`, `--mapping {graph,partitions}`, `--output {python,components}`, `--dataset-keys {path,script-orchestrator}`, `--granularity {task,product}`, `--partition-start`, `--return-all`, `--dbt-dir` (where the dbt projects of Cosmos DAGs are expected, default `dbt`).

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
| `SQLExecuteQueryOperator` (not Snowflake), `PostgresOperator`, `MySqlOperator`, `MsSqlOperator`, `SqliteOperator` | SQLAlchemy step (`<CONN>_URL`); `sql_asset` with `--output components`. Run against SQLite |
| `KubernetesPodOperator` | `dagster-k8s` `execute_k8s_job`. Job built by the real library, cluster calls faked; **not run on a cluster** |
| `EmailOperator`, `SlackWebhookOperator` | `smtplib` and `requests` steps. Run against a local SMTP and HTTP server |
| `SlackAPIPostOperator`, `S3KeySensor`, `S3ListPrefixesOperator`, S3 bucket create and delete, the GCS operators (upload, download, list, delete, existence sensor, bucket create and delete), `SSHOperator`, `DockerOperator`, `LambdaInvokeFunctionOperator`, `BigQueryInsertJobOperator` (query jobs), `SparkSubmitOperator` | Steps on `dagster-slack`, boto3, `dagster-gcp`, `dagster-ssh`, the `docker` SDK, boto3 Lambda and `spark-submit`; `--output components` writes `lambda_invoke_asset`, `ssh_asset`, `docker_container_asset` and `bigquery_query_asset` where the arguments fit. Run against recording fakes, botocore's Stubber and a stand-in `spark-submit`, **not the real services** (a degraded caveat says so); unsupported arguments are loud stubs; see [reference/conversion.md](reference/conversion.md) |
| `DbtCloudRunJobOperator`, `AirbyteTriggerSyncOperator`, `FivetranOperator`, `DatabricksRunNowOperator`, `DatabricksSubmitRunOperator` | `dagster-dbt`, `dagster-airbyte`, `dagster-fivetran`, `dagster-databricks` resources in `resources.py`. Recording fakes and signature checks only; **not run against the services** (a degraded caveat says so) |
| `BashSensor` (with `retry_exit_code`), `TimeSensor` | `wait_until_asset` with `--output components` |
| Outlets and inlets | Asset keys and dependencies |
| Schedule: none, `@once`, cron | No schedule or `ScheduleDefinition` with the timezone |
| Schedule: one asset, any of several, all of several | `AutomationCondition.eager()`; all-of uses a per-asset `newly_updated().since_last_handled()` condition |
| Task groups, retries, retry delay, backoff, pool | Key prefix, `RetryPolicy`, `pool` |
| Connections | Resources under the same key (`resources.py`) |
| Branch, short-circuit, mapped tasks (`.expand`), setup/teardown, trigger rules | One graph asset with an op per task (default), or independently runnable partitions with `--mapping partitions`; see [reference/dynamic-mapping.md](reference/dynamic-mapping.md) |
| `PythonVirtualenvOperator`, `ExternalPythonOperator` | Callable run in its own interpreter (`af_isolated`) |
| `TriggerDagRunOperator` (no `wait_for_completion`), task `sla`, `ExternalTaskSensor` and time sensors (some forms) | Translated, with caveats where behavior differs; see [reference/conversion.md](reference/conversion.md) |
| `CronPartitionTimetable`, `PartitionedAssetTimetable` with identity and start-of-period mappers | Partitioned assets and per-partition automation; see [reference/partitions.md](reference/partitions.md) |
| Cosmos `DbtDag`, `DbtTaskGroup` (any execution mode: local, virtualenv, docker, kubernetes, aws_eks, aws_ecs, azure_container_instance, gcp_cloud_run_job) | **One** `dagster_dbt.DbtProjectComponent` (`defs.yaml`): an asset per dbt model, seed and snapshot with the dbt lineage, tests as asset checks, `select`/`exclude`/`selector`, `--vars`, an env-var based `profiles.yml` for `ProfileConfig` (no secret or profile file is copied); tasks before and after the group stay asset dependencies. Run against dbt and DuckDB. The dbt project is used unchanged; container modes convert like local mode with an informational note; task retries become run retries; `node_converters`, a container that runs something other than dbt, and non-build operators are loud stubs; see [reference/conversion.md](reference/conversion.md) |
| Cosmos per-task operators (`DbtRunLocalOperator`, `DbtBuildLocalOperator`, `DbtSeedLocalOperator`, `DbtSnapshotLocalOperator`, `DbtTestLocalOperator`) | One asset per operator running the same dbt command through `DbtCliResource` (no model-level lineage: the operator only holds a `select`) |
| dag-factory, Blueprint and loop-generated DAGs | Converted from the runtime IR like any DAG; factory handling in [reference/factories.md](reference/factories.md) |

Generated output per project: `tests/test_<dag>.py` (load, shape, schedule and, for DAGs without external systems, a materialize run; structural asset checks are added to the modules, counted in `report.json` under `generated`), `defs/<dag>.py`, `runtime.py` (shims copied into the project), `resources.py`, `sources.py` (assets a DAG reads but no converted DAG produces), `converted_requirements.txt`, `concurrency_limits.yaml` (limits the deployment must apply), and the `report.json`. With `--output components`, DAGs whose tasks all have a registry component form are written as `defs/<dag>/defs.yaml` (Bash, Python callable, S3 operators, SQL, `BashSensor`/`TimeSensor`, cron schedule) and the rest stay Python.

Connections move with `connections_to_resources.py` (names only). Not converted by code: Variables, `default_args` beyond retries, alerts and callbacks, and deployment (the cutover checklist lists each one that applies to a DAG). Those are the manual "platform layer" step in `SKILL.md`.

## When something cannot be converted

- **Loud stubs.** With `--caveats raise` (default) a task that cannot be translated (Airflow API in the callable, unresolved import, unsupported operator or template) keeps its place as an asset whose body raises `AIRFLOW-CAVEAT: <reason>`. `skip` returns nothing instead; `off` defers the whole DAG.
- **Two kinds of caveat in `report.json`:** `stub` (not translated) and `degraded` (translated, with a stated difference, e.g. `depends_on_past`, `pool_slots`, `fail_stop` not enforced).
- **Deferrals.** Whole-DAG problems (producer conflicts, some schedule shapes, SubDAGs, a custom operator class that cannot be copied) defer the DAG with every reason. A converter bug surfaces as a deferral named `CONVERTER ERROR`, not a crash.
- **Operator report.** `report.json` has an `operators` section: each operator type, its task count, whether it is mapped, and the integration or registry component that fits.
- **Manifest.** `apply_conversion.py` writes a `target` per converted unit and defers the rest with the reason. A unit whose task is a `stub` caveat stays `deferred`; a `degraded` caveat does not defer the unit (read the report).

## Validation and parity

1. `validate_defs.py`: `dg list defs --json` loads the project, and every unit's target (assets, deps, jobs, schedule crons) exists and matches. A unit with no target is reported UNPLANNED. `--advance` marks passing MECH units complete; it does not prove behavior.
2. `status.py summary`: exits nonzero while any unit is neither `complete` nor `deferred` with a reason.
3. Parity ([reference/parity.md](reference/parity.md)): `parity_incremental.py CONFIG.json` runs each converted DAG with Airflow's `dag.test()` and with `dg.materialize` in separate interpreters and compares overall outcome, each task's outcome and returned values (`scripts/parity/`). It only re-runs DAGs whose generated module hash changed. [examples/three_dags/](examples/three_dags/) has function-level and automation-condition parity checks.

Measured results are recorded in [reference/conversion.md](reference/conversion.md) and [reference/compatibility.md](reference/compatibility.md): on a corpus of 292 DAGs from 14 public repos, 284 convert (59 with at least one stubbed task); parity on 117 eligible DAGs found identical outcomes on 106 of 115 comparable ones (the rest differ in random or time-based values). The corpus leans toward teaching repos, so do not read the rate as typical. The converter was also checked from Airflow 2.2 to 3.4 ([reference/compatibility.md](reference/compatibility.md)); behavior comparison runs from 2.5 on.

## Layout

- `SKILL.md`: the workflow (phases 0 to 7), hard rules, the tier choice.
- `scripts/`: `inventory.py`, `airflow_ir.py`, `provider_ops.py` (provider operator mappings), `cosmos_ops.py` (Cosmos mapping), `plan.py` (wave plan), `cutover.py` (cutover checklists), `connections.py` and `connections_to_resources.py` (Airflow connections to resources and variable names, no values), `airflow_truth.py`, `reconcile.py`, `convert_ir.py`, `apply_conversion.py`, `validate_defs.py`, `status.py`, `parity_incremental.py`, `parity/` (both sides and the comparer), `tests/`.
- `reference/`: `tiers.md`, `mapping.md`, `conversion.md`, `parity.md`, `factories.md`, `dynamic-mapping.md`, `partitions.md`, `compatibility.md`, `component-wishlist.md`, `airlift.md` (peer, observe, proxy; the Airflow 3 limitation), `hosted-airflow.md` (MWAA, Composer, Astro), `cutover-checklist.md`.
- `examples/`: `three_dags/` (a completed trial with equivalence rows), `s3_to_snowflake_factories/`, `dagfactory_family/`, `runtime_conversion/` (converted repo and parity harness), `migration_plan/` (a wave plan and cutover checklists), `granularity/` (task versus product output for one DAG), `airflow_release_matrix/` (18 one-construct DAG files run across Airflow releases).

## Tests

```bash
unset VIRTUAL_ENV
uv run --no-project --with pytest --with pyyaml --with dagster --with dagster-aws \
  --with dagster-snowflake --with jinja2 --with croniter --with boto3 --with requests \
  --with sqlalchemy --with dagster-k8s --with dagster-dbt --with dagster-airbyte \
  --with dagster-databricks --with dagster-fivetran --with dagster-community-components \
  --with dbt-duckdb --with duckdb --with dagster-airlift --with dagster-gcp --with dagster-slack --with dagster-ssh --with docker \
  python -m pytest -q scripts/tests
```

259 tests pass with the packages above installed (`test_convert_ir.py`, `test_inventory.py`, `test_provider_ops.py`, and new: `test_cosmos.py` 29, `test_plan.py` 7, `test_cutover.py` 8, `test_airlift.py` 3, `test_generated_tests.py` 6, `test_granularity.py` 7, `test_connections.py` 7, `test_provider_ops_more.py` 12; the suite was 180 before the Cosmos, planning, cutover and Airlift work). Tests that need a package skip when it is missing: without `dagster-dbt`, `duckdb` and `dbt-duckdb` the Cosmos tests that load and run dbt skip, and without `dagster-airlift` the Airlift tests skip. Without `dagster` and `jinja2` installed, many tests skip or fail on import, so install them. The tests do not need Airflow; the extractor (`airflow_ir.py`) and the parity harness do (`tests/fixtures_cosmos/ir.json` was extracted with Airflow 2.10.5 and astronomer-cosmos 1.15.1; `regenerate.sh` shows how).

## Limitations

- `airflow_ir.py` and `airflow_truth.py` execute DAG code. Run them only on trusted code in a sandbox.
- Operators convert if they are Python, Bash, S3, Snowflake or in the provider tables above. Others (for example Dataproc, EMR, Glue, Athena, Redshift data, Dataflow, BigQuery dataset and table operators, `GCSToGCS`) become stubs (or defer the DAG with `--caveats off`); the report names the integration or component that fits, and [reference/component-wishlist.md](reference/component-wishlist.md) lists what is missing. A provider operator with an argument the mapping cannot carry (a `response_check` function, `full_pod_spec`, ...) is a stub too, with the reason. Mapped (`.expand`) provider operators other than S3 and Snowflake are stubs.
- A DAG is converted whole or stubbed per task; the converter does not partially convert a graph silently.
- Not enforced in Dagster (reported as degraded or deferred): `depends_on_past`, `wait_for_downstream`, `max_active_tis_per_dagrun`, `pool_slots` above 1, `execution_timeout` on a Python task, `fail_stop`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`.
- `catchup=True` defers the DAG unless `--catchup ignore`, which runs from now on and does not replay history.
- Eight corpus DAGs remain deferred (consumers of runtime-partitioned assets with rollup, fan-out, product or allowed-key mappings; a timetable with a mapper per asset; some mapped-task argument shapes).
- Parity covers DAGs with no external systems. Operator-based tasks, sensors on external systems, durations, failure behavior and data volume still need a side-by-side run on the same logical window.
- Cosmos: `node_converters`, a container whose entrypoint is not dbt, `run-operation`, docs and other non-build operators are loud stubs; Watcher and airflow_async modes are converted as engine variants (documentation only, not run); Cosmos task retries become the `dagster/max_retries` run-retry tag (the dbt component has no retry-policy field; run retries must be enabled on the deployment); `test_behavior=after_all` becomes `dbt build` (tests run per model, stated as a caveat); the dbt project and its profile secrets are not copied; verified only against DuckDB and Cosmos 1.15.1 on Airflow 2.10.5 ([reference/conversion.md](reference/conversion.md)).
- `dagster-airlift` 0.29.31 reads only Airflow 2.x's `/api/v1` REST API; for an Airflow 3 source use the converter ([reference/airlift.md](reference/airlift.md)).
- Airflow's run history does not migrate.
