# Examples

End-to-end Dagster pipelines built entirely from community components.
Each demo:

- Runs end-to-end with a single `curl | bash` then `dg launch`
- Lists exactly what it needs (auth, infra, env vars, cost)
- Has been **validated against real systems**, not just type-checked

The demos are grouped by what they need to run.

## Table of contents

- [No auth required (synthetic or public data)](#no-auth-required-synthetic-or-public-data)
  - [Core ETL patterns](#core-etl-patterns)
  - [Time series + forecasting](#time-series--forecasting)
  - [ML pipelines](#ml-pipelines)
  - [Customer + subscription analytics](#customer--subscription-analytics)
  - [Geospatial](#geospatial)
  - [Public APIs (no auth)](#public-apis-no-auth)
  - [OCSF / Security](#ocsf--security)
  - [Op jobs (no asset materialized)](#op-jobs-no-asset-materialized)
  - [Patterns](#patterns)
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
- [Dagster+ required](#dagster-required)
- [Catalog Lineage Sync — multi-target](#catalog-lineage-sync--multi-target-no-auth-required-for-the-file-demo)
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
| [Pivot ↔ Unpivot](pivot_unpivot.md) | `csv_file_ingestion`, `pivot`, `unpivot`, `dataframe_to_csv` | Long↔wide round-trip on monthly sales |
| [Router](router.md) | `csv_file_ingestion`, `router`, `dataframe_to_csv` | Multi-output conditional split (high/medium/low value orders) |
| [Detect Changes](detect_changes.md) | `csv_file_ingestion`, `detect_changes`, `dataframe_to_csv` | CDC: classify rows insert/update/delete/unchanged |
| [SCD Type 2](scd_type_2.md) | `csv_file_ingestion`, `scd_type_2`, `dataframe_to_csv` | History-tracking dimension load |
| [Window Calculation](window_calculation.md) | `csv_file_ingestion`, `window_calculation`, `dataframe_to_csv` | Every supported window function on stock prices |
| [Regional Orders Union](regional_orders.md) | `csv_file_ingestion`, `dataframe_union`, `dataframe_to_csv` | Merge multi-region order extracts with mismatched columns |
| [Data Hygiene](data_hygiene.md) | `synthetic_data_generator`, `audit_columns`, `schema_validator`, `field_mapper`, `map_values`, `data_masking`, `hash`, `surrogate_key`, `record_id`, `count_records` | Toolbox demo: audit / validate / rename / canonicalize / mask / hash / surrogate-key / number / aggregate |

### Time series + forecasting

| Demo | Components | Highlights |
|---|---|---|
| [Airline Passengers Forecast](passengers_forecast.md) | `csv_file_ingestion`, `datetime_parser`, `ets_forecast`, `dataframe_to_csv` | ETS / Holt-Winters time-series forecasting |
| [Forecast Comparison](forecast_comparison.md) | `time_series_generator`, `ts_filler`, `arima_forecast`, `ets_forecast`, `ts_compare`, `dataframe_to_csv`, `cron_schedule` | Head-to-head ARIMA vs ETS on the same series |
| [Stocks — Anomaly Detection](stocks_anomaly.md) | `csv_file_ingestion`, `anomaly_detection`, `dataframe_to_csv` | Per-ticker z-score outlier flagging |
| [Sensor Gap-Fill](sensor_gapfill.md) | `synthetic_data_generator`, `ts_filler`, `running_total`, `dataframe_to_csv` | Fill missing hourly readings + cumulative metrics |
| [Synthetic Metrics + Anomalies](synthetic_metrics.md) | `time_series_generator`, `anomaly_detection`, `dataframe_to_csv` | No-upstream synthetic generator |

### ML pipelines

| Demo | Components | Highlights |
|---|---|---|
| [Palmer Penguins](penguins.md) | `csv_file_ingestion`, `imputation`, `one_hot_encoding`, `feature_scaler`, `dataframe_to_parquet` | Canonical ML preprocessing |
| [Iris Unsupervised](iris_unsupervised.md) | `csv_file_ingestion`, `feature_scaler`, `pca`, `k_means_clustering`, `dataframe_to_csv` | PCA + K-Means on the classic dataset |
| [Wine ML Pipeline](wine_ml_pipeline.md) | `csv_file_ingestion`, `feature_scaler`, `create_samples`, `decision_tree_model`, `cross_validation`, `dataframe_to_csv`, `cron_schedule` | Feature scaling → train/test split → decision tree + cross-validation |
| [Titanic Complete](titanic_complete.md) | `csv_file_ingestion`, `unique_dedup`, `data_cleansing`, `outlier_clipper`, `imputation`, `type_coercer`, `tile_binning`, `one_hot_encoding`, `logistic_regression_model`, `summarize`, `filter`, `dataframe_to_csv`, `cron_schedule` | Full DS workflow: ingest → quality → ETL → model → 3 outputs |
| [Churn Prediction](churn.md) | `synthetic_data_generator`, `churn_prediction`, `dataframe_to_csv` | Rule-based scoring with interpretable risk factors |
| [Market Basket](market_basket.md) | `csv_file_ingestion`, `market_basket_rules`, `filter`, `summarize`, `dataframe_to_csv`, `cron_schedule` | Apriori association rules with lift filter |

### Customer + subscription analytics

| Demo | Components | Highlights |
|---|---|---|
| [SaaS Metrics (synthetic Stripe)](saas_metrics.md) | `synthetic_data_generator`, `subscription_metrics`, `dataframe_to_csv` | MRR / ARR / churn / LTV / ARPU |
| [Revenue Attribution](revenue_attribution.md) | `csv_file_ingestion`, `synthetic_data_generator`, `revenue_attribution`, `dataframe_to_csv` | Linear attribution across marketing channels |
| [Retail LTV (CDP)](retail_ltv.md) | `csv_file_ingestion`, `data_cleansing`, `formula`, `ltv_prediction`, `dataframe_to_csv` | LTV on 542k real transactions |
| [Subscription Survival](subscription_survival.md) | `synthetic_data_generator`, `survival_analysis`, `dataframe_to_csv`, `cron_schedule` | Kaplan-Meier survival on SaaS subscriptions |
| [Retail Analytics](retail_analytics.md) | `synthetic_data_generator`, `datetime_parser`, `rfm_segmentation`, `cohort_analysis`, `running_total`, `dataframe_to_csv`, `cron_schedule` | RFM segmentation + cohort analysis + running spend |
| [A/B Full Pipeline](ab_full_pipeline.md) | `synthetic_data_generator`, `ab_treatments`, `ab_test_analysis`, `ab_trend`, `ab_controls`, `dataframe_to_csv`, `cron_schedule` | Assignment + analysis + trend + sample-size |

### Geospatial

| Demo | Components | Highlights |
|---|---|---|
| [Airports Cluster](airports_cluster.md) | `csv_file_ingestion`, `spatial_cluster`, `dataframe_to_csv` | DBSCAN on lat/lng (haversine, real km) |
| [US Cities Pairwise Distances](cities_distance.md) | `csv_file_ingestion`, `dataframe_join`, `distance_calculator`, `filter`, `sort`, `dataframe_to_csv` | Haversine distance matrix |
| [Cities Nearest Neighbors](cities_nn.md) | `csv_file_ingestion`, `nearest_neighbors`, `dataframe_to_csv` | Top-3 closest cities (sklearn KD-tree) |
| [West Coast Cities Filter](west_coast_cities.md) | `csv_file_ingestion`, `bounding_box_filter`, `dataframe_to_csv` | Geographic bounding-box filter |
| [Store Coverage](store_coverage.md) | `csv_file_ingestion`, `create_points`, `buffer`, `smooth`, `make_grid`, `spatial_join`, `summarize`, `dataframe_to_csv`, `cron_schedule` | Buffer + spatial_join + summarize coverage |

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
| [arXiv PDF Extraction](arxiv_pdf.md) | `csv_file_ingestion`, `pdf_text_extractor`, `formula`, `dataframe_to_csv` | Document → text → word counts |
| [Cars → SQL](cars_sql.md) | `rest_api_fetcher`, `datetime_parser`, `formula`, `dataframe_to_table` | Land DataFrame in SQLite |
| [Movies → SQL](movies_sql.md) | `csv_file_ingestion`, `type_coercer`, `formula`, `dataframe_to_table` | Real MovieLens Top 250 → SQLite |
| [NBA Scoreboard](nba_scoreboard.md) | `rest_api_fetcher`, `json_path_extractor`, `dataframe_to_csv`, `http_poll_sensor` | `http_poll_sensor` with targeted hashing |
| [RSS Sensor](rss_sensor.md) | `rest_api_fetcher`, `xml_parser`, `dataframe_to_csv`, `rss_feed_sensor` | Sensor-driven HN frontpage ingestion |

### OCSF / Security

| Demo | Components | Highlights |
|---|---|---|
| [OCSF + Security Lake](ocsf_security_lake.md) | `csv_file_ingestion`, `ocsf_normalizer`, `ocsf_validator`, `dataframe_to_parquet` | Synthetic Dagster+ events through full OCSF pipeline (no AWS required) |

### Op jobs (no asset materialized)

| Demo | Components | Highlights |
|---|---|---|
| [Shell Command Job](shell_command_job.md) | `shell_command_job` | Scheduled shell command, no asset |
| [Dynamic Fanout Job](dynamic_fanout_job.md) | `dynamic_fanout_job` | DynamicOut: discover N items, parallel process, optional collect |
| [Per-File Processor](per_file_processor.md) | `per_file_processor_job` | Inbox-style fan-out: list local CSVs, parse each, archive on success |

### Patterns

| Demo | Components | Highlights |
|---|---|---|
| [DuckDB Warehouse](duckdb_warehouse.md) | `csv_file_ingestion`, `duckdb_io_manager`, `cron_schedule` | IO manager round-trip + downstream asset + daily schedule |
| [External Scheduler](external_scheduler.md) | `csv_file_ingestion`, `summarize`, `dataframe_to_csv`, `asset_job` | Pattern for keeping Control-M / Autosys / cron as master with Dagster as executor (GraphQL launchRun) |

---

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

### Microsoft Fabric (next-gen Synapse)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Fabric Full-Stack](fabric_full_stack.md) | All 6 Fabric components: `fabric_workspace`, `fabric_workspace_resource`, `fabric_lakehouse_resource`, `fabric_lakehouse_io_manager`, `dataframe_to_fabric_lakehouse`, `fabric_pipeline_trigger_job` (+ existing `dataframe_from_sql` for the Warehouse SQL endpoint) | F2 capacity + workspace + Lakehouse + Warehouse | ~$0.21/hr ($154/mo always-on) |

### Observability

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Dagster+ → Sentinel](dagster_plus_to_sentinel.md) **(Dagster+ + Azure)** | `dagster_plus_audit_log_ingestion` → `ocsf_normalizer` → `audit_logs_to_sentinel` | Log Analytics workspace | $0 (5GB/mo free tier) |

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

### AI / LLM

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Gemini LLM](gemini_llm.md) | `synthetic_data_generator` → `gemini_llm` (gemini-2.5-flash) | Gemini API key | $0 (free tier 5 RPM) |
| [Nano Banana](nano_banana.md) | `synthetic_data_generator` (image_prompts) → `gemini_image_generation` (gemini-2.5-flash-image) | Gemini API key, billing enabled (image gen requires it) | ~$0.01–$0.10 |
| [Vertex AI Embeddings](vertex_ai_embeddings.md) | `synthetic_data_generator` (image_prompts) → `vertex_ai_text_embeddings_asset` (text-embedding-004) → `dataframe_to_gcs` (parquet) | Vertex AI API enabled, `roles/aiplatform.user` | $0 (under free tier) |
| [Vision + Translation](vision_translate.md) | `synthetic_image_generator` → `vision_api_asset` (LABEL + OBJECT) → `dataframe_extract_field` (top label) → `translation_api_asset` (es/fr/de/ja) → `dataframe_to_csv` | Vision + Translation APIs enabled | ~$0.005 |
| [Speech + Translation](speech_translate.md) | `synthetic_data_generator` (audio_samples) → `speech_to_text_asset` (Cloud Speech v2) → `translation_api_asset` (es/fr/de/ja) → `dataframe_to_csv` | Speech + Translation APIs enabled | ~$0.001 |

### Real-pipeline patterns (multi-component chains)

| Demo | Components exercised | Highlights |
|---|---|---|
| [HRIS Normalizer](hris_normalizer.md) | `synthetic_data_generator` (employees) → `hris_normalizer` (vendor-agnostic) → `dataframe_to_csv` | Synthetic vendor export → canonical schema mapped via `value_maps` (case-insensitive: `Active`/`active`/`ACTIVE` → `active`; `Full-Time`/`FT`/`FULL_TIME` → `full_time`) |

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

### Bug-finds during validation

Live runs surfaced several real bugs the components now handle cleanly:
- `gemini_llm` thinking_budget vs max_output_tokens (truncated output)
- `google_sheets_ingestion` wrong import (`dlt.sources.google_sheets` isn't pip-installable)
- `bigquery_export_to_gcs_asset` ExtractJob has no `total_bytes_processed`
- `hris_normalizer` case-insensitive map didn't lowercase user-supplied keys
- Service account project's Sheets / Docs / Calendar APIs needing per-project enable

---

## Dagster+ required

| Demo | Pipeline | Highlights |
|---|---|---|
| [Dagster+ Audit → Security Lake](dagster_plus_security_lake.md) | `dagster_plus_audit_log_ingestion` → `ocsf_normalizer` → `ocsf_validator` → Parquet | Asset pipeline with full lineage; local Parquet by default. Validated with 176 real entries. |

---

## Catalog Lineage Sync — multi-target (no auth required for the file demo)

| Demo | Components used | Highlights |
|---|---|---|
| [Catalog Lineage Sync](lineage_catalogs.md) | `lineage_graph_extractor` (source) → `lineage_to_file` (sink) — swap in `lineage_to_purview`, `lineage_to_datahub`, `lineage_to_alation`, `lineage_to_collibra`, `lineage_to_openlineage`, `lineage_to_webhook` for real catalogs | Lock-step fan-out across N catalogs; per-sink change-detection skip via payload hashing. Validated locally end-to-end with file sink. |

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
