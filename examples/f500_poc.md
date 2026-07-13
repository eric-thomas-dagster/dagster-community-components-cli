# Fortune 500 Data Platform → Dagster POC

**Goal:** demonstrate that Dagster can orchestrate the full data stack of a large, brick-and-mortar Fortune 500 company — on-prem sources + cloud warehouse + BI + governance + MLOps + legacy scheduling.

**Typical stack this walkthrough addresses:**

- **On-prem BI**: IBM Cognos, Power BI on-prem, Power BI Fabric
- **On-prem data**: MinIO, Trino, dbt (on GKE)
- **On-prem processing**: PySpark jobs on Kubernetes, shell scripts on bare-metal
- **Cloud**: BigQuery, GCS, dbt Cloud
- **BI**: Looker
- **Data governance / catalog**: Collibra (via lineage export)
- **Central log platform**: Elasticsearch
- **Legacy schedulers being retired**: Prefect (MLOps), Autosys (legacy jobs), DolphinScheduler
- **Compute targets**: on-prem k8s, GKE, Cloud Run, bare-metal

---

## 10 POC Evaluation Items

### 1. Platform Setup & Infrastructure (README-only)

**Not a demo asset — this is a Dagster+ deployment question.**

- **Dagster+ Serverless** or **Dagster+ Hybrid** — the hybrid model runs the agent inside your VPC and only pushes metadata to Dagster's control plane, keeping data + code on your infrastructure. Right for regulated F500 environments.
- **Security compliance**: SOC 2 Type II, HIPAA-eligible, PII redaction on materializations, IP allowlisting, SSO/SAML/OAuth for user auth, role-based access. See [Dagster+ security docs](https://docs.dagster.io/dagster-plus/deployment/hybrid).
- **Multi-region** hybrid agents so on-prem clusters + cloud regions each host their own agent → jobs run local to the data.

### 2. Data Mesh Principles

**Dagster+ multi-code-location natively supports data mesh:**

- **Code locations** = independent Dagster projects, one per domain (e.g. `sales-domain`, `marketing-domain`, `finance-domain`). Each has its own repo, dependencies, deploy cadence, and owner team.
- **User access management**: per-code-location RBAC via Dagster+ Teams.
- **Cross-domain asset checks**: assets in one code location can declare deps on assets in another via `AssetSpec(key=..., group_name=...)` — cross-domain lineage renders automatically in the UI.
- **Domain-scoped asset groups**: use `group_name` on every asset. Asset owners set via `owners=[TeamAssetOwner("data-eng")]` or user-owners.

**Demo asset**: this walkthrough scaffolds 3 code locations (`sales/`, `marketing/`, `finance/`) with cross-domain checks.

### 3. Asset-Oriented Orchestration (SDA) — including Data Vault 2.0

**SDA vs task-scheduling head-to-head:**

Task-scheduling (Prefect / Airflow / Autosys) treats each step as an anonymous unit of work. SDA treats each step as a named data asset with typed dependencies + observability + independent materialization.

**Demo**: the `data_vault_hub_link_satellite` community component takes ONE config and emits 2–3 SDAs (hub + sat + optional link) with:
- `hash_key`, `hash_diff`, `load_date`, `record_source` system columns auto-populated
- Each layer independently materializable (rebuild only the sat if descriptive attrs changed)
- Full lineage from raw source → hub → sat → downstream marts, visible in the UI
- Individual sensors / freshness policies / retry configs per layer

```yaml
type: dagster_community_components.DataVaultHubLinkSatelliteComponent
attributes:
  entity: customer
  upstream_asset_key: raw_customers
  business_keys: [customer_id]
  satellite_columns: [name, email, phone, address, updated_at]
  record_source: "erp_customers"
```

### 4. Governance & Data Quality + BigQuery Cost Management

**Data quality — use the `asset_check` primitive:**

- `great_expectations_check` — GX suite as a Dagster asset check
- `pandera_asset_check` — schema + typed constraints
- `bigquery_table_freshness_check` — freshness policy on a BQ table
- `dagster.AssetCheckResult` inside any asset for custom logic

Failed checks block downstream materializations (`asset_check_severity: ERROR`) or just warn (`WARN`). Results plot in the UI over time — business stakeholders see quality trends per domain.

**BigQuery cost management:**

**Use Dagster+ Insights for BigQuery**, not a community component. Dagster+ auto-instruments every asset materialization that runs a BQ query — you get per-asset cost + slot-hours + bytes-processed in the Insights tab. See [Dagster+ Insights for BigQuery docs](https://docs.dagster.io/guides/observe/insights/google-bigquery).

### 5. Orchestration: dbt on BigQuery (via GKE), cross-project deps

**Deploy the Dagster+ Hybrid agent on GKE — dbt-on-BigQuery execution then happens on GKE by default.** No extra components needed for the compute target; the agent's pod scheduler places the run wherever the agent is deployed.

```yaml
# code-location A: sales/dbt/
type: dagster.dbt_assets           # official
attributes:
  manifest_path: target/manifest.json
  select: "tag:sales"
```

Cross-project dependency shape — the sales project's `dim_customer` asset shows up in the marketing project's dbt DAG via a cross-code-location `AssetSpec`:

```yaml
# code-location B: marketing/
external_assets:
  - key: [sales, dim_customer]     # references the asset from code-location A
```

Dagster+ renders the cross-code-location edge in the asset graph, so the marketing team sees where their upstream came from without either team needing to import the other's code.

### 6. Orchestration: DataIngest ETL — PySpark on Kubernetes + GKE

**Use `pyspark_pipeline` + `k8s_job_asset` (or `google_cloud_run_jobs` for GKE).**

Community components:
- `pyspark_pipeline` — declare a Spark ETL as an asset with input/output typing
- `pyspark_resource` — Spark session config (master URL, packages, jars)
- `k8s_job_asset` — run any container-packaged job as a Dagster asset on your k8s cluster

Metadata mapping capabilities — every asset's `metadata` dict is a lineage-linked structured payload. Add source system + column-level lineage + sensitive-field flags per asset, all queryable via GraphQL / OpenLineage.

### 7. Orchestration: MLOps PySpark — migrating FROM Prefect

**Prefect flows migrate to Dagster assets one-to-one:**

| Prefect concept | Dagster equivalent |
|---|---|
| `@flow` decorator | `@asset` group or `@job` |
| `@task` decorator | `@asset` or in-op logic |
| Task dependencies | Asset dependencies (declared, typed) |
| Prefect blocks | Dagster resources |
| Flow runs UI | Materialization history in Dagster UI |
| Schedules | `ScheduleDefinition` |
| Sensors | `@sensor` |
| Retries | `RetryPolicy` on the asset |

**Migration approach:**

1. Wrap each Prefect flow in a Dagster job initially (as-is)
2. Iteratively split into per-model or per-dataset assets
3. Add typed inputs/outputs → gain lineage
4. Delete Prefect once Dagster runs are green for N weeks

Community components: no Prefect observation is needed since you're leaving Prefect. If you need temporary parallel-run visibility, use Dagster+ Insights to compare cost + latency between the two orchestrators.

### 8. Orchestration: Generic k8s / GKE / Cloud Run

**Community components already cover all three:**

- `k8s_job_asset` — on-prem Kubernetes + GKE (same YAML, different context)
- `cloud_run_job_trigger_asset` / `google_cloud_run_jobs` — GCP Cloud Run
- `docker_container_asset` — any container runtime

Each returns a materialization + metadata dict, so downstream assets can consume the job's outputs directly.

### 9. Orchestration: Legacy shell scripts on bare-metal

**Use `shell_command_asset` / `shell_command_job`:**

```yaml
type: dagster_community_components.ShellCommandAssetComponent
attributes:
  asset_key: legacy_data_prep
  command: "/usr/local/bin/nightly_prep.sh --date {run_date}"
  working_dir: /data/nightly
```

Runs on the Dagster agent host (bare-metal in your case). Environment vars, working dir, stdin, retry-on-nonzero all configurable. Stdout + stderr captured to the asset's compute logs — no more grepping `/var/log`.

### 10. Operations / Developer Experience (README-only)

**Not a demo asset — evaluate these in the POC by using the platform:**

- **`dg` CLI** — `dg check defs` validates every YAML against its schema, `dg dev` launches the local UI, `dg launch --assets '*'` runs headless for CI. `uvx create-dagster@latest project <name>` scaffolds a full project in one command.
- **Dagster UI** (OSS + Dagster+) — asset graph browser, materialization history, lineage view, backfill launcher, sensor / schedule dashboard. Point-and-click **materialize / retry / partition selection**, not point-and-click component *authoring*.
- **`dagster-community-components-cli`** — `dagster-component search foo` finds components in the community registry, `dagster-component add foo` installs one into a project (drops files under `src/<pkg>/defs/`, generates the `defs.yaml`, and installs pip deps). 900+ components indexed.
- **YAML defs + schema validation** — every community component ships a `schema.json` alongside `defs.yaml`. `dg check` validates the YAML against the schema before runtime. Enum + required-field violations surface at check time, not at materialization.
- **Dagster+ Insights** — per-asset cost, latency, bytes-processed for BigQuery / Snowflake / Databricks runs, all auto-instrumented. No manual metrics wiring.
- **StateBackedComponent refresh flow** — for workspaces (Salesforce / HubSpot / Cognos / etc.), `refresh_if_dev: true` re-enumerates the vendor catalog on next `dg dev` restart; `dg utils refresh-defs-state` refreshes on demand. Discovery is cached so cold starts are fast even with hundreds of assets.

---

## Ready-to-run POC scaffold

Coming in a follow-up commit (this walkthrough is the map; the setup script is Phase 6b). For now, use it as the checklist to build up your POC project piece by piece — each POC eval item names the concrete community components + Dagster+ features to demo.

## Component coverage summary

Everything the F500 stack requires ships as either a community component (from this session) or an official Dagster integration:

| Category | Coverage |
|---|---|
| **Cognos (BI reports)** | 5-component set: `cognos_resource`, `cognos_report_run_job`, `cognos_report_status_sensor`, `cognos_report_data_ingestion`, `cognos_workspace` |
| **Power BI Fabric** | 7-component set: `fabric_workspace`, `fabric_lakehouse_resource`, etc. |
| **Power BI on-prem** | Official `dagster-powerbi` |
| **Collibra (lineage)** | `lineage_to_collibra` sink |
| **Elasticsearch** | `elasticsearch_asset`, `elasticsearch_reader`, `elasticsearch_resource` |
| **MinIO** | `minio_resource`, `minio_io_manager` |
| **Trino** | `trino_io_manager` |
| **dbt** | Official `dagster-dbt` |
| **BigQuery** | 12 components + `dagster-gcp` |
| **GCS** | Official `dagster-gcp`, `gcs_monitor` |
| **Looker** | `looker_assets` |
| **Autosys** | `autosys_asset` |
| **k8s / GKE** | `k8s_job_asset` |
| **Cloud Run** | `cloud_run_job_trigger_asset`, `google_cloud_run_jobs` |
| **PySpark** | `pyspark_pipeline`, `pyspark_resource` |
| **Shell / bare-metal** | `shell_command_asset`, `shell_command_job`, `docker_container_asset` |
| **Data Vault 2.0** | `data_vault_hub_link_satellite` |
| **Data quality** | `great_expectations_check`, `pandera_asset_check`, `freshness_check` |
| **Cost management** | Dagster+ Insights (not a component) |
| **Data mesh** | Dagster+ multi-code-location + Teams RBAC (not a component) |

## Related walkthroughs

- `warehouse_migration.md` — legacy DB → cloud warehouse
- `snowflake_workspace.md` — the same StateBackedComponent shape for Snowflake
- Per-vendor workspace walkthroughs: `qlik_replicate.md`, `tm1.md`, `qlik_compose.md`, `jde.md`, `cognos.md`
