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

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.
