# Examples

End-to-end Dagster pipelines built entirely from community components.
Each demo:

- Runs end-to-end with a single `curl | bash` then `dg launch`
- Lists exactly what it needs (auth, infra, env vars, cost)
- Has been **validated against real systems**, not just type-checked

The demos are grouped by what they need to run.

## Start here

The ten demos that best answer *"why Dagster on top of my existing stack?"*. Each has been live-validated end-to-end. Skim these first; the depth tables below hold the long tail (~200 more).

| Demo | Why | Cost |
|---|---|---|
| **[Automation-conditions analyzer](automation_analyzer.md)** | Imperative → declarative migration tool. Reads a project's existing schedules + jobs and emits proposed `AutomationConditionApplicatorComponent` rules + a plan of what to disable. `uvx --from dagster-community-components-cli dagster-analyze-schedules`. | $0 |
| **[dbt Slim CI](dbt_slim_ci.md)** | Build only state-modified dbt models on PR deploys, keep morning schedule as full builds. **Three approaches** — ⭐ start with (A) `code_version_changed()` automation condition (zero CI plumbing, least failure-prone), fall through to (B) asset_selection enumeration at CI time via `dbt ls --state`, or (C) translator subclass that tags every asset `dbt/state=modified\|unchanged\|new` so selections like `tag:dbt/state=modified and owner:data-team` work anywhere. Full GH Action snippets for GH artifacts / dbt Cloud / S3. | $0 |
| **[MLflow pipeline (end-to-end MLOps)](mlflow_pipeline.md)** | Uses all 7 community MLflow components: workspace + experiment sensor + model sensor + version check + promotion + inference + resource. Trains → registers → promotes → scores, self-contained sqlite MLflow backend. Complementary to official `dagster-mlflow` (which handles the tracking-in-during-training side). | $0 |
| **[Doris + StarRocks (OSS MPP)](doris_starrocks.md)** | Docker-local end-to-end for 6 Doris/StarRocks components (workspace + resource + query + sink + external + routine-load sensor). One demo shell, two engines — both speak MySQL wire protocol so the same YAML validates against `apache/doris` and `starrocks/allin1-ubuntu`. Round-trip: seed 5 rows → SQL query → new DataFrame → bulk-load back in. | $0 |
| **[Message-driven dbt](dbt_queue_driven.md)** | External-queue orchestration of dbt — sensor picks up "build model X with these vars" or "build all", subclasses the official dbt component for runtime vars, publishes success back to a queue as an asset. | $0 |
| **[dbt + ML + dbt (mid-DAG Python)](dbt_ml_pipeline.md)** | The flagship "why Dagster over Airflow" — a Python ML asset sitting between two sets of dbt models, all in one lineage graph. Airflow can't do this. | $0 |
| **[dbt + LLM + dbt (mid-DAG generative)](dbt_llm_pipeline.md)** | Same shape, LLM in the middle. LangChain generates personalized retention emails between dbt models. | ~$0.01 |
| **[Cube semantic layer + LLM](cube_query.md)** | Cube as the LLM safety layer — LLM never touches raw SQL, gets governed metrics from Cube instead. | $0 simple, ~$0.01 LLM |
| **[CRM reconciliation (HubSpot + Salesforce)](crm_reconciliation.md)** | Every RevOps team's "we have both HubSpot and Salesforce" problem. Outer-join contacts across both, get HS-only + SF-only + both. Synth data locally; swap for real ingestion in prod. | $0 |
| **[Temporal — full four-mode integration](temporal_workflow.md)** | Dagster ↔ Temporal: start, observe, push (signals), pull (queries). Real dev-server + Python worker + SWAPI activity. | $0 |
| **[Temporal signal + query (long-lived workflows)](temporal_signal_query.md)** | Push state INTO / pull state OUT of a running Temporal workflow from Dagster assets. | $0 |
| **[Vercel — deployment observation](vercel_deployment.md)** | Dagster observes Vercel deploys; downstream data assets gate on production going live. | $0 |
| **[Vercel AI Gateway agent (multi-provider LLM)](vercel_ai_gateway_agent.md)** | One Vercel key routes to any provider (OpenAI / Anthropic / Google / xAI) with automatic fallback. | ~$0.005 |
| **[LangGraph agent (multi-step reasoning)](langgraph_agent.md)** | Multi-step LangGraph `StateGraph` (plan / research / critique / synthesize) as one Dagster asset. | ~$0.005 |
| **[Agent + MCP tool loop (three-component family)](agent_family.md)** | Real MCP filesystem tools driven by an OpenAI agent, with an LLM judge grading the trajectory. The template for any tool-using agent. | ~$0.005 |
| **[Supabase pgvector RAG (real vector search + grounding)](supabase_rag.md)** | The RAG demo everyone is building right now — real 1536-d OpenAI embeddings + Supabase pgvector RPC + LLM grounded on retrieval. Local Supabase via CLI. | ~$0.01 |
| **[Firebase (via emulator)](firebase_emulator.md)** | Live-validated Firestore ingestion with filters, running against the official Firebase Emulator Suite. Zero cloud, zero account. | $0 |
| **[PII detection + LLM redaction check](pii_redaction.md)** | Two-pass compliance: Presidio detects PII → redactor scrubs → LLM double-checks each redacted row didn't miss anything (fresh eyes on the scrubbed output). | ~$0.005 |
| **[Data Quality agent — anomaly + LLM explanations](data_quality_agent.md)** | Every DQ pipeline flags "row 42 is anomalous" — no one has time to figure out **why**. Statistical detection + LLM writes a business-plausible reason + concrete followup check per anomaly. | ~$0.005 |
| **[Data Doctor — agent picks remediations from a bounded action space](data_doctor.md)** | The **agent decides what the pipeline does**. gpt-4o-mini looks at each column's DQ profile and picks ONE action from a bounded, safe list (drop_nulls / fill_median / clip_outliers / dedup / …). Full audit trail. This is the "AI decides, Dagster executes" pattern. | ~$0.005 |
| **[Adaptive Triage Router — LLM picks *which downstream* runs per row](adaptive_triage.md)** | Row-by-row content-based routing. Incoming support tickets → LLM classifies into billing / bug / churn_risk / spam / other → `router` fans out to per-route sinks. 100% existing components. | ~$0.005 |
| **[Adaptive Backfill Detective — LLM picks *how to fill each gap*](adaptive_backfill.md)** | Per-partition gap analysis. IoT-sensor readings with random dropouts → LLM picks ok / interpolate / re_ingest / escalate per (sensor, day) → per-action queues. The "adaptive ops" shape. | ~$0.005 |
| **[Supervisor Agent — LLM picks *which agents to call*](supervisor_agent.md)** | The "agent of agents" pattern. A planner LLM picks from a bounded YAML-declared tool set (web_search / kb_expert / math_expert / translator / critic), each pick fans out to its own asset, synthesizer LLM writes the final grounded answer with inline tool citations. | ~$0.02 |
| **[MCP Tool Picker — LLM picks *which MCP tools to call*](mcp_tool_picker.md)** | Same as Supervisor Agent, but tools are **real MCP calls** (stdio / http / sse) instead of LLM personas. Demo uses `@modelcontextprotocol/server-filesystem`; swap for Postgres / GitHub / Slack / Dagster+ MCP in prod. | ~$0.01 |
| **[Adaptive Research Brief — LLM decides *how many things* to research](adaptive_research_brief.md)** | Where DynamicOutput / dynamic partitions fits. Planner LLM picks N sub-topics at runtime (could be 3, could be 12); row-wise researcher writes a note per subtopic; synthesizer combines into a markdown brief. | ~$0.02 |
| **[Iterative Supervisor Agent — chained tool use with per-step lineage](iterative_supervisor_agent.md)** | The **ReAct loop as N real Dagster assets.** Planner runs every step, sees prior tool outputs, picks next tool or `done`. Fixes the placeholder-in-args limitation of single-shot Supervisor. Static DAG, dynamic termination via short-circuit. | ~$0.02 |
| **[Catalog Agent — LLM picks from the live 900-component registry, executes real, with schema discovery](catalog_agent.md)** | The apex. Per-step planner picks REAL components from the manifest; executor materializes them in-process; each step sees the ACTUAL columns of prior steps' outputs so it can plan against customer data with unknown schemas. | ~$0.02 |
| **[Planned Catalog Agent — LLM plans once, real assets forever](planned_catalog_agent.md)** | `dg.StateBackedComponent` variant of `catalog_agent`. Same trajectory + real materializations, but run ONCE at prepare time (`write_state_to_path`) and cached to Dagster's native state store. Every subsequent load emits REAL Dagster assets (no step_N wrappers) — zero LLM cost per run. Perfect for the "input a task in the Dagster+ UI, real assets appear" UX. | ~$0.02 once |

The full depth catalog by what-it-needs-to-run follows.

## Table of contents

