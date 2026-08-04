# JD Edwards Orchestrator → Dagster orchestration
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

**Goal:** wrap **JDE Orchestrator** with Dagster to schedule orchestrations, react to completion events, ingest orchestration output, and auto-emit assets for every orchestration.

**Components used:** `jde_orchestrator_resource`, `jde_orchestration_trigger_job`, `jde_orchestration_status_sensor`, `jde_orchestration_output_ingestion`, `jde_orchestrator_workspace`.

## Components used

- `jde_orchestration_output_ingestion`
- `jde_orchestration_status_sensor`
- `jde_orchestration_trigger_job`
- `jde_orchestrator_workspace`

## Run

Mock JDE AIS server in Docker (Flask, ~130 MB), scaffolds Dagster project, wires all 5 components, materializes end-to-end. **Costs $0**.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_jde_demo.sh \
  -o setup_jde_demo.sh
bash setup_jde_demo.sh
```

## Sync vs async

- **Sync** — POST returns when orchestration completes. Right for short orchestrations (< 30s).
- **Async** — POST returns immediately with a `jobId`; component polls `/status/{jobId}`. Right for long orchestrations. Set `async_mode: true`.

## Swap the mock for a real JDE AIS

Three env vars change:

```bash
export JDE_AIS_URL="https://ais.acme.com"
export JDE_USER="your_jde_user"
export JDE_PASSWORD="your_jde_pass"
```

For older JDE Tools (pre-9.2.7) set `api_path_prefix: /jderest/v2/orchestrator` on the resource.

## Common patterns

### Nightly financial recon + downstream dbt

```yaml
type: dagster_community_components.JDEOrchestrationTriggerJobComponent
attributes:
  job_name: nightly_ar_recon
  orchestration: JDE_AR_Recon
  inputs:
    AsOfDate: "2026-07-10"
    Currency: USD
  async_mode: true
  wait_for_completion: true
  timeout_seconds: 3600
  schedule: "0 3 * * *"
```

Pair with a `jde_orchestration_status_sensor` on `SUCCESS` → triggers dbt refresh.

### Read JDE table via a Data Service orchestration

```yaml
type: dagster_community_components.JDEOrchestrationOutputIngestionComponent
attributes:
  asset_key: open_ap_invoices
  orchestration: JDE_Fetch_Open_APs
  inputs:
    CompanyCode: "00001"
  output_field: "ServiceRequest1.RowSet"
```

Chain `dataframe_to_snowflake` downstream to land JDE data in your warehouse.

### Auto-emit every orchestration

```yaml
type: dagster_community_components.JDEOrchestratorWorkspaceComponent
attributes:
  base_url_env_var: JDE_AIS_URL
  username_env_var: JDE_USER
  password_env_var: JDE_PASSWORD
  orchestration_selector:
    by_pattern: ["JDE_*"]
    exclude_by_pattern: ["*_deprecated"]
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
```

## JDE vs Oracle EBS vs Fusion Apps

- **JDE (JD Edwards EnterpriseOne)** = Oracle's ERP for mid-market manufacturing / distribution / real estate. This integration.
- **Oracle EBS (E-Business Suite)** = separate Oracle ERP for large enterprises. Different REST surface.
- **Oracle Fusion Apps** = Oracle's cloud-native ERP. Different REST surface.

Use these components when JDE is your ERP and Orchestrator is deployed.

## See also

- [qlik_replicate](qlik_replicate.md), [qlik_compose](qlik_compose.md), [tm1](tm1.md) — same 5-component shape for other enterprise systems
