# IBM Cognos Analytics → Dagster orchestration
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

**Goal:** wrap **Cognos Analytics** (BI reports + dashboards) with Dagster to schedule report runs, react to report completion, ingest report output as DataFrames, and auto-emit assets for every report.

**Components used:** `cognos_resource`, `cognos_report_run_job`, `cognos_report_status_sensor`, `cognos_report_data_ingestion`, `cognos_workspace`.

## Components used

- `cognos_report_data_ingestion`
- `cognos_report_run_job`
- `cognos_report_status_sensor`
- `cognos_workspace`

## Run

Mock Cognos REST server in Docker (Flask, ~130 MB), scaffolds Dagster project, wires all 5 components, materializes end-to-end. **Costs $0**.

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cognos_demo.sh \
  -o setup_cognos_demo.sh
bash setup_cognos_demo.sh
```

## Cognos Analytics vs. TM1

- **Cognos Analytics** = BI reports + dashboards. This integration.
- **TM1 / Planning Analytics** = planning cubes with writeback. Separate integration (`tm1_*` components).

Both live in IBM's analytics stack — separate REST APIs, separate integrations. Common pairing: TM1 for planning → Cognos for consumption dashboards → Dagster orchestrates both.

## Auth: session + namespace

Cognos auth requires a security namespace name (LDAP / CognosEx / custom) — that's what maps your creds to the right Cognos user store. Set it via `namespace_env_var` on `cognos_resource`.

## Common patterns

### Nightly financial report

```yaml
type: dagster_community_components.CognosReportRunJobComponent
attributes:
  job_name: nightly_pnl
  report_id: iABC001
  output_format: PDF
  parameters:
    Month: "2026-07"
  resource_key: cognos_resource
  schedule: "0 6 * * *"
```

### Bring a Cognos report into your warehouse

```yaml
type: dagster_community_components.CognosReportDataIngestionComponent
attributes:
  asset_key: monthly_pnl_report
  report_id: iABC001
  output_format: CSV
  parameters:
    Month: "2026-07"
  resource_key: cognos_resource
```

Chain `dataframe_to_snowflake` downstream.

### Auto-emit every report as a Dagster asset

```yaml
type: dagster_community_components.CognosWorkspaceComponent
attributes:
  base_url_env_var: COGNOS_URL
  username_env_var: COGNOS_USER
  password_env_var: COGNOS_PASSWORD
  namespace_env_var: COGNOS_NAMESPACE
  folder_ids: ["/content/folder[@name='Finance']"]
  report_selector:
    by_pattern: ["Monthly*", "Quarterly*"]
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: true
```

## See also

- [qlik_replicate](qlik_replicate.md), [qlik_compose](qlik_compose.md), [tm1](tm1.md), [jde](jde.md) — same 5-component shape for other enterprise systems

## Prospect coverage (this session's arc)

With this integration shipped, the full prospect stack is now covered:

| Prospect tech | Integration |
|---|---|
| SQL Server | `mssql_resource` + `database_query` + `database_replication` |
| SSIS | `database_query` calling `EXEC msdb.dbo.sp_start_job` |
| Stored Procedures | `snowflake_stored_procedure_call_asset` / `database_query` `EXEC` |
| **Qlik Replicate** | 5-component `qlik_replicate_*` set |
| **Qlik Compose** | 5-component `qlik_compose_*` set |
| **IBM Planning (TM1)** | 5-component `tm1_*` set |
| **Cognos (SaaS)** | 5-component `cognos_*` set (this integration) |
| Power BI | Official `dagster-powerbi` |
| SFTP | `sftp_resource` + `sftp_monitor` + `sftp_to_database_asset` |
| MS Fabric | 7-component `fabric_*` set |
| **JDE** | 5-component `jde_*` set |
| ServiceNow | 3-component `servicenow_*` set |
| Oracle EBS | `oracle_resource` + `database_query` |