- [No auth required (synthetic or public data)](#no-auth-required-synthetic-or-public-data)
  - [Core ETL patterns](#core-etl-patterns)
  - [Pushdown compute (warehouse-native + lazy engines)](#pushdown-compute-warehouse-native--lazy-engines)
  - [Database replication + migration](#database-replication--migration)
  - [Time series + forecasting](#time-series--forecasting)
  - [ML pipelines](#ml-pipelines)
  - [Customer + subscription analytics](#customer--subscription-analytics)
  - [Geospatial](#geospatial)
  - [Public APIs (no auth)](#public-apis-no-auth)
  - [OCSF / Security](#ocsf--security)
  - [Op jobs (no asset materialized)](#op-jobs-no-asset-materialized)
  - [Patterns](#patterns)
  - [Industry data standards](#industry-data-standards)
  - [Media transforms](#media-transforms)
  - [AI / NLP (no auth required)](#ai--nlp-no-auth-required)
  - [AI / NLP — via Vercel AI Gateway (one key, any provider)](#ai--nlp--via-vercel-ai-gateway-one-key-any-provider)
  - [Cloud observability + enterprise SaaS](#cloud-observability--enterprise-saas)
  - [Durable workflow orchestration (Temporal)](#durable-workflow-orchestration-temporal)
- [Azure (subscription required)](#azure-subscription-required)
  - [Storage + lakehouse](#storage--lakehouse)
  - [Databases](#databases)
  - [Orchestration + workflow](#orchestration--workflow)
  - [Streaming + queues](#streaming--queues)
  - [Microsoft Fabric (next-gen Synapse)](#microsoft-fabric-next-gen-synapse)
  - [Observability](#observability)
  - [Auth: managed identity in Azure compute](#auth-managed-identity-in-azure-compute)
- [Google Cloud (GCP, subscription required)](#google-cloud-gcp-subscription-required)
  - [Workspace (Drive, Docs, Sheets, Calendar)](#workspace-drive-docs-sheets-calendar)
  - [Warehouse (BigQuery)](#warehouse-bigquery)
  - [AI / LLM](#ai--llm)
  - [Real-pipeline patterns (multi-component chains)](#real-pipeline-patterns-multi-component-chains)
  - [Auth: workload identity in GCP compute](#auth-workload-identity-in-gcp-compute)
- [Snowflake (subscription required)](#snowflake-subscription-required)
  - [Primary — Snowflake workspace (booth demo)](#primary--snowflake-workspace-booth-demo)
  - [Companion — single-entity components](#companion--single-entity-components-manual-selection)
  - [Pushdown / Snowpark](#pushdown--snowpark)
  - [Snowflake as part of a cross-vendor blueprint](#snowflake-as-part-of-a-cross-vendor-blueprint)
- [Dagster+ required](#dagster-required)
- [Catalog Lineage Sync — multi-target](#catalog-lineage-sync--multi-target-no-auth-required-for-the-file-demo)
- [Cross-vendor blueprints (not validated)](#cross-vendor-blueprints-not-validated)
- [SAP family + OData ecosystem (one component covers many vendors)](#sap-family--odata-ecosystem-one-component-covers-many-vendors)
- [Lakehouse — external Iceberg + Delta tables (cross-engine)](#lakehouse--external-iceberg--delta-tables-cross-engine)
- [How a demo is built](#how-a-demo-is-built)
- [Auth-required demos: comprehensive prereqs](#auth-required-demos-comprehensive-prereqs)

---

## No auth required (synthetic or public data)

The biggest section — these run offline against synthetic data or public APIs.
Useful for onboarding, CI smoke tests, and proving a component works.

### Core ETL patterns

| Demo | Components | Highlights |
|---|---|---|
| [Kitchen Sink](kitchen_sink.md) | `synthetic_data_generator`, `unique_dedup`, `type_coercer`, `outlier_clipper`, `data_cleansing`, `dataframe_join`, `filter`, `select_columns`, `sort`, `summarize`, `rank`, `rfm_segmentation`, `dataframe_to_csv`, `cron_schedule` | The breadth showcase — ingest × 3 → quality × 4 → join → transform × 3 → analytics × 4 → sink × 5 → schedule |
| [Pivot ↔ Unpivot](pivot_unpivot.md) | `file_ingestion`, `pivot`, `unpivot`, `dataframe_to_csv` | Long↔wide round-trip on monthly sales |
| [Router](router.md) | `file_ingestion`, `router`, `dataframe_to_csv` | Multi-output conditional split (high/medium/low value orders) |
| [Detect Changes](detect_changes.md) | `file_ingestion`, `detect_changes`, `dataframe_to_csv` | CDC: classify rows insert/update/delete/unchanged |
| [SCD Type 2](scd_type_2.md) | `file_ingestion`, `scd_type_2`, `dataframe_to_csv` | History-tracking dimension load |
| [Window Calculation](window_calculation.md) | `file_ingestion`, `window_calculation`, `dataframe_to_csv` | Every supported window function on stock prices |
| [Regional Orders Union](regional_orders.md) | `file_ingestion`, `dataframe_union`, `dataframe_to_csv` | Merge multi-region order extracts with mismatched columns |
| [Data Hygiene](data_hygiene.md) | `synthetic_data_generator`, `audit_columns`, `schema_validator`, `field_mapper`, `map_values`, `data_masking`, `hash`, `surrogate_key`, `record_id`, `count_records` | Toolbox demo: audit / validate / rename / canonicalize / mask / hash / surrogate-key / number / aggregate |
| [DataFrame Basics](dataframe_basics.md) | `filter`, `sort`, `unique_dedup`, `select_columns`, `data_cleansing`, `summarize`, `rank`, `running_total`, `transpose` | 9 fundamental shape-preserving transforms |
| [Data Combination](data_combination.md) | `dataframe_join`, `dataframe_union`, `formula`, `type_coercer`, `datetime_parser`, `array_exploder`, `ts_filler` | Joins / unions / reshape / coerce — the toolbox for stitching shapes |
| [Text Extraction](text_extraction.md) | `xml_parser`, `html_parser`, `json_flatten`, `json_path_extractor`, `nested_field_extractor`, `regex_parser` | Pull structured fields from semi-structured columns (XML, HTML, nested JSON) |
| [Transformations Mega-Demo](transformations.md) | 34 transforms — every shape-preserving + shape-changing transform in one chain | Comprehensive toolbox showcase |
| [Partitions](partitions.md) | `dataframe_to_csv`, `per_partition_backfill_job` | The four canonical partition shapes (daily/weekly/monthly + static dimensions) end-to-end |

### Pushdown compute (warehouse-native + lazy engines)

Components that push the compute to the engine instead of materializing through pandas — predicate pushdown to parquet, CTAS chains in the warehouse, lazy chains in polars / PySpark / Snowpark. Each demo exercises many components in one shot.

| Demo | Components | Highlights |
|---|---|---|
| [Warehouse-native pipeline](warehouse_native_pipeline.md) | 12 — `synthetic_data_generator`, `dataframe_to_table`, `warehouse_filter`, `warehouse_top_n_per_group`, `warehouse_dedup`, `warehouse_join`, `warehouse_union`, `warehouse_formula`, `warehouse_multi_field_formula`, `warehouse_multi_row_formula`, `warehouse_summarize`, `warehouse_pipeline` | Every `warehouse_*` CTAS-pushdown component composed end-to-end. Local DuckDB; same YAML retargets to Snowflake / BigQuery / Redshift / Databricks / Postgres / MSSQL. Formula family covers Alteryx "Formula In-DB" / "Multi-Field Formula" / "Multi-Row Formula" equivalents in pure SQL. |
| [Polars-native pushdown](polars_pushdown.md) | `synthetic_data_generator`, `dataframe_to_parquet`, `polars_scan_parquet`, `polars_pipeline` | Predicate + column pushdown to parquet, then 5-op LazyFrame chain in one asset (filter → with_columns → group_by → sort → head). Demonstrates what the per-asset `backend: polars` field architecturally can't deliver. |
| [Polars pipeline (standalone)](polars_pipeline.md) | `synthetic_data_generator`, `polars_pipeline` | Same single-asset multi-op chain pattern, taking pandas DataFrame input (no parquet step). Useful when upstream is already in-memory. |
| [PySpark pipeline](pyspark_pipeline.md) | `synthetic_data_generator`, `dataframe_to_parquet`, `pyspark_pipeline` | Multi-step Spark DataFrame chain compiled to ONE Catalyst plan. `local[*]` Spark for the demo; same YAML retargets to Standalone / YARN / Kubernetes / Databricks Connect. Requires Java 17+. |
| [Snowpark pipeline](snowpark_pipeline.md) **(needs Snowflake creds)** | `snowpark_pipeline` | Multi-step Snowpark DataFrame chain compiled to ONE Snowflake SQL statement. Whole pipeline runs server-side in the Snowflake compute warehouse — no data through Python. Code-level validated; end-to-end run needs `SNOWFLAKE_ACCOUNT` / `SNOWFLAKE_USER` / `SNOWFLAKE_PASSWORD` plus a source table in `RAW.ORDERS`. |

### Database replication + migration

End-to-end SQL → SQL data movement, plus the one-shot lift+shift workflow. Postgres → DuckDB in the demos, but the same YAML retargets to Oracle / Db2 / MSSQL sources and Snowflake / BigQuery / Redshift / Databricks targets unchanged.

| Demo | Components | Highlights |
|---|---|---|
| [Recurring SQL→SQL Replication](replication.md) | `database_replication` | The **recurring data sync** pattern. 3 instances: full refresh, incremental + upsert with watermark, column subset + WHERE filter. Wraps the official `dagster-sling` `@sling_assets` under the hood — no Sling YAML to write. |
| [Warehouse Migration](warehouse_migration.md) | `database_migration_assessment` + `database_schema_inventory` + `database_tables_migration` + `database_constraints_migration` + `database_view_migration` + `database_views_migration` + `database_replication` + `dataframe_to_csv` | The **one-time lift+shift** story. (1) Inventory every source object. (2) Pre-flight assessment dry-runs every CREATE inside a transaction that rolls back — returns per-object status (`auto_convertible` / `needs_review` / `will_fail`), complexity heuristic, and estimated manual effort. (3) DDL-first or data-first migration with all 6 constraint types preserved (PK + FK + NOT NULL + DEFAULT + CHECK + UNIQUE). (4) Bulk view recreation with table-ref + function-name substitutions. (5) Per-step status DataFrames → CSV migration completion report. AWS SCT / SSMA pattern, inside Dagster. |
| [Databricks Workspace → Dagster](databricks_workspace.md) | **official** `DatabricksWorkspaceComponent` (`dagster-databricks`) | **One-script setup** for customers with existing Databricks Jobs. Interactive prompts → enumerates every job in the workspace via the Jobs API → user picks which to bring in → asks about cross-job dependencies → generates a working Dagster project with `databricks_filter.include_jobs.job_ids` + `asset_overrides.<job>.depends_on` populated. Auto-installs `uv` + `jq` if missing. Token verified before scaffolding. |
| [**Snowflake Workspace → Dagster**](snowflake_workspace.md) **(booth demo)** | `snowflake_workspace` + 11 optional add-ons (`warehouse_pipeline`, `snowpark_pipeline`, `snowflake_cortex_asset`, `snowflake_cortex_search`, `snowflake_iceberg_table`, `snowflake_time_travel_asset`, `snowflake_snowpipe_load_sensor`, partitioned heterogeneous chain, `dagster-dbt`, 7 define-as-code DDL components, freshness checks, external table refs) | **Two-script booth demo** — empty Snowflake to fully-orchestrated Dagster project in 5 minutes. Script 1 (`setup_snowflake_environment.sh`) seeds ~30 entities. Script 2 (`setup_snowflake_workspace_demo.sh`) auto-detects what your account can do (capability probes for Iceberg / Cortex / MV / Unistore) and only scaffolds components that materialize. Works on Standard edition with graceful tier-aware degradation. PAT / SSO / keypair / password / password+MFA all supported. |
| [**Qlik Replicate → Dagster**](qlik_replicate.md) | `qlik_replicate_workspace` (StateBackedComponent) + `qlik_replicate_resource` + `qlik_replicate_task_trigger_job` + `qlik_replicate_task_status_sensor` + `qlik_replicate_task_metrics_ingestion` | Wrap Qlik Replicate with Dagster. Mock Qlik Enterprise Manager in Docker (Flask, ~130 MB, $0). Workspace enumerates every server × task and auto-emits one asset per task (Fivetran-shape `task_selector`). Materializing an asset triggers the underlying Replicate task (run/reload/stop) and polls to terminal state. Same base URL swap works against real Qlik EM in prod. |
| [**TM1 (IBM Planning Analytics) → Dagster**](tm1.md) | `tm1_workspace` (StateBackedComponent) + `tm1_resource` + `tm1_process_trigger_job` + `tm1_process_status_sensor` + `tm1_cube_data_ingestion` | Wrap TM1 planning cubes with Dagster. Mock TM1 REST server in Docker. Workspace enumerates every Cube / Process / Chore with per-kind Fivetran-shape selectors and auto-emits one asset each; Process/Chore assets execute their underlying TI when materialized; Cube assets pair with `tm1_cube_data_ingestion` for MDX-based data extraction. CAM SSO supported. |
| [**Qlik Compose → Dagster**](qlik_compose.md) | `qlik_compose_workspace` (StateBackedComponent) + `qlik_compose_resource` + `qlik_compose_workflow_trigger_job` + `qlik_compose_workflow_status_sensor` + `qlik_compose_workflow_metrics_ingestion` | Wrap Qlik Compose (DW automation) with Dagster. Mock Compose REST in Docker. Workspace enumerates every Project × Workflow × Data Mart with per-kind Fivetran-shape selectors. Common pairing: Replicate lands source CDC → Compose runs DW workflows on top → both wrapped as one Dagster asset graph. |
| [**JDE Orchestrator → Dagster**](jde.md) | `jde_orchestrator_workspace` (StateBackedComponent) + `jde_orchestrator_resource` + `jde_orchestration_trigger_job` + `jde_orchestration_status_sensor` + `jde_orchestration_output_ingestion` | Wrap JDE Orchestrator (Oracle's low-code automation for JD Edwards EnterpriseOne) with Dagster. Mock AIS REST in Docker. Supports sync + async orchestration invocation with jobId polling. Workspace auto-emits assets per orchestration. Data Services orchestrations can be materialized as DataFrames for warehouse pipelines. |
| [**IBM Cognos Analytics → Dagster**](cognos.md) | `cognos_workspace` (StateBackedComponent) + `cognos_resource` + `cognos_report_run_job` + `cognos_report_status_sensor` + `cognos_report_data_ingestion` | Wrap Cognos Analytics (BI reports) with Dagster. Mock Cognos REST in Docker. Session-based auth with security namespace (LDAP / CognosEx). Six output formats (PDF/HTML/CSV/XLSX/XML/JSON); CSV+JSON parse into DataFrames for warehouse pipelines. Workspace enumerates reports in Cognos folders with Fivetran-shape selector. |
| [**Fortune 500 Data Platform POC**](f500_poc.md) | 10 POC eval items mapped to concrete components + Dagster+ features | The full brick-and-mortar F500 stack: Cognos + Power BI Fabric + Collibra (lineage) + Elasticsearch + MinIO + Trino + dbt + BigQuery + GCS + Looker + PySpark + Cloud Run + shell-on-bare-metal + Data Vault 2.0. Walkthrough maps each POC evaluation item (data mesh, SDA vs task scheduling, DV2.0 modeling, dbt on GKE, MLOps PySpark migration from Prefect, generic k8s / Cloud Run / bare-metal orchestration, BigQuery cost visibility) to concrete community components or Dagster+ features. |

The component family — `database_schema_inventory`, `database_migration_assessment`, `database_tables_migration`, `database_constraints_migration`, `database_view_migration`, `database_views_migration`, `database_replication`, plus the power-user `sling_replication_asset` — all share a consistent design: per-component status DataFrame, `dry_run: true` flag for transaction+rollback validation before commit, `include_patterns` / `exclude_patterns` for fnmatch-style filtering, `*_ddl_overrides` escape hatches for the dialect-quirky 20%, and pre-flight warnings when the target accepts but doesn't enforce constraints (Snowflake / Redshift) or rejects them entirely (BigQuery CHECK).

### Time series + forecasting

| Demo | Components | Highlights |
|---|---|---|
| [Airline Passengers Forecast](passengers_forecast.md) | `file_ingestion`, `datetime_parser`, `ets_forecast`, `dataframe_to_csv` | ETS / Holt-Winters time-series forecasting |
| [Forecast Comparison](forecast_comparison.md) | `time_series_generator`, `ts_filler`, `arima_forecast`, `ets_forecast`, `ts_compare`, `dataframe_to_csv`, `cron_schedule` | Head-to-head ARIMA vs ETS on the same series |
| [Stocks — Anomaly Detection](stocks_anomaly.md) | `file_ingestion`, `anomaly_detection`, `dataframe_to_csv` | Per-ticker z-score outlier flagging |
| [Sensor Gap-Fill](sensor_gapfill.md) | `synthetic_data_generator`, `ts_filler`, `running_total`, `dataframe_to_csv` | Fill missing hourly readings + cumulative metrics |
| [Synthetic Metrics + Anomalies](synthetic_metrics.md) | `time_series_generator`, `anomaly_detection`, `dataframe_to_csv` | No-upstream synthetic generator |
| [Forecasting (ARIMA + ETS)](forecasting.md) | `arima_forecast`, `ets_forecast`, `create_samples` | Side-by-side ARIMA + ETS with train/val/test splitting |
| [Time-Series Advanced](time_series_advanced.md) | `ts_forecast`, `ts_compare`, `ts_covariate_forecast`, `ts_model_factory` | Comparison + covariates + per-group factory |

### ML pipelines

| Demo | Components | Highlights |
|---|---|---|
| [Palmer Penguins](penguins.md) | `file_ingestion`, `imputation`, `one_hot_encoding`, `feature_scaler`, `dataframe_to_parquet` | Canonical ML preprocessing |
| [Iris Unsupervised](iris_unsupervised.md) | `file_ingestion`, `feature_scaler`, `pca`, `k_means_clustering`, `dataframe_to_csv` | PCA + K-Means on the classic dataset |
| [Wine ML Pipeline](wine_ml_pipeline.md) | `file_ingestion`, `feature_scaler`, `create_samples`, `decision_tree_model`, `cross_validation`, `dataframe_to_csv`, `cron_schedule` | Feature scaling → train/test split → decision tree + cross-validation |
| [Titanic Complete](titanic_complete.md) | `file_ingestion`, `unique_dedup`, `data_cleansing`, `outlier_clipper`, `imputation`, `type_coercer`, `tile_binning`, `one_hot_encoding`, `logistic_regression_model`, `summarize`, `filter`, `dataframe_to_csv`, `cron_schedule` | Full DS workflow: ingest → quality → ETL → model → 3 outputs |
| [Churn Prediction](churn.md) | `synthetic_data_generator`, `churn_prediction`, `dataframe_to_csv` | Rule-based scoring with interpretable risk factors |
| [Market Basket](market_basket.md) | `file_ingestion`, `market_basket_rules`, `filter`, `summarize`, `dataframe_to_csv`, `cron_schedule` | Apriori association rules with lift filter |
| [ML Feature Engineering](ml_features.md) | `imputation`, `outlier_clipper`, `label_encoder`, `one_hot_encoding`, `feature_scaler`, `tile_binning` | 6 pre-modeling transforms (drop-in for sklearn pipelines) |

### Customer + subscription analytics

| Demo | Components | Highlights |
|---|---|---|
| [CRM Reconciliation (HubSpot + Salesforce)](crm_reconciliation.md) | `hubspot_ingestion`, `salesforce_ingestion`, `dataframe_join`, `synthetic_data_generator` | The "we have both CRMs" problem — outer-join on email → HubSpot-only + Salesforce-only + both. Synth data locally, swap ingestion type for production. Live-validated. |
| [SaaS Metrics (synthetic Stripe)](saas_metrics.md) | `synthetic_data_generator`, `subscription_metrics`, `dataframe_to_csv` | MRR / ARR / churn / LTV / ARPU |
| [Revenue Attribution](revenue_attribution.md) | `file_ingestion`, `synthetic_data_generator`, `revenue_attribution`, `dataframe_to_csv` | Linear attribution across marketing channels |
| [Retail LTV (CDP)](retail_ltv.md) | `file_ingestion`, `data_cleansing`, `formula`, `ltv_prediction`, `dataframe_to_csv` | LTV on 542k real transactions |
| [Subscription Survival](subscription_survival.md) | `synthetic_data_generator`, `survival_analysis`, `dataframe_to_csv`, `cron_schedule` | Kaplan-Meier survival on SaaS subscriptions |
| [Retail Analytics](retail_analytics.md) | `synthetic_data_generator`, `datetime_parser`, `rfm_segmentation`, `cohort_analysis`, `running_total`, `dataframe_to_csv`, `cron_schedule` | RFM segmentation + cohort analysis + running spend |
| [A/B Full Pipeline](ab_full_pipeline.md) | `synthetic_data_generator`, `ab_treatments`, `ab_test_analysis`, `ab_trend`, `ab_controls`, `dataframe_to_csv`, `cron_schedule` | Assignment + analysis + trend + sample-size |
| [Customer Analytics](customer_analytics.md) | `customer_journey_mapping`, `customer_segmentation`, `multi_touch_attribution`, `random_forest_model`, `text_preprocessing`, `topic_modeling` | Journey / segmentation / attribution / ML / NLP all in one chain |
| [Analytics Mega-Demo](analytics.md) | 40 analytics components | Comprehensive showcase |

### Geospatial

| Demo | Components | Highlights |
|---|---|---|
| [Airports Cluster](airports_cluster.md) | `file_ingestion`, `spatial_cluster`, `dataframe_to_csv` | DBSCAN on lat/lng (haversine, real km) |
| [US Cities Pairwise Distances](cities_distance.md) | `file_ingestion`, `dataframe_join`, `distance_calculator`, `filter`, `sort`, `dataframe_to_csv` | Haversine distance matrix |
| [Cities Nearest Neighbors](cities_nn.md) | `file_ingestion`, `nearest_neighbors`, `dataframe_to_csv` | Top-3 closest cities (sklearn KD-tree) |
| [West Coast Cities Filter](west_coast_cities.md) | `file_ingestion`, `bounding_box_filter`, `dataframe_to_csv` | Geographic bounding-box filter |
| [Store Coverage](store_coverage.md) | `file_ingestion`, `create_points`, `buffer`, `smooth`, `make_grid`, `spatial_join`, `summarize`, `dataframe_to_csv`, `cron_schedule` | Buffer + spatial_join + summarize coverage |

### Public APIs (no auth)

| Demo | Components | Highlights |
|---|---|---|
| [USGS Earthquakes](earthquakes.md) | `rest_api_fetcher`, `json_flatten`, `select_columns`, `sort`, `dataframe_to_json` | REST + nested JSON |
| [Earthquakes Partitioned](partitioned_earthquakes.md) | `rest_api_fetcher`, `json_flatten`, `select_columns`, `sort`, `dataframe_to_json` | Backfillable date range |
| [SpaceX Launches](spacex.md) | `rest_api_fetcher`, `select_columns`, `datetime_parser`, `rank`, `dataframe_to_excel` | Datetime parsing + ranking |
| [SpaceX Multi-Source Join](spacex_join.md) | `rest_api_fetcher`, `dataframe_join`, `select_columns`, `dataframe_to_csv` | Fan-in two REST sources |
| [REST Countries](countries.md) | `rest_api_fetcher`, `formula`, `summarize`, `dataframe_to_json` | Computed columns + rollup |
| [NYC Weather](weather.md) | `rest_api_fetcher`, `datetime_parser`, `running_total`, `transpose`, `dataframe_to_csv` | Cumulative + pivot |
| [Dagster GitHub Releases](releases.md) | `rest_api_fetcher`, `select_columns`, `datetime_parser`, `filter`, `sort`, `dataframe_to_parquet` | Filter + sort + parquet |
| [GitHub Search JSONPath](github_jsonpath.md) | `rest_api_fetcher`, `nested_field_extractor`, `json_path_extractor`, `dataframe_to_csv` | Two ways to flatten nested JSON |
| [HN RSS](hn_rss.md) | `rest_api_fetcher`, `regex_parser`, `filter`, `dataframe_to_csv` | XML feed → structured rows |
| [HN XML Parser](hn_xml.md) | `rest_api_fetcher`, `xml_parser`, `array_exploder`, `dataframe_to_csv` | xpath all the way down |
| [Wikipedia Multi-page Scraper](wiki_scraper.md) | `rest_api_fetcher`, `html_parser`, `array_exploder`, `dataframe_to_json` | Scrape multiple wiki pages |
| [Books Scraper (partitioned)](books_scraper.md) | `rest_api_fetcher`, `html_parser`, `dataframe_to_json` | Multi-page HTML scrape, one partition per page |
| [arXiv PDF Extraction](arxiv_pdf.md) | `file_ingestion`, `pdf_text_extractor`, `formula`, `dataframe_to_csv` | Document → text → word counts |
| [Cars → SQL](cars_sql.md) | `rest_api_fetcher`, `datetime_parser`, `formula`, `dataframe_to_table` | Land DataFrame in SQLite |
| [Movies → SQL](movies_sql.md) | `file_ingestion`, `type_coercer`, `formula`, `dataframe_to_table` | Real MovieLens Top 250 → SQLite |
| [NBA Scoreboard](nba_scoreboard.md) | `rest_api_fetcher`, `json_path_extractor`, `dataframe_to_csv`, `http_poll_sensor` | `http_poll_sensor` with targeted hashing |
| [RSS Sensor](rss_sensor.md) | `rest_api_fetcher`, `xml_parser`, `dataframe_to_csv`, `rss_feed_sensor` | Sensor-driven HN frontpage ingestion |

### OCSF / Security

| Demo | Components | Highlights |
|---|---|---|
| [OCSF + Security Lake](ocsf_security_lake.md) | `file_ingestion`, `ocsf_normalizer`, `ocsf_validator`, `dataframe_to_parquet` | Synthetic Dagster+ events through full OCSF pipeline (no AWS required) |

### Op jobs (no asset materialized)

| Demo | Components | Highlights |
|---|---|---|
| [Shell Command Job](shell_command_job.md) | `shell_command_job` | Scheduled shell command, no asset |
| [Dynamic Fanout Job](dynamic_fanout_job.md) | `dynamic_fanout_job` | DynamicOut: discover N items, parallel process, optional collect |
| [Per-File Processor](per_file_processor.md) | `per_file_processor_job` | Inbox-style fan-out: list local CSVs, parse each, archive on success |
| [Composition Primitives](composition_primitives.md) | `python_callable_job`, `http_webhook_job`, `observability_heartbeat_job`, `warehouse_maintenance_job`, `sql_command_job` | 5 small op-job wrappers in one project — SQLite + httpbin.org, no auth |

### Patterns

| Demo | Components | Highlights |
|---|---|---|
| [DuckDB Warehouse](duckdb_warehouse.md) | `file_ingestion`, `duckdb_io_manager`, `cron_schedule` | IO manager round-trip + downstream asset + daily schedule |
| [External Scheduler](external_scheduler.md) | `file_ingestion`, `summarize`, `dataframe_to_csv`, `asset_job` | Pattern for keeping Control-M / Autosys / cron as master with Dagster as executor (GraphQL launchRun) |
| [Data Quality](data_quality.md) | `pandas_dataframe_check`, `pandera_asset_check`, `enhanced_data_quality_checks`, `freshness_check`, `great_expectations_check` | 4 asset_check components on a synthetic orders asset |
| [Data Quality Checks](data_quality_checks.md) | `synthetic_data_generator`, `dataframe_to_csv`, `enhanced_data_quality_checks`, `pandas_dataframe_check`, `pandera_asset_check`, `freshness_check` | End-to-end DQ pipeline |
| [Email Round-Trip (SMTP + IMAP)](email_roundtrip.md) | `synthetic_data_generator`, `smtp_send_asset`, `imap_inbox_source` | Fan out via SMTP, pull replies via IMAP. Local aiosmtpd + pure-Python IMAP stub for offline testing |
| [External Assets](external_assets.md) | 23 external-system integrations as declared assets | Declare external systems without owning their execution (Snowflake / BigQuery / Iceberg / Delta / Kafka / SharePoint / etc.) |
| [Lakehouse Local Roundtrip](lakehouse_local.md) | `synthetic_data_generator`, `iceberg_catalog_resource`, `iceberg_ingestion`, `dataframe_to_iceberg_table`, `delta_ingestion`, `dataframe_to_delta_table`, `external_iceberg_table`, `external_delta_table` | Full Iceberg + Delta read/write cycle on the local filesystem — no cloud, no JVM, SQLite-backed Iceberg catalog |
| [Local Transforms + Sinks](local_transforms.md) | `synthetic_data_generator`, `local_parquet_io_manager`, `filter`, `summarize`, `dataframe_to_avro` | Parquet IO manager + filter → summarize → Avro sink, all on /tmp |
| [Papermill Notebooks](notebooks.md) | `synthetic_data_generator`, `jupyter_notebook_asset` | Execute a `.ipynb` as a Dagster asset via papermill, with injected parameters + executed-notebook artifact |
| [HTTP External Asset](http_external_asset.md) | `http_external_asset` | Wraps any HTTP-driven external job runner as a Dagster asset |
| [Local IO Managers](local_io.md) | 9 IO managers + 3 source/sink components — duckdb / sqlite / lance / polars / parquet / csv / json | Round-trip a DataFrame through every local format |
| [Local Sinks](local_sinks.md) | `dataframe_to_csv`, `dataframe_to_parquet`, `dataframe_to_json`, `dataframe_to_excel`, `dataframe_to_table` | 5 file/table format sinks side by side |
| [Document Extractors](document_extractors.md) | 13 document-source components — PDF / Word / HTML / RST / Markdown / etc. | Mega-demo: every shipped document-extraction component |
| [S3 Dynamic-Partition Pipeline](s3_pipeline.md) | `s3_monitor` (dynamic_partition mode), `file_ingestion` (partitioned), `summarize`, `dataframe_to_parquet` | Sensor-driven round-trip on **local Minio S3** (Docker). Each detected file becomes a tracked dynamic partition → processed → parquet back to S3. Real S3 / GCS / ADLS by swapping the URI scheme. |
| [Kafka End-to-End](kafka.md) | `external_kafka_asset`, `kafka_resource`, `kafka_to_database_asset`, `kafka_monitor`, `kafka_observation_sensor` | Full Kafka family against a **local KRaft broker** (Docker, no Zookeeper). Topic → SQLite via SQLAlchemy + sensor + observation. Retargets at MSK / Confluent Cloud / self-hosted by swapping `bootstrap_servers`. |
| [Apache Pulsar End-to-End](pulsar.md) | `pulsar_to_database_asset`, `pulsar_monitor`, `pulsar_observation_sensor`, `python_callable_job` | Full Pulsar family against a **local `apachepulsar/pulsar:latest` standalone** container. Topic → SQLite + monitor sensor + observation. Retargets at StreamNative Cloud unchanged. |
| [Docker Container Asset](docker_container.md) | `docker_container_asset` | Run any container image as a Dagster asset via `dagster-docker`. Image / command / env / network all declarative; logs stream into the Dagster run log. Demo runs `alpine` + `python:3.11-slim` end-to-end. |
| [MongoDB End-to-End](mongodb.md) | `mongodb_resource`, `mongodb_reader`, `mongodb_writer`, `synthetic_data_generator` | Read / write MongoDB against a **local mongo:7 container**. Query + projection + sort on the reader, append/replace/upsert on the writer. Retargets at Atlas / self-hosted replica set by swapping `connection_string_env_var`. |
| [Redis End-to-End](redis.md) | `redis_resource`, `redis_streams_monitor`, `redis_stream_observation_sensor`, `cache_invalidation_job`, `python_callable_job` | Streams + cache invalidation against a **local redis:7-alpine container**. Cache flush by glob pattern (`session:*`), stream-sensor target job, observation sensor for freshness. Retargets at ElastiCache / Redis Cloud unchanged. |
| [RabbitMQ End-to-End](rabbitmq.md) | `rabbitmq_to_database_asset`, `rabbitmq_monitor`, `rabbitmq_observation_sensor`, `python_callable_job` | AMQP queue → SQLite via SQLAlchemy + sensors against a **local rabbitmq:4-management container**. Uses v4 `rabbitmqadmin` (different CLI from v3). Retargets at Amazon MQ / CloudAMQP unchanged. |
| [NATS End-to-End](nats.md) | `nats_to_database_asset`, `nats_monitor`, `nats_observation_sensor`, `python_callable_job` | JetStream pull-consumer → SQLite against **local nats:latest + natsio/nats-box sidecar** for CLI ops. Pre-creates durable consumer with `--deliver=all` so backfill works. Retargets at Synadia Cloud unchanged. |
| [MQTT End-to-End](mqtt.md) | `mqtt_to_database_asset`, `mqtt_monitor`, `mqtt_observation_sensor`, `python_callable_job` | Topic subscribe → SQLite against a **local eclipse-mosquitto:2 container**. Fire-and-forget broker — demo runs a concurrent publisher during the asset's collect window. Retargets at AWS IoT Core / HiveMQ Cloud unchanged. |
| [Neo4j End-to-End](neo4j.md) | `neo4j_resource`, `neo4j_reader`, `neo4j_writer`, `synthetic_data_generator` | Cypher read + DataFrame → labeled nodes (MERGE upsert) against a **local neo4j:5-community container**. Retargets at AuraDB unchanged. |
| [Elasticsearch End-to-End](elasticsearch.md) | `elasticsearch_resource`, `elasticsearch_reader` | Index search → DataFrame against a **local elasticsearch:8.15 container** (security disabled). Retargets at Elastic Cloud unchanged. |
| [Cassandra End-to-End](cassandra.md) | `cassandra_resource`, `cassandra_reader`, `cassandra_writer` | CQL read + DataFrame → table write against a **local cassandra:5 container**. Seeds 10 events, reader returns all 10, writer copies to a sibling table. |
| [CouchDB End-to-End](couchdb.md) | `couchdb_resource`, `couchdb_reader`, `couchdb_writer` | Mango-selector read + upsert against a **local couchdb:3 container**. Seeds 5 docs (4 active + 1 cancelled), reader filters via `{status: active}`, transform bumps totals +10%, writer upserts back into a target database. |
| [DynamoDB Local End-to-End](dynamodb_local.md) | `dynamodb_resource`, `dynamodb_reader`, `dynamodb_writer` | Scan + BatchWriteItem against the official **amazon/dynamodb-local container**. Uses boto3's `AWS_ENDPOINT_URL_DYNAMODB` env var trick to retarget every reader/writer at the emulator with no component change. Round-trip: 5 seeded items → filter → transform → write back to a sibling table. |
| [ClickHouse advanced (IO manager + sensor)](clickhouse_advanced.md) | `clickhouse_io_manager`, `clickhouse_table_observation_sensor`, `external_clickhouse_table`, `clickhouse_resource` | Companion to `clickhouse.md` — validates the **code-level** ClickHouse components against the same Docker image. Project-wide `ClickhousePandasIOManager` auto-persists DataFrames as tables; observation sensor emits row_count / size_bytes / active_parts every 60s. Fixes upstream `username`/`user` mismatch bug in both IO managers. |
| [Trino](trino.md) | `trino_resource`, `trino_io_manager` | Trino coordinator (memory catalog) in **local trinodb/trino container**. `dg check defs` passes + connectivity verified; full materialization requires a DELETE-supporting catalog (Iceberg / Delta / Postgres). |
| [Oracle Database](oracle.md) | `oracle_resource`, `dataframe_to_table`, `synthetic_data_generator`, `local_parquet_io_manager` | Oracle Database Free in **local Docker container** — no license, no Instant Client. Retargets at Oracle Autonomous DB / Enterprise / OCI by changing host + service_name only. |
| [IBM Db2](db2.md) | `db2_resource`, `dataframe_to_table`, `synthetic_data_generator`, `local_parquet_io_manager` | Db2 Community Edition in **local Docker container** (free, non-production). Retargets at Db2 on Cloud / Db2 Warehouse by changing host + port + ssl. |
| [IBM Db2 for i (AS/400 / iSeries)](db2_iseries.md) | `db2_resource` + downstream components | Meta-walkthrough for pointing existing Db2 components at a real AS/400 — no Docker image exists for IBM i (proprietary hardware). Covers what changes vs. Db2 LUW + which catalog queries work on i. |
| [Applying Automation Conditions broadly](automation_condition_pipeline.md) | `automation_condition_applicator` | Set Dagster `AutomationCondition`s across many assets at once without editing every `defs.yaml` — fall-through priority, preserve-existing, auto-derive from upstream cadences. Validated against a 4-asset test project. |

---

### Industry data standards

| Demo | Components | Highlights |
|---|---|---|
| [ACORD (Insurance XML)](acord.md) | `synthetic_data_generator`, `acord_xml_parser` | Carrier / broker / quote XML → flat Policy/Claim/Quote rows. Auto-strips namespaces, handles SignonRq boilerplate |
| [FHIR R4 / R5 (Healthcare)](fhir_normalizer.md) | `synthetic_data_generator`, `fhir_resource_normalizer` | 10 resource types (Patient/Observation/Claim/Coverage/Practitioner/Organization/Bundle/Encounter/Condition/MedicationRequest) |
| [HL7 v2 (Healthcare)](hl7_parser.md) | `synthetic_data_generator`, `hl7_v2_parser` | ADT^A01 + ORU^R01 + ORM^O01 → 9 segments parsed (MSH/PID/OBX/ORC/OBR/PV1/EVN/DG1/AL1) |
| [ISO 20022 (Payments)](iso20022.md) | `synthetic_data_generator`, `iso20022_payment_parser` | pacs.008 + pacs.002 XML → flat transaction rows; SEPA/Fedwire/CHIPS compatible |
| [X12 EDI (US B2B)](x12_edi.md) | `synthetic_data_generator`, `x12_edi_parser` | 270/271/835/837/850 transaction sets, ISA/GS envelope walking with auto-detected delimiters |
| [FIX 4.4 (Trading)](fix_message.md) | `synthetic_data_generator`, `fix_message_parser` | NewOrderSingle + ExecutionReport with checksum-correct wire format; tag→column resolution |

### Media transforms

| Demo | Components | Highlights |
|---|---|---|
| [Audio Transform](audio_transform.md) | `synthetic_audio_generator`, `audio_transform_asset` | Resample to 16kHz mono WAVs — Whisper-ready output |
| [Image Transform](image_transform.md) | `synthetic_image_generator`, `image_transform_asset` | PNG → resized WebP thumbnails (Pillow) |
| [Image EXIF Extractor](image_exif.md) | `synthetic_image_generator`, `image_exif_extractor` | JPEGs with injected EXIF → flat DataFrame of metadata |
| [Video Pipeline](video_pipeline.md) | `synthetic_video_generator`, `video_metadata_extractor`, `video_frame_extract_asset`, `video_audio_extract_asset` | MP4 → metadata + sampled frames + extracted audio (ffmpeg/ffprobe) |
| [Text Codec Convert](codec_convert.md) | `synthetic_data_generator`, `text_codec_convert_asset` | Sanitize multilingual text to ASCII (NFD + transliteration) |
| [Vision Pipeline](vision_pipeline.md) | `synthetic_image_generator`, `image_metadata_extractor`, `vision_model` | Image metadata + vision-LLM description |

### AI / NLP — truly no auth (local-only)

These demos run end-to-end with **no API keys** — just `pip install` of local libraries (spaCy / nltk / pandas / etc.) or a locally-installed model server.

| Demo | Components | Highlights |
|---|---|---|
| [Message-driven dbt](dbt_queue_driven.md) | `dagster_dbt.DbtProjectComponent` (subclassed for runtime `--vars`), user-written `@job` ×2, `@sensor`, `@asset` write-back | External-queue orchestration pattern. A sensor picks up messages ("build model X with vars", "build all"), routes to one of two jobs, dbt injects the vars per run, then a `queue_completion` asset writes success back to `output_queue.jsonl`. Sensor simulates queue reads with weighted random (80% single / 15% skip / 5% run-all) so the demo runs with just `dg dev`. Teaches how to customize the official dbt integration via subclassing. Live-validated end-to-end. |
| [dbt + ML + dbt (mid-DAG Python)](dbt_ml_pipeline.md) | `dagster_dbt.DbtProjectComponent` (×2 with `op.name` split), `churn_prediction`, `duckdb_pandas_io_manager` | **Flagship "why Dagster over Airflow" demo.** A Python ML asset sitting between two sets of dbt models, all in ONE lineage graph. Airflow can't do this — dbt is one opaque operator. Dagster treats every dbt model as an asset, so a Python asset drops in. DuckDB + local sklearn heuristic scorer, $0, no API keys. |
| [AI without LLMs](ai_no_llm.md) | `synthetic_data_generator`, `keyword_extractor`, `language_detector`, `pii_detector`, `pii_redactor`, `embeddings_generator` | Rule-based + local-model AI — no API keys needed |
| [Ollama (local LLM)](ollama.md) | `synthetic_data_generator`, `ollama_inference_asset` | Local Llama / Mistral / etc. via Ollama (free, runs in your laptop) — requires `ollama serve` running |
| [Text Extraction](text_extraction.md) | `json_flatten`, `json_path_extractor`, `nested_field_extractor`, `xml_parser`, `html_parser`, `regex_parser` | Pull structured fields from semi-structured columns |

### AI / NLP — optional OpenAI (works without; better with)

These demos run **without** `OPENAI_API_KEY` set (skipping the OpenAI-touching components) AND **with** it set (full LLM features). Cost is near-zero when the key is set.

| Demo | Components | Cost with key |
|---|---|---|
| [Local NLP Mega-Demo](local_nlp.md) | 9 truly-local NLP components + 4 OpenAI-using ones (`schema_fit`, `precision_match`, `ticket_classifier`, `sql_generator`) — optional | ~$0.05 (shared gpt-4o-mini calls) |
| [Content Moderation](content_moderation.md) | `moderation_scorer` (local) + optional `text_moderator` (OpenAI /moderations) | $0 — OpenAI moderation endpoint is free |

### AI / NLP — requires an LLM provider key

Most demos below default to OpenAI's `gpt-4o-mini` (cheapest, most permissive rate limits). A few are Anthropic-native or multi-provider by design. If you'd rather run an "OpenAI (default)" demo against Anthropic / Gemini / Groq / etc., swap the LLM component in its `defs.yaml` for `litellm_inference_asset` (any LiteLLM-supported provider) or `vercel_ai_gateway_agent` (any provider behind a Vercel key) — one-line change. See the multi-provider rows at the bottom of this table for the swap patterns.

| Demo | Components | Provider | Why the key is required |
|---|---|---|---|
| [NLP Utilities](nlp_utilities.md) | 6 standalone transforms — `document_chunker`, `word_cloud`, `part_of_speech_tagger`, `topic_modeler`, `text_similarity` | OpenAI (default; swappable via LiteLLM) | The `synthetic_data` source uses gpt-4o-mini to generate test articles; the 6 utilities downstream are local |
| [LangGraph Agent](langgraph_agent.md) | `langgraph_agent` | OpenAI (default; swappable via LiteLLM) | Multi-step LangGraph `StateGraph` (plan → research → critique → synthesize) as a single Dagster asset. Each step is a real OpenAI call; supports conditional routing via `condition_regex`. Live-validated 2026-07-02. |
| [dbt + LLM + dbt (mid-DAG generative AI)](dbt_llm_pipeline.md) | `dagster_dbt.DbtProjectComponent` (×2 with `op.name` split), `langchain_chain_asset`, `duckdb_pandas_io_manager` | OpenAI (default; swappable via LiteLLM) | LLM version of the [dbt + ML pipeline](dbt_ml_pipeline.md) — a LangChain asset generates personalized retention emails between the dbt models. `parse_json: true` expands the structured LLM output into DataFrame columns the marts model can select by name. ~$0.01/run on gpt-4o-mini. |
| [PII detection + LLM redaction check](pii_redaction.md) | `synthetic_data_generator`, `pii_detector`, `pii_redactor`, `langchain_chain_asset` | OpenAI (default; swappable via LiteLLM) | Presidio flags PII (PERSON / EMAIL / SSN / etc.) + redactor scrubs, then a gpt-4o-mini pass double-checks the redacted output for anything missed. Two-pass compliance: statistical detection + LLM as fresh eyes. ~$0.005/run. Live-validated. |
| [Data Quality agent — anomaly + LLM explanations](data_quality_agent.md) | `synthetic_data_generator`, `anomaly_detection`, `filter`, `langchain_chain_asset` | OpenAI (default; swappable via LiteLLM) | Statistical anomaly detection + LLM writes a plausible business reason + concrete follow-up check per flagged row. Two-pass DQ: the LLM turns "z-score 4.2" into a queue of pre-thought-out investigation notes for on-call. ~$0.005/run. Live-validated. |
| [Data Doctor — agent-directed DQ remediation](data_doctor.md) | `synthetic_data_generator` (with `inject_dq_issues`), `dataframe_describe`, `langchain_chain_asset`, **`data_remediation_asset`** | OpenAI (default; swappable via LiteLLM) | The **agent decides what the pipeline does.** LLM reads each column's DQ profile and picks ONE action from a bounded, safe list (drop_nulls / fill_median / clip_outliers / dedup / …). `data_remediation_asset` executes the picks. Full audit trail: every action + reason lands in asset metadata. ~$0.005/run. Live-validated. |
| [Adaptive Triage Router — LLM-driven per-row routing](adaptive_triage.md) | `synthetic_data_generator` (support_tickets), `langchain_chain_asset`, `router`, `dataframe_to_csv` | OpenAI (default; swappable via LiteLLM) | LLM classifies each ticket into billing / bug / churn_risk / spam / other; `router` fans out to per-route downstream sinks. Multi-lingual (validated with EN/ES/DE/FR tickets). ~$0.005/run. 100% existing components. Live-validated. |
| [Adaptive Backfill Detective — LLM-driven per-partition gap response](adaptive_backfill.md) | `synthetic_data_generator` (sparse_sensors), `formula`, `summarize`, `langchain_chain_asset`, `router`, `dataframe_to_csv` | OpenAI (default; swappable via LiteLLM) | Per-partition analysis of IoT sensor gaps. LLM reads each (sensor, day)'s reading count + avg temp and picks ok / interpolate / re_ingest / escalate. Bounded action space, full audit trail. 100% existing components. Live-validated. |
| [Supervisor Agent — LLM picks which agents to call](supervisor_agent.md) | **`supervisor_agent`** (new — one YAML block emits planner + N tools + synthesis) | OpenAI (default; swappable via LiteLLM) | The "agent of agents" pattern. Planner LLM picks from a bounded YAML-declared tool set (web_search / kb_expert / math_expert / translator / critic); each pick fans out to its own per-tool Dagster asset (LLM persona); synthesizer LLM writes the final grounded answer with inline tool citations. Validated with a French pricing question — planner picked 3 of 5 tools, synthesizer answered in French. ~$0.02/run. Live-validated. |
| [MCP Tool Picker — LLM picks which MCP tools to call](mcp_tool_picker.md) | **`mcp_tool_picker`** (new — one YAML block emits planner + N MCP-tool assets + synthesis) | OpenAI (default; swappable via LiteLLM) | Same shape as Supervisor Agent, but each tool is a **real MCP call** (stdio / http / sse). Demo runs against `@modelcontextprotocol/server-filesystem` (npx). Planner picks tool + args → per-tool assets invoke MCP → synthesizer grounds the answer. Live-validated with 2 tool picks. ~$0.01/run. |
| [Adaptive Research Brief — LLM decides how many things to research](adaptive_research_brief.md) | **`adaptive_research_brief`** (new — one YAML block emits plan + notes + brief) | OpenAI (default; swappable via LiteLLM) | Where DynamicOutput / dynamic partitions fits. Planner LLM decides N sub-topics per run based on topic scope; row-wise researcher LLM writes a note per subtopic; synthesizer LLM emits a markdown brief with headings + executive summary. Live-validated with N=5 for an Anthropic competitive brief. ~$0.02/run. |
| [Iterative Supervisor Agent — chained tool use with per-step lineage](iterative_supervisor_agent.md) | **`iterative_supervisor_agent`** (new — one YAML block emits N step assets + synthesis) | OpenAI (default; swappable via LiteLLM) | The ReAct loop as N real Dagster assets. Planner runs every step, sees prior tool outputs, picks next tool OR `done`. Static DAG (max_iterations step assets pre-declared), dynamic termination via short-circuit. Chaining companion to Supervisor Agent — fixes the placeholder-in-args limitation. Live-validated: chained math_expert(149*12) → 1788 → writer with the real number → done. ~$0.02/run. |
| [Catalog Agent — meta-agent over the live 900-component registry, with schema discovery](catalog_agent.md) | **`catalog_agent`** (new — one YAML block emits N step assets + synthesis) | OpenAI (default; swappable via LiteLLM) | The apex. Per-step planner picks REAL components from the live 900-component manifest via reflection + `dg.materialize()` in-process; sees the ACTUAL columns/preview of every prior step's real DataFrame; handles customer data with unknown schemas. Also: multi-source picks (`dataframe_join`'s left+right upstream keys wire automatically), self-correction on planning errors (dangling/self-dep upstreams surface to the next step so the planner course-corrects), and vague-task interpretation (`"group by month"` when only a timestamp exists auto-inserts a `formula` step). **Setup script ships TWO demos:** a simple 3-step schema-discovery pipeline, and a 7-step multi-source join pipeline driven by a natural-language task (*"generate synthetic orders + customers, join, group by first name, email, month, sum total, count orders, store to csv"*) — no wiring hints, agent figures it out. Set `max_iterations: 1` for single-shot behavior. ~$0.02/run. |
| [Planned Catalog Agent — state-backed catalog_agent, LLM plans once, real assets forever](planned_catalog_agent.md) | **`planned_catalog_agent`** (new — `dg.StateBackedComponent` variant of catalog_agent) | OpenAI (default; swappable via LiteLLM) | Same LLM planner + real materialization trajectory, but run ONCE at prepare time (`write_state_to_path`) and cached via Dagster's native `LOCAL_FILESYSTEM` state. Every subsequent load reads the cached plan and emits REAL Dagster assets (no `step_N` wrappers, no synthesis wrapper) — zero LLM cost per run. State key includes a hash of the task string, so changing the task re-plans on the next `dg utils refresh-defs-state`. Set `refresh_if_dev: false` for deterministic "cache is source of truth" behavior. Perfect for the *"input a task in the Dagster+ UI, real assets appear"* flow. Live-validated: 62s prepare (one trajectory), 2.85s subsequent loads (pure cache read), materialize succeeds end-to-end. ~$0.02 once. |
| [Anthropic Claude](anthropic.md) | `synthetic_data_generator` → `anthropic_llm` | **Anthropic (native)** | Direct `anthropic_llm` component wrapping the Claude API. Native Anthropic SDK — swap providers by replacing `anthropic_llm` with `litellm_inference_asset` or `openai_llm` in the `defs.yaml`. |
| [Vision pipeline](vision_pipeline.md) | `synthetic_image_generator`, `image_metadata_extractor`, `vision_model` | **Anthropic (native — Claude vision)** | Vision component uses Claude's vision endpoint. Image metadata + LLM description as two chained assets. |
| [LiteLLM Multi-Provider](litellm_multi_provider.md) | `litellm_inference_asset` (× 2 providers side-by-side), `synthetic_data_generator`, `dataframe_join`, `dataframe_to_csv` | **Any (LiteLLM: OpenAI / Anthropic / Gemini / Groq / Bedrock / …)** | Point at as many providers as you have keys for; runs each independently and joins outputs for side-by-side comparison. Validated Gemini + OpenAI. |
| [Vercel AI Gateway Agent](vercel_ai_gateway_agent.md) | `vercel_ai_gateway_agent` | **Any (single Vercel `vck_*` key)** | One Vercel gateway key routes to OpenAI / Anthropic / Google / xAI / Groq / etc. via `<provider>/<model>` strings, with optional fallback chain. Live-validated across three providers. |

### Semantic layer (Cube)

Governed metrics as first-class Dagster assets, plus the "Cube as LLM safety layer" pattern.

| Demo | Components | Highlights |
|---|---|---|
| [Cube — simple query](cube_query.md) | `cube_query_asset`, `external_cube_metric` | Docker-local Cube dev server with a sample `Orders` cube; two `CubeQueryAssetComponent` queries (summary + by-status) materialize as Dagster DataFrames. **$0, no keys.** Live-validated 2026-07-06. |
| [Cube + LLM (semantic layer for AI)](cube_llm.md) | `cube_query_asset`, `langchain_chain_asset` | Cube gives the LLM a governed, typed interface — no raw SQL, no hallucinated columns. Query customer totals via Cube, then gpt-4o-mini narrates each row in natural language. **Requires OPENAI_API_KEY.** Live-validated. |

### Cloud observability + enterprise SaaS

| Demo | Components | Highlights |
|---|---|---|
| [AWS CloudWatch](aws_cloudwatch.md) | `cloudwatch_metrics_query`, `cloudwatch_logs_insights` | Pull metrics + Logs Insights query results into a DataFrame |
| [New Relic + Dynatrace](newrelic_dynatrace.md) | `newrelic_event_sink`, `dynatrace_metric_sink` | Emit pipeline-row events to APM SaaS |
| [OpenTelemetry Full-Stack](opentelemetry.md) | `otel_metrics_emitter`, `otel_logs_emitter`, `otel_traces_emitter` | Metrics + logs + traces in one demo |
| [Prometheus](prometheus.md) | `prometheus_push_gateway`, `prometheus_query_asset` | Push + pull patterns side by side |
| [Enterprise SaaS Resources](enterprise_saas.md) | `workday_resource`, `marketo_resource`, `intercom_resource`, `plaid_resource` | Declare 4 SaaS APIs in one code location |
| [SAP HANA via SQLAlchemy](sap_hana.md) | `dataframe_to_table` (mssql adapter pattern) | SAP HANA via SQLAlchemy |
| [Precisely Connect ETL](precisely_validation.md) | `precisely_job_sensor` | Sensor-only — Precisely owns the run, Dagster fires `RunRequest` on terminal SUCCESS via the documented Job Status endpoint |
| [Compute Log Managers — Splunk + OTel](compute_log_managers.md) | `SplunkComputeLogManager`, `OtlpComputeLogManager`, `TeeComputeLogManager` | **Instance-level** infra (`dagster.yaml`) — not a defs.yaml component. Routes op stdout/stderr to Splunk HEC + OTel Collector in parallel via Tee. Live-validated: 22 events on each path. |
| [Vercel Deployment](vercel_deployment.md) | `vercel_deployment_sensor`, `external_vercel_deployment` | Poll Vercel `/v6/deployments` for terminal READY, emit `AssetObservation` with commit SHA / branch / URL. Downstream Dagster assets gate on production being live. Live-validated. |

### Durable workflow orchestration (Temporal)

Dagster observes and interacts with Temporal — a durable-execution engine built for long-running workflows. Four modes covered:

| Demo | Components | Highlights |
|---|---|---|
| [Temporal Workflow (trio: trigger + external + sensor)](temporal_workflow.md) | `temporal_workflow_trigger`, `external_temporal_workflow`, `temporal_workflow_sensor` | Full E2E via local `temporal server start-dev` + a real Python worker. Dagster asset starts a Temporal workflow that fetches a Star Wars planet from swapi.dev via a Temporal activity; sensor observes the completed workflow. Live-validated. |
| [Temporal Signal + Query](temporal_signal_query.md) | `temporal_signal_asset`, `temporal_query_asset` | Push state INTO / pull state OUT of a long-lived running workflow. Scripted `query → signal add → query → signal flush → query` sequence against an `OrderBatchWorkflow` with `@signal` + `@query` handlers. Live-validated 2026-07-02. Completes the four-mode Dagster ↔ Temporal integration alongside the trio. |

## Azure (subscription required)

Each Azure demo lists exact provisioning commands and teardown. Costs are noted
per-demo; all of these stay well under the free monthly allotments for personal
subscriptions.

### Storage + lakehouse

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [ADLS Round-Trip](adls_round_trip.md) | `dataframe_to_adls`, `external_adls_asset` | 1 storage account + container | <$0.05/mo |
| [ADLS Inbox](adls_inbox.md) | `adls_monitor` (sensor) → `asset_job` → `adls_to_database_asset` | same storage account, files in `demo/inbox/` | <$0.05/mo |
| [Bicep Self-Provision](bicep_self_provision.md) | `bicep_asset` provisions storage; downstream uses it | none upfront — Bicep creates it | <$0.05/mo |

### Databases

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Cosmos DB Round-Trip](cosmosdb_round_trip.md) | `cosmosdb_writer` → `cosmosdb_reader` → `dataframe_to_csv` | Cosmos DB account (free tier) | $0 |
| [Azure SQL Database](azure_sql.md) | `synthetic_data_generator` → `dataframe_to_table` (mssql+pymssql) | SQL Server + serverless DB | <$0.05/mo idle |
| [Azure PostgreSQL Flexible](azure_postgres.md) | `synthetic_data_generator` → `dataframe_to_table` (postgresql+psycopg2) | Flexible Server B1ms | ~$13/mo |
| [Azure MySQL Flexible](azure_mysql.md) | `synthetic_data_generator` → `dataframe_to_table` (mysql+pymysql) | Flexible Server B1ms | ~$13/mo |
| [Azure Cache for Redis](azure_redis.md) | `redis_writer` (TLS) → `redis_reader` (TLS) → `dataframe_to_csv` | Cache Basic C0 | ~$16/mo |
| [Azure Data Explorer (Kusto)](azure_data_explorer.md) | `adx_query_asset`, `dataframe_to_adx` | ADX cluster (Free tier OK) | $0 free tier |
| [Azure AI Search](azure_search.md) | `synthetic_data_generator`, `azure_search_indexer`, `azure_search_query`, `dataframe_to_csv` | Azure AI Search Free F1 | $0 free tier |
| [Azure Tables](azure_tables.md) | `synthetic_data_generator`, `dataframe_to_azure_table`, `azure_table_reader`, `dataframe_to_csv` | Storage account + tables | <$0.05/mo |
| [Azure Key Vault](key_vault.md) | `synthetic_data_generator`, `key_vault_resource`, `dataframe_to_table` | Key Vault standard | $0.03/10k operations |

### Orchestration + workflow

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Azure Data Factory](azure_data_factory.md) | `azure_data_factory` (import + trigger ADF pipelines, capture per-activity metadata) | ADF instance + service principal | $0 idle, $0.001/activity |
| [Azure Synapse Analytics](azure_synapse.md) | `azure_synapse` (import + trigger Synapse pipelines; Spark/notebook discovery) | Synapse workspace + ADLS Gen2 storage + service principal | $0 idle, free serverless SQL <1TB/mo |
| [Synapse Serverless SQL (OPENROWSET)](azure_synapse_serverless.md) | `dataframe_to_adls` → `dataframe_from_sql` (no Synapse-specific component needed!) | Same Synapse workspace + a demo db with master key + db-scoped credential + external data source | $0 — first 1TB/mo scanned is free |

### Streaming + queues

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Azure Event Hubs Round-Trip](azure_eventhubs.md) | `dataframe_to_eventhub` (NEW) → `eventhubs_to_database_asset` → Postgres | EH Basic namespace + hub | ~$11/mo + $0.028/M events |
| [Azure Service Bus](azure_servicebus.md) | `dataframe_to_servicebus`, `servicebus_to_database_asset` | Service Bus Basic namespace + queue | ~$0.05/M operations |

### Microsoft Fabric (next-gen Synapse)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Fabric Full-Stack](fabric_full_stack.md) | All 6 Fabric components: `fabric_workspace`, `fabric_workspace_resource`, `fabric_lakehouse_resource`, `fabric_lakehouse_io_manager`, `dataframe_to_fabric_lakehouse`, `fabric_pipeline_trigger_job` (+ existing `dataframe_from_sql` for the Warehouse SQL endpoint) | F2 capacity + workspace + Lakehouse + Warehouse | ~$0.21/hr ($154/mo always-on) |

### Observability

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Dagster+ → Sentinel](dagster_plus_to_sentinel.md) **(Dagster+ + Azure)** | `dagster_plus_audit_log_ingestion` → `ocsf_normalizer` → `audit_logs_to_sentinel` | Log Analytics workspace | $0 (5GB/mo free tier) |
| [Azure Log Analytics KQL](azure_log_analytics.md) | `log_analytics_query_asset` | Log Analytics workspace | $0 (5GB/mo free) |

**Validated end-to-end** against a real Azure subscription with a real
Dagster+ deployment. Examples include:
- Sentinel: 176 production audit-log entries → OCSF v1.1 normalize → all 176 landed in `DagsterPlusAudit_CL`
- ADF: `demo_wait_pipeline` triggered, polled Queued→InProgress→Succeeded in 15s, per-activity metadata captured
- Cosmos: 50 orders upserted, 9 high-value rows queried back, CSV report written
- Azure SQL: 100 rows landed in `dbo.orders`, top-5 verified via `SELECT TOP`
- Postgres / MySQL Flexible: 100 rows landed in each in <4s
- Cache for Redis: 30 rows HSET via TLS:6380, read back, CSV report
- Event Hubs: 100 events published in 1.37s, consumer drained 200 events into Postgres in 6.12s
- Synapse: workspace pipeline triggered, polled Queued→Succeeded in 35s, run metadata captured
- Synapse Serverless: parquet on ADLS → OPENROWSET → 7-row aggregation in 3.27s ($0 — free tier)

### Auth: managed identity in Azure compute

When running these in **Azure Container Apps** or **AKS** with a managed
identity attached, you can omit the env-var auth entirely. The Azure
components use `DefaultAzureCredential`, which falls back through
env vars → managed identity → `az login` automatically. Local development
uses env vars; production in Azure compute uses the attached identity.

---

## Google Cloud (GCP, subscription required)

Each GCP demo is validated end-to-end against a real GCP project. Auth via
service-account JSON pointed at by `GOOGLE_APPLICATION_CREDENTIALS`.
Most APIs need to be enabled per-project on first use; the components
surface the exact activation URL on the first call. Costs are noted
per-demo; everything below stays well under the free tier for typical
demos.

### Workspace (Drive, Docs, Sheets, Calendar)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Google Sheets](google_sheets.md) | `google_sheets_ingestion` (single-component demo) | SA shared on a sheet, Sheets API enabled | $0 |
| [Google Calendar](google_calendar.md) | `google_calendar_ingestion` → `dataframe_to_csv` + `dataframe_to_bigquery` | SA shared on a calendar, Calendar API enabled | $0 |
| [Drive + Docs + Gemini](google_drive_docs.md) | `google_drive_ingestion` → `google_docs_extractor` → `gemini_llm` → CSV | SA shared on a Drive folder, Drive + Docs + Generative Language APIs enabled | $0 |

### Warehouse (BigQuery)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [BigQuery Query](bigquery_query.md) | `bigquery_query_asset` against `bigquery-public-data.samples.shakespeare` (single-component demo) | BigQuery API enabled, `roles/bigquery.jobUser` | $0.0001 |
| [BigQuery ML Pipeline](bigquery_ml_pipeline.md) | `bigquery_create_table_from_query_asset` (CTAS) → `bigquery_ml_train_asset` (LOGISTIC_REG) → `bigquery_ml_predict_asset` → CSV | BQ dataset, `roles/bigquery.dataEditor` + `jobUser` | <$0.001 |
| [BigQuery ↔ GCS Bulk Bridge](bigquery_bulk_bridge.md) | `bigquery_export_to_gcs_asset` (EXTRACT) → `bigquery_load_from_gcs_asset` (LOAD JOB) — round-trip | BQ dataset + GCS bucket | $0 (extract + load free) |
| [BigQuery Asset Checks](bigquery_checks.md) | `external_bigquery_table`, `bigquery_dry_run_check`, `bigquery_table_freshness_check` | BQ dataset | $0 (dry-runs free) |
| [BigQuery Vector Search](bq_vector_search.md) | `bigquery_vector_search_asset` | BQ with vector column | $0 with free tier |
| [GCS Round-Trip](gcs_roundtrip.md) | `synthetic_data_generator`, `dataframe_to_gcs`, `bigquery_load_from_gcs_asset`, `bigquery_export_to_gcs_asset` | GCS bucket + BQ dataset | $0 (extract + load free) |

### AI / LLM

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Gemini LLM](gemini_llm.md) | `synthetic_data_generator` → `gemini_llm` (gemini-2.5-flash) | Gemini API key | $0 (free tier 5 RPM) |
| [Nano Banana](nano_banana.md) | `synthetic_data_generator` (image_prompts) → `gemini_image_generation` (gemini-2.5-flash-image) | Gemini API key, billing enabled (image gen requires it) | ~$0.01–$0.10 |
| [Vertex AI Embeddings](vertex_ai_embeddings.md) | `synthetic_data_generator` (image_prompts) → `vertex_ai_text_embeddings_asset` (text-embedding-004) → `dataframe_to_gcs` (parquet) | Vertex AI API enabled, `roles/aiplatform.user` | $0 (under free tier) |
| [Vision + Translation](vision_translate.md) | `synthetic_image_generator` → `vision_api_asset` (LABEL + OBJECT) → `dataframe_extract_field` (top label) → `translation_api_asset` (es/fr/de/ja) → `dataframe_to_csv` | Vision + Translation APIs enabled | ~$0.005 |
| [Speech + Translation](speech_translate.md) | `synthetic_data_generator` (audio_samples) → `speech_to_text_asset` (Cloud Speech v2) → `translation_api_asset` (es/fr/de/ja) → `dataframe_to_csv` | Speech + Translation APIs enabled | ~$0.001 |
| [Anthropic Claude](anthropic.md) | `synthetic_data_generator`, `anthropic_llm` | Anthropic API key | usage-priced |
| [Gemini LLM](gemini_llm.md) | `synthetic_data_generator`, `gemini_llm` | Gemini API key, billing enabled | $0 free tier / usage |
| [LiteLLM Multi-Provider](litellm_multi_provider.md) | `litellm_inference_asset`, `synthetic_data_generator`, `dataframe_to_csv`, `dataframe_join` | API key for at least one provider | usage-priced |
| [Image Generation (LiteLLM-routed)](image_generation.md) | `synthetic_data_generator` (image_prompts) → `litellm_image_generation` | DALL-E 3 via OpenAI (default) — swaps to Stability / Imagen / Replicate / Bedrock / Nano Banana via one YAML change | ~$0.12 (3× 1024×1024 DALL-E 3) |
| [LLM Execution Mega-Demo](llm_execution.md) | 13 LLM components — OpenAI / LiteLLM / prompt-executor / batch / etc. | OpenAI API key | usage-priced |
| [LiteLLM Agent + MCP](litellm_agent.md) | `litellm_agent` with the official `@modelcontextprotocol/server-filesystem` server as tool layer | OpenAI API key + `npx` | ~$0.0005/run |
| [Agent family (3 shapes of MCP use)](agent_family.md) | `mcp_tool_call` + `openai_agent` + `llm_evaluator` — deterministic, agentic, and evaluated, all on the same MCP server | OpenAI API key + `npx` | ~$0.0005/run |
| [Vector / RAG](vector_rag.md) | `embeddings_generator`, `vector_store_writer`, `vector_store_query`, `reranker`, `rag_pipeline`, `conversation_memory` | Vector store + embeddings model | usage-priced (~$0.0001/doc) |
| [AI with LLMs](ai_with_llm.md) | `synthetic_data_generator`, `text_classifier`, `entity_extractor`, `sentiment_analyzer`, `document_summarizer`, `data_enricher` | OpenAI / Azure OpenAI key | usage-priced |
| [Multi-modal AI](multimodal_ai.md) | `image_captioner`, `image_llm_extractor`, `litellm_embedding_batch` | OpenAI API key (vision-capable) | usage-priced |
| [HuggingFace (full surface)](huggingface.md) | `huggingface_pipeline`, `huggingface_dataset_asset`, `huggingface_model_asset`, `huggingface_inference_endpoint`, `huggingface_space_status_sensor` | None for local mode + observation assets; `HF_TOKEN` for Inference API + dedicated endpoints | $0 local / usage-priced API / endpoint-hour billing for dedicated |

### Real-pipeline patterns (multi-component chains)

| Demo | Components exercised | Highlights |
|---|---|---|
| [HRIS Normalizer](hris_normalizer.md) | `synthetic_data_generator` (employees) → `hris_normalizer` (vendor-agnostic) → `dataframe_to_csv` | Synthetic vendor export → canonical schema mapped via `value_maps` (case-insensitive: `Active`/`active`/`ACTIVE` → `active`; `Full-Time`/`FT`/`FULL_TIME` → `full_time`) |
| [Bigtable Round-Trip](bigtable_roundtrip.md) | `synthetic_data_generator`, `bigtable_writer_asset`, `bigtable_reader_asset` | Bigtable instance + table | $0.65/hr (dev instance) |
| [Cloud DLP (PII detection)](cloud_dlp.md) | `synthetic_data_generator`, `cloud_dlp_inspect_asset`, `cloud_dlp_pii_check` | DLP API enabled | usage-priced |
| [Cloud Tasks Fan-out](cloud_tasks_fanout.md) | `synthetic_data_generator`, `cloud_tasks_enqueue_asset` | Cloud Tasks queue | $0 free tier |
| [Document AI (OCR)](document_ai.md) | `synthetic_pdf_generator`, `document_ai_extractor` | Document AI API + processor | $1.50/1k pages |
| [Firestore Round-Trip](firestore_roundtrip.md) | `synthetic_data_generator`, `firestore_writer_asset`, `firestore_reader_asset` | Firestore database | $0 free tier |
| [GCP Observability Snapshot](gcp_observability_snapshot.md) | `cloud_logging_query_asset`, `cloud_monitoring_metrics_asset`, `dataframe_flatten_nested_columns`, `dataframe_to_bigquery` | Cloud Logging + Monitoring access | $0 |
| [Pub/Sub Publish](pubsub_publish.md) | `synthetic_data_generator`, `pubsub_publish_asset` | Pub/Sub topic | $0 free tier |

### Auth: workload identity in GCP compute

When running these in **GKE / Cloud Run / Compute Engine / Cloud Functions**
with a service account attached, you can omit the env-var auth entirely.
The Google client libraries fall back through `GOOGLE_APPLICATION_CREDENTIALS`
→ workload identity → `gcloud auth application-default` automatically.
Local development uses the env var; production in GCP compute uses the
attached SA.

**Validated end-to-end** against a real GCP project. Examples include:
- BigQuery ML: 150-row iris → CTAS → LOGISTIC_REG model trained in BQ in 1m36s → 10 predictions returned, setosa @ 99.92% top class
- BigQuery bridge: 150-row table → EXTRACT to GCS parquet (2.6 KB compressed) → LOAD back to a new BQ table → identical row count + schema, ~8s round-trip
- Calendar: 60 events from `ethomasii@gmail.com` next 30 days → CSV + BQ table
- Drive + Docs + Gemini: 1 Doc, 661 words extracted → real Gemini summary → CSV in <6s
- Sheets: 2 rows pulled live from a shared spreadsheet
- Vertex embeddings: 5 product descriptions → 5 × 768-dim vectors via `text-embedding-004`, all distinct
- Vision + Translation: 3 synthetic PNGs → labels (`Red`, `Blue`, `Clip art`) → translated to es/fr/de/ja (赤 / 青 / クリップアート)
- Speech + Translation: `brooklyn_bridge.mp3` → "How old is the Brooklyn Bridge?" → translated to es/fr/de/ja (`ブルックリン橋は何年前にできたのですか？`)

---

## Snowflake (subscription required)

Each Snowflake demo runs against a real Snowflake account. Auth via
`SNOWFLAKE_ACCOUNT` + `SNOWFLAKE_USER` + (password / PAT / keypair) +
`SNOWFLAKE_WAREHOUSE` + `SNOWFLAKE_ROLE`. The booth demo is the primary
shape; single-entity components are the companion when you want to wire
one named task / proc / dynamic table by hand.

### Primary — Snowflake workspace (booth demo)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [**Snowflake Workspace → Dagster**](snowflake_workspace.md) **(booth demo)** | `snowflake_workspace` + 11 optional add-ons (`warehouse_pipeline`, `snowpark_pipeline`, `snowflake_cortex_asset`, `snowflake_cortex_search`, `snowflake_iceberg_table`, `snowflake_time_travel_asset`, `snowflake_snowpipe_load_sensor`, partitioned heterogeneous chain, `dagster-dbt`, 7 DDL components, freshness checks, external table refs) | Snowflake account (any edition; the script auto-detects capabilities and only scaffolds what your tier supports) | Snowflake credits to materialize |
| [Snowflake account requirements](snowflake_demo_account_requirements.md) | Reference doc — what each privilege / tier unlocks; the booth demo's capability probes; the SECURITY_ASK.md auto-output | — | — |

### Companion — single-entity components (manual selection)

| Demo | Components exercised | Infra needed |
|---|---|---|
| [Snowflake single-entity components](snowflake_single_entity.md) | `snowflake_task_execute_asset`, `snowflake_stored_procedure_call_asset`, `snowflake_dynamic_table_refresh_asset`, `snowflake_task_completion_sensor`, `external_snowflake_openflow_flow`, `snowflake_openflow_status_sensor` | Same Snowflake account, but only the privileges for the named entities |

Use this companion when you want to wire one specific task / proc / DT by hand instead of scanning the whole account. Same auth surface; finer-grained control over `deps`, `partitions_def`, `automation_condition` per asset.

### Pushdown / Snowpark

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Snowpark pipeline](snowpark_pipeline.md) | `snowpark_pipeline` | Snowflake account + `RAW.ORDERS` source table | Snowflake credits |

Multi-step Snowpark DataFrame chain compiled to ONE Snowflake SQL statement. Whole pipeline runs server-side in the Snowflake compute warehouse — no data through Python.

### Snowflake as part of a cross-vendor blueprint

| Blueprint | What Snowflake does | Other vendors |
|---|---|---|
| [Snowflake → Iceberg → Databricks Lakeflow](snowflake_iceberg_databricks.md) | Source — Dynamic Iceberg Tables written via `snowflake_workspace` | Databricks Lakeflow (Lakeflow pipelines wrapped in Jobs) |
| [Snowflake → Dagster cross-engine Iceberg](snowflake_to_dagster_iceberg.md) | Source — writes Iceberg via Snowflake-managed catalog | PyIceberg REST client; zero Snowflake compute per Dagster read |

---

## Dagster+ required

| Demo | Pipeline | Highlights |
|---|---|---|
| [Dagster+ Audit → Security Lake](dagster_plus_security_lake.md) | `dagster_plus_audit_log_ingestion` → `ocsf_normalizer` → `ocsf_validator` → Parquet | Asset pipeline with full lineage; local Parquet by default. Validated with 176 real entries. |
| [Deploying any demo to Dagster+](deploy_to_dagster_plus.md) | Meta-walkthrough | How to take any of the demos in this repo and deploy them to Dagster+ (Serverless or Hybrid). Covers project structure, deployment, secrets, and schedules. |

---

## Catalog Lineage Sync — multi-target (no auth required for the file demo)

| Demo | Components used | Highlights |
|---|---|---|
| [Catalog Lineage Sync](lineage_catalogs.md) | `lineage_graph_extractor` (source) → `lineage_to_file` (sink) — swap in `lineage_to_purview`, `lineage_to_datahub`, `lineage_to_openmetadata`, `lineage_to_alation`, `lineage_to_collibra`, `lineage_to_webhook` for real catalogs | Lock-step fan-out across N catalogs; per-sink change-detection skip via payload hashing. Validated locally end-to-end with file sink. |
| [Lineage → DataHub (Docker)](lineage_to_datahub.md) | `lineage_graph_extractor` (source) → `lineage_to_datahub` (sink) — DataHub OSS in Docker | End-to-end validated against DataHub v1.3.0 quickstart. 8 datasets ingested + lineage edges confirmed via GraphQL. |

---

## Cross-vendor blueprints (not validated)

Multi-vendor production patterns. These scaffold the Dagster project + defs.yaml and document the architecture + infra prerequisites, but materialization end-to-end requires real Snowflake / Databricks / cloud accounts — bring your own.

| Blueprint | Pipeline | What you bring |
|---|---|---|
| [Snowflake → Iceberg → Databricks Lakeflow](snowflake_iceberg_databricks.md) | `snowflake_workspace` (Dynamic Iceberg Tables) → object storage (Iceberg files) → official `DatabricksWorkspaceComponent` (Lakeflow pipelines wrapped in Jobs) | Snowflake account with external volume for Iceberg; Databricks workspace with Unity Catalog; the Lakeflow pipeline wrapped in a Databricks Job. Full prereq SQL in the walkthrough. |
| [Event Hubs Capture → ADLS → Dagster → Synapse](eh_capture_pipeline.md) | EH Capture writes Parquet to ADLS → `adls_monitor` (dynamic_partition) → `file_ingestion` → `summarize` → curated Parquet → Synapse Serverless OPENROWSET | Azure Event Hubs namespace with Capture enabled; ADLS Gen2 storage account; optionally Synapse Serverless. Stream→durable-storage→batch — production-shape pattern that solves the "queues aren't re-runnable" problem by landing every event in ADLS first. |
| [Pub/Sub → GCS Subscription → Dagster → BigQuery](pubsub_gcs_pipeline.md) | Pub/Sub Cloud Storage Subscription writes Parquet to GCS → `gcs_monitor` (dynamic_partition) → `file_ingestion` → `summarize` → curated Parquet → BigQuery external table | GCP Pub/Sub topic with a Cloud Storage Subscription; GCS bucket; optionally BigQuery dataset. GCP mirror of the EH Capture pattern. |
| [Kinesis Firehose → S3 → Dagster → Athena](kinesis_firehose_pipeline.md) | Kinesis Firehose writes Parquet to S3 (Glue schema) → `s3_monitor` (dynamic_partition) → `file_ingestion` → `summarize` → curated Parquet → Athena external table | Kinesis Data Stream (or direct-PUT to Firehose), Firehose delivery stream, S3 bucket, Glue table, optionally Athena. AWS mirror of the EH Capture / Pub/Sub patterns. |

---

## SAP family + OData ecosystem (one component covers many vendors)

The OData protocol is the dominant enterprise-ERP machine interface. One `odata_ingestion` component covers SAP S/4HANA, SuccessFactors, Datasphere, Microsoft Dynamics 365 / Dataverse, MS Graph, Oracle Fusion, Epicor, IFS Cloud. Plus the headless-OAuth stack (`oauth_token_resource` + `oauth_rest_ingestion`) for Concur, Ariba, and any other Bearer-token REST API.

| Blueprint | Pipeline | What you bring |
|---|---|---|
| [OData generic — Northwind](odata_pipeline.md) | `odata_ingestion` → `summarize` → `dataframe_to_parquet`. Public `services.odata.org/V4/Northwind`. | Nothing — no credentials, no infra. The fastest way to prove the OData component works. |
| [Dremio (Docker, end-to-end)](dremio_pipeline.md) | `dremio_ingestion` → `summarize` → `dataframe_to_parquet`. Docker Dremio OSS + PAT auth. | Docker Desktop. The setup script handles everything else (~2 min UI clicks for first-user + PAT). |
| [SAP S/4HANA](sap_s4hana_pipeline.md) | `odata_ingestion` against S/4HANA — three modes documented: API Business Hub sandbox (free signup), Cloud basic auth (Communication User), Cloud OAuth (XSUAA). Plus `dataframe_to_odata` for write-back with CSRF handling. | Free SAP account at api.sap.com (sandbox); OR an S/4HANA tenant. |
| [SAP SuccessFactors](sap_successfactors_pipeline.md) | `odata_ingestion` against SuccessFactors HRIS — employees, jobs, departments, comp, performance reviews. `User@CompanyID` basic auth or SAML-assertion OAuth. | SuccessFactors API user credentials. |
| [SAP Concur](sap_concur_pipeline.md) | `oauth_token_resource` (refresh_token grant + writeback rotation) → `oauth_rest_ingestion` (next_url pagination). Full headless flow with AWS SM / Azure KV / Vault writeback patterns documented. | Concur OAuth app credentials. |
| [SAP Ariba](sap_ariba_pipeline.md) | `oauth_token_resource` (client_credentials — easy headless) → `oauth_rest_ingestion` (cursor pagination). Operational Reporting / Sourcing / Supplier endpoints. | Ariba Developer Portal app with the right scopes. |
| [SAP Datasphere](sap_datasphere_pipeline.md) | `odata_ingestion` over Datasphere consumption APIs (analytic models). XSUAA OAuth flow. | Datasphere tenant + Space + OAuth client. |
| [SAP HANA](sap_hana_pipeline.md) | `sap_hana_ingestion` for direct SQL — Cloud + on-prem + Calculation Views in `_SYS_BIC`. Pairs with `sap_hana_resource`. | HANA Cloud or on-prem tenant. |
| [Microsoft Dynamics 365 / Dataverse](dynamics365_pipeline.md) | `odata_ingestion` v4 against Dataverse. Azure AD app permissions + workload identity option. | Azure AD app registration with Dataverse access. |
| [Microsoft Graph](msgraph_pipeline.md) | `odata_ingestion` v4 against `graph.microsoft.com`. Users, mail, calendar, Teams, OneDrive, SharePoint. App permissions + workload identity. | Azure AD app registration with Graph application permissions. |
| [SAP RFC (R/3 / ECC / on-prem S/4)](sap_rfc_pipeline.md) | `sap_rfc_resource` + `sap_rfc_ingestion`. Two modes: `read_table` (RFC_READ_TABLE → DataFrame for MARA/KNA1/BSEG/etc.) and `bapi` (any BAPI / Z-RFC). The protocol for on-prem SAP without OData. | SAP-licensed NW RFC SDK install + RFC service user + network access to the SAP gateway. |
| [SAP Event Mesh (event-driven)](sap_event_mesh_pipeline.md) | `sap_event_mesh_sensor` polls Event Mesh queues, registers a dynamic partition per message. Real-time-ish: latency bounded by interval. The right shape for S/4HANA business events / SuccessFactors events. | BTP Event Mesh service instance + OAuth client + queue subscriptions to S/4 topics. |
| [SAP CPI / Integration Suite (iFlow observability)](sap_cpi_pipeline.md) | `sap_cpi_observation_sensor` polls Message Processing Logs → AssetObservations. Pair with `oauth_rest_ingestion` for the trigger side. | Integration Suite OAuth client with `MonitoringDataRead` scope. |
| [SAP IBP (Integrated Business Planning)](sap_ibp_pipeline.md) | `odata_ingestion` against IBP's EXTRACT_ODATA_SRV — forecast key figures, master data, planning views. | IBP tenant + Communication User. |
| [SAP Commerce Cloud (Hybris) OCC](sap_commerce_cloud_pipeline.md) | `oauth_rest_ingestion` against OCC REST APIs — orders / products / customers / carts. Page-based pagination. | Commerce Cloud OAuth client. |
| [SAP Marketing Cloud (incl. Emarsys)](sap_marketing_cloud_pipeline.md) | `odata_ingestion` for CUAN_* services — contacts, interactions, campaigns, consent records. | Marketing Cloud Communication User. |
| [SAP MDG (Master Data Governance)](sap_mdg_pipeline.md) | `odata_ingestion` for MDG_BS_BP_API_SRV (Business Partners), MDG_BS_MAT_API_SRV (Materials), MDG_CHANGE_REQUEST_API_SRV (workflow). Read golden records + observe CR workflow. | MDG running on S/4 or ECC + Communication User. |
| [SAP Fieldglass (contingent workforce)](sap_fieldglass_pipeline.md) | `oauth_rest_ingestion` for workers / SOWs / timesheets / invoices. Page-based pagination. | Fieldglass OAuth client (refresh-token grant — uses writeback rotation). |
| [SAP Analytics Cloud (SAC)](sap_analytics_cloud_pipeline.md) | `oauth_rest_ingestion` + custom asset for SAC's REST APIs. Push data INTO SAC models is the common pattern. | SAC OAuth client with `Public_API` scope. |
| [SAP BW / BW/4HANA](sap_bw_pipeline.md) | Open Hub Destination → HANA table → `sap_hana_ingestion`, OR Open Hub → file → `file_ingestion`. The canonical BW extraction pattern. | BW system + Open Hub Destination configured + DB-or-file output. |

---

## Lakehouse — external Iceberg + Delta tables (cross-engine)

Read from and write to Iceberg / Delta tables owned by **other engines** — Snowflake, Trino, Spark, Flink, Databricks. The official `dagster_iceberg` / `dagster_deltalake` packages only ship IO managers (Dagster owns the table); these blueprints cover the cross-engine pattern using `pyiceberg` and `delta-rs` directly. No Spark / no JVM.

| Blueprint | Pipeline | What you bring |
|---|---|---|
| [Iceberg generic](iceberg_pipeline.md) | `iceberg_catalog_resource` → `iceberg_ingestion` → transform → `dataframe_to_iceberg_table`. Catalog matrix: REST (Polaris / Nessie / Lakekeeper / Tabular / S3 Tables / Snowflake-managed) + Glue + Hive + Hadoop + SQL. | Iceberg catalog credentials + S3/ADLS/GCS storage. |
| [Delta generic](delta_pipeline.md) | `delta_ingestion` → transform → `dataframe_to_delta_table`. Storage: S3 / ADLS / GCS / Unity Catalog / local. Append / overwrite / merge. | Delta table location + storage credentials. |
| [Snowflake → Dagster (cross-engine Iceberg)](snowflake_to_dagster_iceberg.md) | Snowflake writes Iceberg via its managed catalog → Dagster reads via PyIceberg REST. Zero Snowflake compute per read. | Snowflake PAT + external volume IAM role + S3 read access. |
| [Databricks Delta → Dagster](databricks_delta_to_dagster.md) | Databricks writes Delta (UC-managed or HMS) → Dagster reads via delta-rs. Either `uc://` scheme (UC token) or raw `s3://` / `az://` path. | Databricks PAT or OAuth M2M creds. |

---

## How a demo is built

Each demo is a single Bash script (`setup_*.sh`) that:

1. `uvx create-dagster project <name>` — scaffolds a canonical Dagster project
2. `uv add` runtime libs (pyarrow, requests, etc.)
3. `dagster-component add <id> --auto-install` for each component. Class
   files land in `src/<pkg>/components/<id>/`; the configured instance lands
   in `src/<pkg>/defs/<id>/defs.yaml` — the canonical `create-dagster` split
4. Writes a `defs.yaml` per component with demo-specific attributes
5. Prints the run command + an inspect snippet

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_<name>_demo.sh | bash
cd <name>-demo
uv run dg launch --assets '*'
```

## Auth-required demos: comprehensive prereqs

For demos that need credentials, every walkthrough now documents:

- **What to install** (Azure CLI, AWS CLI, etc.)
- **What providers/features to enable** (Microsoft.Storage, Microsoft.OperationalInsights, etc.)
- **What env vars to set** (with the `az` / `aws` commands to fetch them)
- **What it costs** (with realistic numbers for the demo's data volume)
- **How to tear down** (single-command deletion)

If a section is missing from any auth-required walkthrough, file an issue or PR.
