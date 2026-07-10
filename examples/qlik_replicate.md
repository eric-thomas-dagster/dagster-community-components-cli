# Qlik Replicate → Dagster orchestration

**Goal:** wrap Qlik Replicate (control plane: Qlik Enterprise Manager) with Dagster so you can start/stop tasks, react to task-state transitions, and materialize CDC metrics as assets — without leaving the Dagster UI.

**Components used:**
- [`qlik_replicate_resource`](../../dagster-component-templates/tree/main/resources/qlik_replicate_resource) — shared EM auth
- [`qlik_replicate_task_trigger_job`](../../dagster-component-templates/tree/main/jobs/qlik_replicate_task_trigger_job) — start / stop / reload a task
- [`qlik_replicate_task_status_sensor`](../../dagster-component-templates/tree/main/sensors/qlik_replicate_task_status_sensor) — event-drive on task state
- [`qlik_replicate_task_metrics_ingestion`](../../dagster-component-templates/tree/main/assets/ingestion/qlik_replicate_task_metrics_ingestion) — per-task CDC metrics DataFrame
- [`qlik_replicate_workspace`](../../dagster-component-templates/tree/main/integrations/qlik_replicate_workspace) — **StateBackedComponent** — auto-emit one Dagster asset per task by enumerating the workspace; one YAML instead of one-per-task

## One-command demo

Spins up a mock Qlik Enterprise Manager in Docker (Flask container, ~130 MB), scaffolds a Dagster project, wires all four components, and materializes end-to-end. **Costs $0** — no Qlik license needed.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_qlik_replicate_demo.sh \
  -o setup_qlik_replicate_demo.sh
bash setup_qlik_replicate_demo.sh
```

Output ends with the URL of the mock EM (default `http://localhost:4442`) and instructions to run `dg dev`.

Once the UI is up (`http://localhost:3000`):

1. **See the four defs** — resource, trigger job, sensor, ingestion asset — in the sidebar.
2. **Materialize `qlik_task_metrics`** — click the asset → **Materialize**. The mock returns pseudo-random CDC metrics; the DataFrame shows one row per task per materialization.
3. **Trigger the reload job** — Jobs → `reload_orders_cdc` → **Launchpad** → **Launch Run**. The mock transitions the task STARTING → RUNNING → STOPPED over ~5s; the job's `wait_for_completion` loop tracks it.
4. **Toggle the sensor** — Sensors → `orders_reload_done` → **Start**. It polls every 30s; when a task hits `STOPPED`, it triggers `reload_orders_cdc` (in this demo it kicks the same job again — in production you'd point it at a downstream validation / transform job).

## Swap the mock for a real Qlik Enterprise Manager

Only three env vars change — everything else stays the same:

```bash
export QLIK_EM_URL="https://qlikem.acme.com"      # your EM base URL
export QLIK_EM_API_TOKEN="<token from EM UI>"     # Manage API Tokens → Generate
```

Then edit the resource's `defs.yaml` to switch from basic auth to API token:

```yaml
type: dagster_community_components.QlikReplicateResourceComponent
attributes:
  resource_key: qlik_replicate_resource
  base_url_env_var: QLIK_EM_URL
  api_token_env_var: QLIK_EM_API_TOKEN     # replaces username/password
```

The trigger job, sensor, and metrics ingestion all reference the resource by key — nothing else changes.

## Common production patterns

### Nightly full reload of a slow-changing target

```yaml
type: dagster_community_components.QlikReplicateTaskTriggerJobComponent
attributes:
  job_name: reload_dim_customer
  server: prod-replicate-01
  task: dim_customer_snowflake
  action: reload
  wait_for_completion: true
  poll_interval_seconds: 30
  timeout_seconds: 3600
  resource_key: qlik_replicate_resource
  schedule: "0 3 * * *"
  default_status: RUNNING
```

### Alert on-call when a critical CDC task errors

```yaml
type: dagster_community_components.QlikReplicateTaskStatusSensorComponent
attributes:
  sensor_name: orders_cdc_error_alert
  server: prod-replicate-01
  task: orders_sqlserver_to_snowflake
  target_states: [ERROR]
  job_name: page_oncall_qlik_error
  resource_key: qlik_replicate_resource
  minimum_interval_seconds: 60
```

Pair with your incident-response job (e.g. `dataframe_to_pagerduty`, `dataframe_to_slack_message`).

### Dashboard-input asset for CDC health

```yaml
type: dagster_community_components.QlikReplicateTaskMetricsIngestionComponent
attributes:
  asset_key: qlik_task_metrics
  servers: [prod-replicate-01, prod-replicate-02]
  group_name: qlik_observability
```

Then downstream:
- `dataframe_to_snowflake` → BI dashboards
- `dataframe_to_prometheus` → Grafana panels for latency SLOs

## What Qlik Replicate covers well vs. what Dagster covers well

- **Qlik Replicate**: source-DB log mining, low-latency CDC, target-DB DDL/DML apply, mainframe VSAM / SAP HANA / Oracle GoldenGate compatibility.
- **Dagster**: orchestrating multiple Replicate tasks, chaining Replicate output to downstream Snowflake transforms + dbt, alerting, replaying failed runs, observability dashboards.

Use this integration when Replicate is already deployed. If you're greenfield and don't have Replicate, [`database_replication`](../../dagster-component-templates/tree/main/assets/sources/database_replication) (Sling-backed, native Dagster) is a simpler starting point.

## Related walkthroughs

- [`warehouse_migration.md`](warehouse_migration.md) — one-time lift-and-shift with `database_*_migration` components
- [`replication.md`](replication.md) — recurring Sling-based replication as an alternative to Replicate
