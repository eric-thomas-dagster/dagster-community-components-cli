# What the components need to do (handoff to the components session)

The converter turns existing orchestrator DAGs into Dagster definitions. It owns the translation: it reads the DAG, works out what each part does, and writes generated code. This file lists what the components could still do for it, in Dagster terms. None of it is specific to one orchestrator.

Measured on 292 DAGs from 14 public example repos: 284 convert and 8 are deferred (5 of those are runtime-partition mappings). About 225 of the 284 convert with nothing stubbed; about 60 contain at least one stubbed task. The corpus is mostly teaching examples, so use the ranking, not the percentages.

## What the converter does not need from you

These are built into the converter's generated runtime, so a converted project needs no component for them: fire-and-forget launch of another job, an SLA measured from the scheduled tick, running a callable in its own virtualenv or interpreter, mapped S3 and Snowflake steps, the setup and teardown scope, and the skip-aware gates. 0.10.164 has `trigger_job_asset` `wait: false` and `sla_asset` `start_from`; the converter can emit those components instead once you want converted projects to depend on the package.

## Delivered in 0.10.164 and 0.10.165, and what is left on the converter side

Everything the earlier version of this file asked for now exists. What remains is converter work, not component work:

| Capability | Component | Converter status |
|---|---|---|
| Asset whose partition key is chosen while it runs | `runtime_partition_asset` | The converter generates the same two-asset shape itself (the step is one asset, the dynamically partitioned catalog asset is an `AssetSpec` that receives `report_runless_asset_event` per key) and verified it with real Dagster runs: composite keys added with `add_partitions`, and a key taken from a run tag. Consumers with the identity mapping convert (eager automation on the same dynamic partition set). Rollup, fan-out, product and allowed-key mappings are not converted yet (5 DAGs) |
| Repeat a step until a condition | `loop_gate_asset` | The converter handles Airflow 3.4 task-group loops itself, so it does not need the component: the group's tasks run in order inside one asset, once per iteration, with Airflow's `loop` object. The component repeats one callable, so it fits a one-task loop only |
| S3 create object | `s3_create_object_asset` | The converter translates `S3CreateObjectOperator` itself (text or bytes, `replace`); compression, encryption and ACLs are stubbed |
| Read a non-direct upstream value by name | `get_upstream_value` | Does not fit graph-form DAGs: their values are op outputs and the assets are only materialized when the graph ends. Those pulls stay stubbed |
| Fire and forget launch, SLA from the scheduled tick | `trigger_job_asset` `wait: false`, `sla_asset` `start_from` | The converter has its own implementation in the generated runtime; it can emit the components instead |
| SQL column checks | `Enhanced Data Quality Checks` | Needs the components output mode (write the component YAML from the operator's column mapping). If `distinct_check`, or tolerances on `equal_to`/`geq_to`/`leq_to`, have no counterpart there, that is the only request |

Human-in-the-loop operators inside a run (pause, wait for a person's choice, continue) are not something Dagster can do today; the converter stubs them with a caveat and asks for nothing.

## Settings that components need to expose

`retry_policy_*`, `pool` and `timeout_seconds` landed in 0.10.163. Everything else an asset needs (automation condition, partitions, metadata, owners, tags, kinds, group, freshness policy, code version) is set through `post_processing` in `defs.yaml`, which already works for every component. Nothing further is needed here.

## Low priority

- `automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`.
- Timeouts abandon the thread at the deadline and the work continues; say so in each README that offers one.

## Operators the converter calls a library for because no component fits

Checked against `dagster-community-components` 0.10.186 (each component's `schema.json` and `README.md`). The converter now translates these operators itself (see `reference/conversion.md`, "Provider operators"); a component with the fields below would let `--output components` write YAML for them instead of generated Python.

| Airflow operator | Closest component | What is missing |
|---|---|---|
| `HttpOperator`, `SimpleHttpOperator` | `http_external_asset`, `http_webhook_job`, `http_resource` | A one-request asset: method, endpoint joined to the resource's base URL, query parameters (GET) or body, headers, the status that counts as success (Airflow fails on any error status unless `check_response` is false), the response body returned as the asset's value. `http_external_asset` requires a trigger, a status poll and a run id; `http_webhook_job` is a job and returns nothing |
| `HttpSensor` | `http_poll_sensor` | A condition for `wait_until_asset` (for example `http_ok`: url, method, headers, status codes that mean "not yet", default 404). `http_poll_sensor` starts a run when a response changes; it does not hold a step open |
| `KubernetesPodOperator` | `k8s_job_asset` | `args` (separate from `command`), `labels`, `node_selector`, `tolerations`, `affinity`, `env_from` (config maps and secrets), `kubeconfig_file`, `kube_context` and `load_incluster_config` (it runs only with in-cluster config), a Job name that is unique per asset (it uses the first 32 characters of the run id, so two pod steps in one run collide), `delete_failed_k8s_jobs` and a TTL for finished Jobs |
| `SQLExecuteQueryOperator` and the database-specific SQL operators | `sql_asset` | Returning the rows of the last statement (Airflow pushes them to XCom), per-statement commit (`autocommit`), splitting a script into statements, positional (list) parameters. The component's `connection_env` already matches what the converter reads (`<CONN>_URL`) |
| `EmailOperator` | `smtp_send_asset` | A message with a fixed recipient list, subject, HTML body and attachments and no upstream DataFrame (the component sends one email per row or one summary of a DataFrame) |
| `SlackWebhookOperator` | `slack_notification_job`, `slack_message_send` | The same webhook post as a step: `slack_notification_job` is a job and `slack_message_send` needs an upstream DataFrame |
| `DbtCloudRunJobOperator` | `dbt_cloud_trigger_job` | An asset form (a step in a DAG, with `deps`) of what the job does; the job is not a step. Confirm that its resource does not switch off the dbt Cloud job's schedule: `DbtCloudClientResource` does by default (`disable_schedule_on_trigger=True`), the operator never does |
| `AirbyteTriggerSyncOperator`, `FivetranOperator` | `airbyte_sync_trigger_job`, `fivetran_sync_trigger_job` | An asset form that waits for the sync (the Fivetran job has no wait), and returns the job id |
| `DatabricksRunNowOperator`, `DatabricksSubmitRunOperator` | `databricks_asset_bundle` | A component that runs an existing job by id or submits a one-off run from a JSON body and waits for it |
| `wait_until_asset` as the target of sensors | `wait_until_asset` | `SFTPSensor`, `S3KeySensor`, `GCSObjectExistenceSensor` and the other file sensors need `sftp_file_exists`, `s3_key_exists`, `gcs_object_exists` conditions (the `*_monitor` sensors start runs; they do not hold a step). A skip on timeout that also skips what follows (`soft_fail`) |

## Operators not converted yet that have a component or library

Not "no component fits": not done in this pass. The report names the component for each; each is a loud stub today.

| Airflow operator | Where it would go |
|---|---|
| `SSHOperator` | `ssh_asset` (commands, host and credentials from environment variables). Extracting the IR needs the Airflow connection to exist, because the operator builds its hook when it is created |
| `DockerOperator` | `docker_container_asset` (image, command, env vars, network only: no mounts, `auto_remove`, `mem_limit`, TLS) |
| `BigQueryInsertJobOperator` | `dagster-gcp` `BigQueryResource` with the job configuration; the registry's `bigquery_query_asset` returns a DataFrame and has no job configuration |
| `SlackAPIPostOperator` | `dagster-slack` `SlackResource` (`chat_postMessage`) |
| `SQLColumnCheckOperator`, `SQLCheckOperator` | `enhanced_data_quality_checks` as asset checks |
| `GlueJobOperator`, `SageMakerTrainingOperator`, `RedshiftSQLOperator`, `AzureDataFactoryRunPipelineOperator`, `DataflowTemplatedJobStartOperator` | `aws_glue`, `aws_sagemaker`, `aws_redshift`, `azure_data_factory`, `google_dataflow` |

## Cosmos and dbt (checked against `dagster-community-components` 0.10.187 and `dagster-dbt` 0.29.31)

What the registry already has for dbt: `enriched_dbt_project` (a drop-in for `DbtProjectComponent`: freshness policies, exposures, contract checks, per-model configuration), `enriched_dbt_cloud_workspace`, `dbt_run_job` (a dbt command as a *job*, no asset modeling), `dbt_cloud_trigger_job`, `dbt_cloud_job_sensor`, `dbt_cloud_resource`, `dbt_state_reuse_patch`. The converter writes the official `dagster_dbt.DbtProjectComponent` for Cosmos `DbtDag` / `DbtTaskGroup` because it covers the whole mapping (select, exclude, selector, `cli_args`, `--vars`, key prefix, op name and pool, `post_processing`); `enriched_dbt_project` can replace it by changing `type:`. What is missing, in the order it costs the converter:

Not asked for, on purpose: **per-step retries** (a single `dbt build` is the better shape: `dbt retry` re-runs only the failed models, and Dagster's run retries cover the rest; see `conversion.md`) and **running dbt in a container image** (Cosmos's container modes exist because dbt's dependencies clash with Airflow's; in Dagster dbt runs in the code location's own environment).

| Gap | Why it matters | Where it belongs |
|---|---|---|
| `post_processing` `attributes.deps` **replaces** an asset's dependencies instead of adding to them (checked: a root dbt model lost its dbt parent when an upstream task was added) | A Cosmos group that follows an Airflow task needs the upstream task's asset as an extra dependency of the group's root models. The converter works around it by listing each root's dbt parents again next to the upstream key, which assumes the default dbt asset keys | `DbtProjectComponent` (upstream Dagster) and `enriched_dbt_project`: an append mode for `deps` (or a field such as `extra_deps` per node selection) |
| An asset-with-dependencies form of "run one dbt command" (`dbt run --select X`, `seed`, `snapshot`, `test`) that streams dbt's logs and fails the step on a non-zero exit | Cosmos's per-task operators (`DbtRunLocalOperator` and the others) are one task each with no node list; the converter generates a `DbtCliResource` call for each. `dbt_run_job` is a job, not a step with `deps` | a registry component with `deps`, `command`, `select`, `exclude`, `vars`, `full_refresh`, profile fields |
| Cosmos `profile_mapping` classes (`SnowflakePasswordProfileMapping` and the rest) as profile generators | The converter writes an `env_var()` based `profiles.yml` from the mapping's field names; a maintained mapping table would remove the "check the field names" caveat | a small library or component: connection type to dbt profile fields, read from the environment |
| Airlift: `dagster_airlift` 0.29.31's REST client is hard-coded to `/api/v1` | Airflow 3 serves `/api/v2`, so peer and observe cannot read an Airflow 3 instance (see `airlift.md`) | upstream `dagster-airlift`; the registry's `composer_airflow_auth_backend` cannot fix it (it supplies authentication only) |

## Operators the registry does not cover (checked against `dagster-community-components` 0.10.188, ranked by measured use)

The ranking is the number of Apache Airflow provider system-test DAG files that use each operator (334 files; method and caveats in `conversion.md`, "More provider operators"): provider test DAGs, so read it as the operator surface rather than an estate. "Searched" means the 0.10.188 manifest was searched by id, name, description and tags; "none found" means no hit. Tiers are the manifest's `validation.level`.

| Rank | Operators (files) | Converter today | Registry (0.10.188) | What would help |
|---|---|---|---|---|
| 1 | `GCSCreateBucketOperator`, `GCSDeleteBucketOperator` (88, 87); `S3CreateBucketOperator`, `S3DeleteBucketOperator` (50, 50) | Python on `GCSResource` and boto3 (new) | none found for bucket create or delete (`gcs_cleanup_job` and `s3_delete_objects_asset` delete objects) | `gcs_bucket_asset` / `s3_bucket_asset` (create if missing, delete with force) as assets with `deps` |
| 2 | `BigQueryCreateEmptyDatasetOperator`, `BigQueryDeleteDatasetOperator` (29, 29), `BigQueryCreateTableOperator` (24) | stub | none found for dataset create or delete; `bigquery_create_table_from_query_asset` creates a table from a query, not an empty table with a schema | `bigquery_dataset_asset`, `bigquery_table_asset` (schema, partitioning) |
| 3 | `DataprocCreateClusterOperator`, `DataprocDeleteClusterOperator` (23, 23), `DataprocSubmitJobOperator` (16), `DataprocCreateBatchOperator` (3) | stub | none found (searched "dataproc") | `dataproc_job_asset` (submit and wait); cluster create and delete as one managed-cluster component |
| 4 | `GCSToGCSOperator` (15), `GCSSynchronizeBucketsOperator` (11), `GCSToBigQueryOperator` (5) | stub | none found for GCS-to-GCS copy; `bigquery_load_from_gcs_asset` (tier `live`) fits `GCSToBigQueryOperator` and was not wired | `gcs_copy_asset`; wire `bigquery_load_from_gcs_asset` |
| 5 | Compute Engine (10, 10), GKE (7, 7, 3), EKS (6, 6, 5, 6, 4), RDS (6, 6), Cloud SQL (5, 5, 5, 4) | stub | none found (searched "compute engine", "gke", "eks", "cloud sql") | infrastructure lifecycle components are out of scope for most migrations: convert by hand with the cloud's SDK or Terraform |
| 6 | `RedshiftDataOperator` (4), `AthenaOperator` (3) | stub | `redshift_resource`, `aws_redshift` (tier `code`); Athena has only `athena_io_manager`; no run-SQL-and-wait asset for either | `athena_query_asset`, a Redshift Data API query asset |
| 7 | `BeamRunPythonPipelineOperator` (4), Dataflow job operators and sensors (4, 3, 3, 3) | stub | `google_dataflow` (tier `code`, an integration; not tried against these arguments) | check `google_dataflow` against `DataflowTemplatedJobStartOperator`; a Beam runner component |
| 8 | Pub/Sub create and delete topic and subscription (5, 5, 3, 3) | stub | `pubsub_publish_asset` (`live`), `external_pubsub_asset` (`live`), `pubsub_monitor`: publish and observe, not create and delete | topic and subscription lifecycle assets |
| 9 | `BigQueryValueCheckOperator` (3), `SQLColumnCheckOperator` | stub | `bigquery_dry_run_check` (`live`) is a dry-run check, not a value check; `enhanced_data_quality_checks` for column checks (not tried) | a value-check asset check taking a query and a tolerance |
| not in the corpus above | `EmrAddStepsOperator` and the other EMR operators, Discord webhook, `SparkSubmitOperator` with a cluster connection | EMR, Discord: stub; Spark: `spark-submit` subprocess (new) | none found for EMR or Discord; for Spark only `spark_livy_batch_asset` and `spark_k8s_operator_asset` (tier `code`), which are Livy and Kubernetes, not `spark-submit` | an EMR step component; a `spark_submit_asset` that builds the command line the Spark hook builds |

Registry components that could replace code the converter now generates: `lambda_invoke_asset`, `ssh_asset`, `docker_container_asset` and `bigquery_query_asset` are emitted in `--output components` when the operator's arguments fit (see `conversion.md`). Gaps in them that stop more operators from using them: `docker_container_asset` has no `user`, `working_dir`, `entrypoint`, mounts or auto-remove; `ssh_asset` has no `environment`; `lambda_invoke_asset` requires `region_name` (an Airflow operator usually leaves it to the connection); `bigquery_query_asset` has no destination table or write disposition. Slack API post has no registry component that returns a value from one message (`msteams_message_send` and the Slack components in the registry are agents, sensors and DataFrame senders).

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.

## Found while testing registry components through the rewrite gates

- `dagster-component add <id>` asks `Continue? [Y/n]` before copying files and has no `--yes` flag (`--auto-install` only covers the pip requirements), so a non-interactive worker or script gets `Aborted!`. A `--yes` / `--no-input` flag would let agents and CI use it without `yes |`.
- `shell_command_asset` prints `$ <command>` to standard output before running the command. That is fine, but it is an extra printed line next to the original's output: the harness's printed-output comparison tolerates extra lines, so it costs nothing here, and it is noted so nobody removes the output lines that matter.
- `shell_command_asset` has no job or schedule of its own, so a component-style rewrite needs a small `.py` file next to the `defs.yaml` for `define_asset_job` and `ScheduleDefinition`. A `job_name` / `cron_schedule` attribute (as some other components have) would make a one-component DAG a one-file project.
