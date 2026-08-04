# Qlik Compose → Dagster orchestration
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

**Goal:** wrap Qlik Compose (data-warehouse automation) with Dagster to run workflows, react to workflow state, ingest per-workflow metrics, and auto-emit assets for every Project × Workflow × Data Mart.

**Components used:**
- `qlik_compose_resource` — shared Compose REST auth
- `qlik_compose_workflow_trigger_job` — run / stop a workflow
- `qlik_compose_workflow_status_sensor` — event-drive on workflow state
- `qlik_compose_workflow_metrics_ingestion` — per-workflow metrics DataFrame
- `qlik_compose_workspace` — StateBackedComponent — auto-emit one asset per Workflow / Data Mart

## Components used

- `qlik_compose_workflow_metrics_ingestion`
- `qlik_compose_workflow_status_sensor`
- `qlik_compose_workflow_trigger_job`
- `qlik_compose_workspace`

## Run

Mock Qlik Compose in Docker (Flask, ~130 MB), scaffolds Dagster project, wires all 5 components, materializes end-to-end. **Costs $0**.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_qlik_compose_demo.sh \
  -o setup_qlik_compose_demo.sh
bash setup_qlik_compose_demo.sh
```

## Compose vs. Replicate — same platform, different tool

- **Qlik Replicate** = CDC / replication FROM source DBs → target platforms. Use `qlik_replicate_*` components.
- **Qlik Compose** = DW modeling + workflow orchestration ON TOP OF the data Replicate lands. Use these components.

Common pairing:
- `qlik_replicate_workspace` writes source-DB CDC into your target warehouse
- `qlik_compose_workspace` runs the DW-modeling workflows on top of that

Both integrations pair naturally in a Dagster asset graph.

## Swap the mock for a real Qlik Compose

Three env vars change — everything else stays the same:

```bash
export QLIK_COMPOSE_URL="https://qlikcompose.acme.com"
export QLIK_COMPOSE_TOKEN="<api token from Compose UI>"
```

Then edit the resource `defs.yaml` to swap basic auth for `api_token_env_var: QLIK_COMPOSE_TOKEN`.

## Common patterns

### Nightly full DW rebuild

```yaml
type: dagster_community_components.QlikComposeWorkflowTriggerJobComponent
attributes:
  job_name: rebuild_finance_dw
  project: FinanceDW
  workflow: FullBuildAndPopulate
  wait_for_completion: true
  timeout_seconds: 7200
  schedule: "0 2 * * *"
  default_status: RUNNING
```

### Auto-emit every workflow + data mart

```yaml
type: dagster_community_components.QlikComposeWorkspaceComponent
attributes:
  base_url_env_var: QLIK_COMPOSE_URL
  api_token_env_var: QLIK_COMPOSE_TOKEN
  workflow_selector:
    by_pattern: ["FullBuild*", "Incremental*"]
  data_mart_selector:
    exclude_by_pattern: ["*_deprecated"]
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
```

## See also

- [qlik_replicate](qlik_replicate.md) — CDC counterpart
- `tm1.md` — IBM Planning Analytics with the same 5-component shape
