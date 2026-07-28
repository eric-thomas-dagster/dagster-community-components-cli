# Fortune 500 Data Platform → Dagster POC

> **Heads up — this is a big one.** The runnable demo brings up **6 containers** (Postgres, MinIO, Trino, Elasticsearch, Kibana, OpenTelemetry Collector) and scaffolds **3 real Dagster code-locations** with cross-code-location asset deps. First-run pulls ~2 GB of images and takes ~10 min. Subsequent runs are ~2 min. If you want to preview the pieces before running the whole thing, skim the [Architecture](#architecture) section first.

**Goal:** demonstrate that Dagster can orchestrate the full data stack of a large, brick-and-mortar Fortune 500 company — on-prem sources + cloud warehouse + BI + governance + MLOps + legacy scheduling.

**Typical stack shape this walkthrough addresses** — specific vendor names below are *representative examples* of the common F500 pattern; swap in your actual stack:

- **On-prem BI** *(e.g. IBM Cognos, Power BI on-prem, Power BI Fabric)*
- **On-prem data + query engine** *(e.g. MinIO S3-compatible object store, Trino / Presto for federated SQL, dbt for transformations)*
- **On-prem processing** *(e.g. PySpark on Kubernetes, shell scripts on bare-metal)*
- **Cloud warehouse + storage** *(e.g. BigQuery + GCS, Snowflake + S3, Databricks + ADLS)*
- **Cloud BI** *(e.g. Looker, Power BI Fabric, Tableau Cloud)*
- **Data governance / catalog** *(e.g. Collibra, Alation, Atlan — Dagster pushes lineage out)*
- **Central log platform** *(e.g. Elasticsearch, Splunk, Datadog)*
- **Three legacy schedulers being retired** — see next section (centralized batch + per-domain DAG + Python-flow tool)
- **Compute targets**: on-prem k8s, cloud k8s (GKE / EKS / AKS), serverless containers, bare-metal

---

## The three-scheduler problem Dagster replaces

The typical F500 stack carries **three schedulers** with distinct pain, each solving a different piece and none of them talking to the others:

- **A centralized legacy job-scheduler** (representative: AutoSys / Control-M / Tivoli Workload Scheduler) — bare-metal / mixed workloads. Dependencies are **scheduling-based, not data-aware**: "app A finished → *assume* the table is filled → run app B." No lineage, no asset guarantees, no way to answer "why did today's number look weird?"
- **A per-domain DAG scheduler** (representative: DolphinScheduler / Airflow-per-team / vendor-specific tools) — YAML-defined DAGs scoped to a single domain project, but **no cross-project asset linking**. Each domain becomes an island; cross-domain deps are duct-taped with time-based waits.
- **A Python-flow tool for MLOps / data-science** (representative: vanilla Airflow / bespoke Python) — often deployed per-team and hitting scale ceilings. Per-team instances can't interconnect, so ML features derived from central marts either duplicate the upstream logic or fall out of sync.

Dagster+ replaces all three with one asset graph, one control plane, one alert path. What replaces what depends on the specific tools in play — the community components cover the common shapes:

| Pattern in the legacy stack | Dagster shape |
|---|---|
| Time-based "next job" dependency | Asset dependency (declared, typed, data-aware) |
| Per-domain YAML DAG | Per-code-location `defs.yaml` + cross-loc `AssetSpec` |
| Python-flow @task | `@asset` or in-op logic |
| Bare-metal / shell-orchestrated batch | `shell_command_asset`, `docker_container_asset` |
| K8s Spark submission | `spark_k8s_operator_asset`, `k8s_job_asset` |
| MLOps pipelines | `pyspark_pipeline`, `jupyter_notebook_asset`, `mlflow_*` |

The team's ops toil around "which of the 3 UIs do I check?" collapses to one. Dagster+ ships bridge components (e.g. `autosys_asset`) for mid-migration parallel-run periods so you don't have to cut over all workloads simultaneously.

## Cross-cloud, cross-domain, cross-project chain

The POC needs to demo a single lineage chain that crosses ownership boundaries: `on-prem MinIO → Trino federated read → domain A dbt on GKE → BigQuery mart → domain B PySpark ML feature → Looker`. Three properties fall out of Dagster+ multi-code-location:

1. **Every hop is one asset** — no glue scripts. Each hop's compute target (on-prem k8s / GKE / Cloud Run / BQ query) is a per-asset config, not a per-orchestrator boundary.
2. **The cross-loc edge is declarative** — `AssetSpec(key=["sales", "dim_customer"])` in domain B references domain A's asset. Dagster+ renders the edge without either team importing the other's code.
3. **Failures in the upstream domain surface in the downstream domain** — the marketing team's on-call sees "upstream `sales/dim_customer` failed" before they even realize their run started.

## Agent topology decision — one per cluster vs one per domain

For a 56-node k8s cluster hosting 16 domains, two patterns:

| Pattern | Agent count | Deploy cadence | Right when |
|---|---|---|---|
| **One agent per cluster** | 1 on-prem + 1 GKE + 1 bare-metal + 1 Cloud Run = 4 | Central platform team owns agent config | Small platform team, uniform runtime deps |
| **One agent per domain** | Up to 16 | Each domain team owns their agent | Domains have divergent runtimes (custom Docker images, GPU nodes, region-locality) |

**Recommendation for the POC**: start with **one agent per cluster** (4 total). Split per-domain only if a specific domain hits agent-level conflicts. Domain isolation for the *asset graph* is already handled by code-locations — the agent boundary is a runtime concern, not a governance one.

## RBAC — 4 practical roles

Dagster+ Teams supports arbitrary role granularity. For the F500 POC, the 4 roles that actually matter:

| Role | Can materialize? | Can edit code? | Can view all domains? | Can launch backfills? |
|---|---|---|---|---|
| **Platform admin** | ✅ all | ✅ all | ✅ | ✅ |
| **Domain engineer** | ✅ own domain | ✅ own domain repo | ✅ (read-only cross-domain) | ✅ own domain |
| **Business analyst** | ❌ | ❌ | ✅ (read-only) | ❌ |
| **On-call responder** | ✅ retry only | ❌ | ✅ | ❌ |

Set via code-location-scoped Teams: `sales-team` has `Editor` on `sales/`, `Viewer` on everything else. Cross-domain reads work because the UI is unified; cross-domain writes are blocked because the team scope only extends to their own code-location.

## Self-service project creation — the two-PR pattern

Once the platform is stood up, adding a new domain shouldn't require a platform-team ticket. The pattern:

1. **PR #1 — new repo** — domain team runs `uvx create-dagster@latest project <domain-name>` locally, adds their `defs.yaml` files with community components, opens a PR against a template repo.
2. **PR #2 — register the code-location** — platform team merges a one-line entry into the Dagster+ workspace config pointing at the new repo. Cross-loc AssetSpec references become live once both PRs are merged.

Total platform-team touch: ~5 minutes reviewing PR #2. The domain team is autonomous for their own asset graph.

## Failure behavior at scale

At 56 nodes × 16 domains × three integration types (Spark / dbt / Python), failures happen constantly. What matters is:

- **Isolation** — a failing sales-domain asset doesn't block marketing-domain materializations. Dagster's asset graph is dep-driven, not run-driven; unrelated assets keep flowing.
- **Automatic retry** — `RetryPolicy(max_retries=3, delay=30)` on every ingestion asset absorbs transient network/vendor blips without paging anyone.
- **Blast-radius asset checks** — a check on a shared dim table (`check dim_customer.pk_unique`) blocks downstream materializations across ALL domains that depend on it. One check, many domains protected.
- **On-call routing via Dagster+ alerts** — email / Slack / PagerDuty per code-location. Sales-domain failures page sales-eng on-call, not the platform team.

## Data quality tracking + alerting

Every `@asset` can attach `@asset_check`s. Results plot over time in the UI — quality trends per domain are visible to business stakeholders without a separate BI dashboard. For the F500 POC:

- **Schema drift** — `pandera_asset_check` on every ingestion asset; fails if a column type changes upstream.
- **Row-count sanity** — `row_count_within` bounds; fails if a batch is 3σ outside historical mean.
- **Freshness** — `bigquery_table_freshness_check` on every mart; fails if the underlying table hasn't updated in N hours.
- **Cross-domain gates** — see item 2 (data mesh) — a finance asset can block on a sales asset's freshness check.

Failed checks → Dagster+ alert → PagerDuty. No separate GreatExpectations deployment needed; the check runs inside the same asset materialization.

## Cost visibility

**Dagster+ Insights for BigQuery** — see item 4. Per-asset slot-hours, bytes-processed, and dollar cost, auto-instrumented on every BQ materialization. No manual metrics wiring, no separate BQ billing export pipeline. See [Dagster+ Insights for BigQuery](https://docs.dagster.io/guides/observe/insights/google-bigquery).

Also covered: Snowflake credits, Databricks DBUs, Fivetran MAR. All native to Dagster+, not community components.

## BI layer — how each tool appears in the asset graph

Different BI tools plug in through different components. For the typical F500 stack shape:

| BI tool | Component to use | Why this one |
|---|---|---|
| **Looker** | Official [`dagster-looker`](https://docs.dagster.io/integrations/libraries/looker/dagster-looker) — a **workspace** (single component, one config, auto-emits an asset per LookML view / explore / dashboard) | Officially maintained by Dagster Labs; keeps LookML lineage in sync with the actual Looker instance. |
| **Power BI Fabric** | Community `fabric_workspace` (7-component set — lakehouse, warehouse, pipeline, notebook, semantic model, report, dashboard) | Workspace-shape: one config picks up everything in the tenant / capacity. |
| **Power BI on-prem** | Official [`dagster-powerbi`](https://docs.dagster.io/integrations/libraries/powerbi) | Officially maintained; connects to a Power BI Report Server. |
| **IBM Cognos** | Community `cognos_workspace` + 4 sibling components (`cognos_resource`, `cognos_report_run_job`, `cognos_report_status_sensor`, `cognos_report_data_ingestion`) | No official Cognos integration; the community set handles both refresh-orchestration and data-ingest-from-Cognos-reports. |
| **Tableau** | Official [`dagster-tableau`](https://docs.dagster.io/integrations/libraries/tableau) | Officially maintained; workbook / worksheet / dashboard assets. |

The lineage chain the POC demos looks like this:

```
raw_orders  →  orders_hub / sat  →  dbt marts (BigQuery)  →  LookML views (dagster-looker)
                                                          →  Fabric semantic model (fabric_workspace)
                                                          →  Cognos report (cognos_workspace)
```

Every downstream BI asset becomes a first-class node in the Dagster graph. When a Looker view or a Cognos report is broken, the alert points at the upstream mart that changed shape — one asset graph, one on-call surface.

**Lineage-out to Collibra** — separately, the `lineage_to_collibra` sink pushes the full Dagster asset graph (marts + BI + everything upstream) to Collibra on every materialization run. Not a BI tool itself, but the governance surface for the BI layer.

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

Task-scheduling (Airflow / Autosys / legacy job schedulers) treats each step as an anonymous unit of work. SDA treats each step as a named data asset with typed dependencies + observability + independent materialization.

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

### 7. Orchestration: MLOps PySpark — migrating from a Python-flow tool

For teams migrating off a Python-flow orchestrator (vanilla Airflow, bespoke Python) the concept mapping is one-to-one:

| Python-flow concept | Dagster equivalent |
|---|---|
| `@flow` / DAG-level decorator | `@asset` group or `@job` |
| `@task` decorator | `@asset` or in-op logic |
| Task dependencies | Asset dependencies (declared, typed) |
| Named connection blocks / hooks | Dagster resources |
| Flow-runs UI | Materialization history in Dagster UI |
| Time-based schedules | `ScheduleDefinition` |
| File / message triggers | `@sensor` |
| Retry decorators | `RetryPolicy` on the asset |

**Migration approach:**

1. Wrap each legacy flow in a Dagster job initially (as-is)
2. Iteratively split into per-model or per-dataset assets
3. Add typed inputs/outputs → gain lineage
4. Delete the legacy orchestrator once Dagster runs are green for N weeks

No observation component for the retiring orchestrator is needed since you're leaving it. If you need temporary parallel-run visibility, use Dagster+ Insights to compare cost + latency between the two orchestrators during the cutover window.

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

## Architecture

```
─── SOURCES ─────────────────────────────────────────────────────────────────

┌─────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌──────────┐  ┌────────┐
│  MinIO  │  │  Trino  │  │  MSSQL / │  │ Cognos  │  │ BigQuery │  │  GCS   │
│ (S3-API)│  │  (SQL)  │  │  Oracle  │  │ reports │  │ (cloud)  │  │ (cloud)│
└────┬────┘  └────┬────┘  └────┬─────┘  └────┬────┘  └────┬─────┘  └────┬───┘
     │            │            │             │             │             │
     └────────────┴────────────┴─────────────┴─────────────┴─────────────┘
                                        │
                                        ▼
─── INGESTION (community components) ────────────────────────────────────────

     s3_to_database_asset    trino_io_manager    mssql_ingestion
     minio_resource          cognos_workspace    bigquery_query_asset
                                                 gcs_monitor

                                        │
                                        ▼
─── ORCHESTRATION (Dagster+ Hybrid — agents where the data is) ─────────────

┌───────────────────────────────────────────────────────────────────────────┐
│  agent: on-prem k8s        agent: GKE                agent: bare-metal    │
│  (Spark ETL, dbt)          (dbt on BigQuery,          (shell_command,     │
│                             MLOps PySpark)             legacy jobs)       │
└─────────────┬───────────────────────┬───────────────────────┬─────────────┘
              │                       │                       │
              ▼                       ▼                       ▼
    ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
    │  sales/          │    │  marketing/      │    │  finance/        │
    │  code-location   │    │  code-location   │    │  code-location   │
    │  (dbt + DV2.0)   │    │  (dbt + Looker)  │    │  (Cognos + SOX)  │
    └────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘
             │                       │                       │
             └───────────────────────┼───────────────────────┘
                                     │  (cross-code-location AssetSpec)
                                     ▼
                        ┌────────────────────────┐
                        │  Data Vault 2.0 layer  │
                        │  raw ─▶ hub / link /   │
                        │  sat (SDA per layer)   │
                        └───────────┬────────────┘
                                    ▼
                        ┌────────────────────────┐
                        │  dbt marts on BigQuery │
                        │  + PySpark ML features │
                        └───────────┬────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌──────────────────┐    ┌──────────────────┐         ┌──────────────────┐
│  Cognos + Power  │    │     Looker       │         │  Power BI Fabric │
│  BI (on-prem)    │    │  (LookML views)  │         │  (cloud)         │
└──────────────────┘    └──────────────────┘         └──────────────────┘


─── GOVERNANCE + OBSERVABILITY (auto-instrumented, every asset) ────────────

┌──────────────────┐    ┌──────────────────┐    ┌──────────────────────┐
│    Collibra      │    │  Elasticsearch   │    │  Dagster+ Insights   │
│  (lineage_to_    │    │  (compute logs   │    │  (BigQuery cost,     │
│   collibra sink) │    │   via OTLP CLM)  │    │   slot-hours,        │
│                  │    │                  │    │   asset SLAs)        │
└──────────────────┘    └──────────────────┘    └──────────────────────┘
```

## Data mesh — three code-locations with cross-domain deps

```
    ┌──────────────────────────────────────────────────────────────────┐
    │                     Dagster+ Control Plane                        │
    │           (unified asset graph across all three domains)          │
    └──────────────────────────────────────────────────────────────────┘
        │                          │                            │
   ─────┼──────────────────────────┼────────────────────────────┼──────
        ▼                          ▼                            ▼
   ┌───────────┐              ┌───────────┐               ┌───────────┐
   │  sales/   │              │ marketing/│               │  finance/ │
   │           │              │           │               │           │
   │ owner:    │              │ owner:    │               │ owner:    │
   │  sales-eng│              │  mktg-eng │               │  fin-eng  │
   │           │              │           │               │           │
   │  raw_     │              │  raw_     │               │  raw_gl   │
   │  orders   │              │  campaigns│               │           │
   │    │      │              │    │      │               │    │      │
   │    ▼      │              │    ▼      │               │    ▼      │
   │  customer_│──[cross-loc]─▶ campaign_ │               │  gl_close │
   │  hub      │              │  attrib   │               │           │
   │  customer_│              │           │               │           │
   │  sat      │              │           │               │           │
   │    │      │              │    │      │               │    │      │
   │    ▼      │              │    ▼      │               │    ▼      │
   │ orders_   │──[cross-loc]─▶ mkt_      │──[cross-loc]─▶│ p&l_      │
   │ mart      │              │ effective │               │ statement │
   │           │              │ ness      │               │           │
   └───────────┘              └───────────┘               └───────────┘

    Cross-loc edges = AssetSpec(key=...) references across code locations.
    Rendered in the Dagster+ UI as a unified graph; each team owns its
    own code + deploy cadence but sees upstream/downstream automatically.

    Asset checks that span domains:
      finance/p&l_statement:
        checks:
          - freshness:                     sales/orders_mart < 3h
          - row_count_within:              marketing/mkt_effectiveness ± 5%
      → cross-domain quality gate, ONE alert if either upstream drifts
```

## Ready-to-run POC scaffold — local Docker

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_f500_poc_local_demo.sh \
  -o setup_f500_poc_local_demo.sh
bash setup_f500_poc_local_demo.sh
```

**Pre-reqs:** Docker (running) + `uv` (`https://docs.astral.sh/uv/`). No cloud credentials needed. Cost: $0. First run: ~10 min (image pulls + venv installs). Subsequent runs: ~2 min.

### What the script builds

| Layer | Piece | Container / mechanism |
|---|---|---|
| **Legacy source DBs** | Postgres 16 with sales / marketing / finance schemas | `f500-postgres` (5432) |
| **Landing zone** | MinIO S3-compatible object store; Dagster's default IO manager writes Delta tables here | `f500-minio` (9000 API / 9001 console) |
| **SQL engine** | Trino coordinator with a Postgres catalog for federated reads | `f500-trino` (8080) |
| **Warehouse** | DuckDB — file-based, `warehouse/f500.duckdb` at the workspace root | in-process, no container |
| **Central log platform** | Elasticsearch + Kibana; Dagster compute logs land in a `dagster-compute-logs` data stream | `f500-elasticsearch` (9200) + `f500-kibana` (5601) |
| **Log pipeline** | OpenTelemetry Collector — receives OTLP/HTTP from Dagster, exports to ES | `f500-otel-collector` (4318) |
| **Dagster** | 3 code-locations (`sales/`, `marketing/`, `finance/`) under one `dg.toml` workspace | `dg dev` on host |

### Dagster asset graph the demo builds

- **`sales/`** code-location — Postgres → `raw_customers` / `raw_orders` → `DataVaultHubLinkSatelliteComponent` emits hub / link / sat (SDA per layer) → `DuckDBTableWriterComponent` writes `sales_dim_customer` to the warehouse.
- **`marketing/`** code-location — Postgres → `raw_campaigns` / `raw_touches` → `campaign_attribution` (declares **cross-code-location dep on `sales/dv2/customer_hub`**) → DuckDB warehouse.
- **`finance/`** code-location — Postgres → `raw_gl_entries`; separately, `TrinoQueryComponent` runs a **federated query across `postgres.finance` + `postgres.sales`** and produces `federated_pnl` → DuckDB warehouse. Also: `FreshnessPolicyComponent` YAML that blocks on `sales/dv2/customer_sat` freshness (cross-domain quality gate — 100% components, no custom Python), `ShellCommandAssetComponent` for legacy bare-metal jobs, `K8sJobAssetComponent` stub (validates via `dg check`; execution requires a real cluster).
- **Compute logs** — `OtlpComputeLogManager` in `dagster.yaml` ships every op's stdout/stderr as OTLP LogRecords to the OTel Collector → Elasticsearch. Browse in Kibana at http://localhost:5601 (data view `dagster-compute-logs*`).

### Verification the script runs at the end

- `dg check defs` — all 3 code-locations pass schema + component validation.
- Per-project `dg launch --assets '*'` — materializes the entire graph, respecting cross-loc deps.
- ES record count — the script curls `/_count` on the compute-logs data stream and prints the number of records that landed. Expect ~100-200 records on a fresh full run.

### After the script finishes

Run `uv run dg dev` from `deployments/local/` inside the workspace to open the Dagster UI at http://localhost:3000. All 3 code-locations show side by side; browse the cross-code-location lineage from `marketing/campaign_attribution` → `sales/dv2/customer_hub`.

### Swapping to GCP (BigQuery / GCS / Dagster+ Hybrid)

Once the local demo is running, the swap to GCP is a **component substitution**, not a rewrite:

| Local component | GCP component |
|---|---|
| `DatabaseQueryComponent(database_url=postgres://...)` | `BigQueryQueryComponent` — official `dagster-bigquery` |
| `DuckDBTableWriterComponent` (file-based) | `dataframe_to_bigquery_table` or `dbt_assets` with `dagster-dbt` targeting BigQuery |
| `MinIOIOManagerComponent` | `s3_parquet_io_manager` re-pointed at GCS via `gs://` endpoint, or the `dagster-gcp` GCS IO manager |
| `TrinoQueryComponent` (against local Postgres) | Same component, pointed at a Trino instance federating BigQuery + GCS |
| Elasticsearch container | GCP Logging (leave `OtlpComputeLogManager` as-is; swap the OTel Collector's exporter from `elasticsearch` to `googlecloud`) |
| `ShellCommandAssetComponent` | Same — runs wherever the Dagster+ Hybrid agent is deployed |
| `K8sJobAssetComponent` stub | Same YAML — becomes real when a `dagster-k8s` agent is deployed on GKE |

The **3-code-location + cross-loc `AssetSpec` + cross-domain freshness check** patterns don't change — those are Dagster shapes, not vendor concerns. Pre-reqs when you cut over: GCP project + billing, BigQuery API + GCS API enabled, ADC (`gcloud auth application-default login`), `roles/bigquery.dataEditor` + `roles/storage.objectAdmin`, one landing-zone bucket.

### Real BI vendors (add-on)

The runnable demo omits live BI connections because they need real vendor creds. To wire each, follow the matching walkthrough:

- **Cognos** — full mock-Cognos-in-Docker walkthrough at `examples/cognos.md` (uses `cognos_workspace` + siblings; no credentials needed for the mock).
- **Power BI Fabric** — 7-component set (`fabric_workspace` + lakehouse / warehouse / pipeline / notebook / semantic model / report / dashboard).
- **Looker** — official [`dagster-looker`](https://docs.dagster.io/integrations/libraries/looker/dagster-looker); LookML views + explores + dashboards auto-emitted as external assets.
- **Tableau / Power BI on-prem** — official [`dagster-tableau`](https://docs.dagster.io/integrations/libraries/tableau) / [`dagster-powerbi`](https://docs.dagster.io/integrations/libraries/powerbi).

Once wired, downstream BI assets show up as first-class nodes in the Dagster graph; failures on an upstream mart alert against the exact BI reports impacted.

### What each mode demonstrates

- **Data mesh** — 3 real code-locations (`sales/`, `marketing/`, `finance/`), each with its own `pyproject.toml` + venv + Dagster project; workspace-level `dg.toml` brings them under one UI.
- **SDA + DV2.0** — `data_vault_hub_link_satellite` emits hub / link / sat as independently-materializable assets.
- **Cross-domain asset check** — `FreshnessPolicyComponent` YAML in the finance code-location blocks on `sales/dv2/customer_sat` freshness — one check, cross-code-location.
- **Legacy shell orchestration** — `shell_command_asset` runs a bash script as a Dagster asset; stdout streams through the compute log manager to Kibana.
- **Central log platform** — `OtlpComputeLogManager` in `dagster.yaml` → OTel Collector → Elasticsearch data stream, browsable in Kibana.
- **k8s job stub** — `k8s_job_asset` YAML loads and validates via `dg check` (won't execute without a real cluster; the smoke-test skips it).

Local mode uses synthetic seed data; the shape + graph + checks + lineage are identical to what you'd see against real vendors.

## Component coverage summary

Everything the F500 stack requires ships as either a community component (from this session) or an official Dagster integration:

| Category | Coverage |
|---|---|
| **Cognos (BI reports)** | 5-component set: `cognos_resource`, `cognos_report_run_job`, `cognos_report_status_sensor`, `cognos_report_data_ingestion`, `cognos_workspace` |
| **Power BI Fabric** | 7-component set: `fabric_workspace`, `fabric_lakehouse_resource`, etc. |
| **Power BI on-prem** | Official `dagster-powerbi` |
| **Collibra (lineage)** | `lineage_to_collibra` sink |
| **Elasticsearch — reads** (query ES as a data source) | `elasticsearch_asset`, `elasticsearch_reader`, `elasticsearch_resource` |
| **Elasticsearch — Dagster compute logs → ES** (instance-level, `dagster.yaml`) | `OtlpComputeLogManager` from `compute_log_managers/otlp/` → OTel Collector → Elasticsearch data stream. Same wire protocol lets you swap ES for Splunk / Datadog / Honeycomb / Loki / CloudWatch — see [`OtlpComputeLogManager` README](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/compute_log_managers/otlp/README.md). |
| **MinIO** | `minio_resource`, `minio_io_manager` |
| **Trino** | `trino_query` (federated read as an asset), `trino_io_manager`, `trino_resource` |
| **dbt** | Official `dagster-dbt` |
| **BigQuery** | 12 components + `dagster-gcp` |
| **GCS** | Official `dagster-gcp`, `gcs_monitor` |
| **Looker** | Official [`dagster-looker`](https://docs.dagster.io/integrations/libraries/looker/dagster-looker) (recommended) — auto-loads LookML views + explores + dashboards as external assets |
| **Legacy centralized scheduler** (AutoSys / Control-M / etc.) | `autosys_asset` for AutoSys bridge; `shell_command_asset` post-cutover |
| **Per-domain DAG scheduler** (DolphinScheduler / etc.) | Replaced by per-code-location `defs.yaml` + `spark_k8s_operator_asset` / `k8s_job_asset` |
| **k8s / GKE** | `k8s_job_asset` |
| **Cloud Run** | `cloud_run_job_trigger_asset`, `google_cloud_run_jobs` |
| **PySpark** | `pyspark_pipeline`, `pyspark_resource` |
| **Shell / bare-metal** | `shell_command_asset`, `shell_command_job`, `docker_container_asset` |
| **Data Vault 2.0** | `data_vault_hub_link_satellite` |
| **Data quality** | `great_expectations_check`, `pandera_asset_check`, `freshness_check` |
| **Cost management** | Dagster+ Insights (not a component) |
| **Data mesh** | Dagster+ multi-code-location + Teams RBAC (not a component) |

## Related walkthroughs

- `cognos.md` — mock Cognos in Docker (no creds); wire the BI leg of this POC to a real (mocked) Cognos server.
- `warehouse_migration.md` — legacy DB → cloud warehouse (SCT / SSMA style, but component-driven).
- `snowflake_workspace.md` — the same StateBackedComponent shape for Snowflake.
- Per-vendor workspace walkthroughs: `qlik_replicate.md`, `tm1.md`, `qlik_compose.md`, `jde.md`, `cognos.md`.
- Official [`dagster-looker`](https://docs.dagster.io/integrations/libraries/looker/dagster-looker) — for wiring a real Looker instance into the BI leg.
