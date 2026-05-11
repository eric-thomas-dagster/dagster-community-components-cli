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

<a id="no-auth-required-synthetic-or-public-data"></a>
## No auth required (synthetic or public data) [¶](#no-auth-required-synthetic-or-public-data)

The biggest section — these run offline against synthetic data or public APIs.
Useful for onboarding, CI smoke tests, and proving a component works.

<a id="core-etl-patterns"></a>
### Core ETL patterns [¶](#core-etl-patterns)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Kitchen Sink](kitchen_sink.md) | 21 components | The breadth showcase — ingest × 3 → quality × 4 → join → transform × 3 → analytics × 4 → sink × 5 → schedule |
| [Pivot ↔ Unpivot](pivot_unpivot.md) | csv → pivot → unpivot → CSVs | Long↔wide round-trip on monthly sales |
| [Router](router.md) | csv → router → 3× CSV | Multi-output conditional split (high/medium/low value orders) |
| [Detect Changes](detect_changes.md) | csv × 2 → detect_changes → CSV | CDC: classify rows insert/update/delete/unchanged |
| [SCD Type 2](scd_type_2.md) | csv × 2 → scd_type_2 → CSV | History-tracking dimension load |
| [Window Calculation](window_calculation.md) | csv → window_calculation → CSV | Every supported window function on stock prices |
| [Regional Orders Union](regional_orders.md) | csv × 3 → dataframe_union → CSV | Merge multi-region order extracts with mismatched columns |

<a id="time-series--forecasting"></a>
### Time series + forecasting [¶](#time-series--forecasting)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Airline Passengers Forecast](passengers_forecast.md) | csv → datetime → ets_forecast → CSV | ETS / Holt-Winters time-series forecasting |
| [Forecast Comparison](forecast_comparison.md) | time_series → ARIMA + ETS → ts_compare | Head-to-head ARIMA vs ETS on the same series |
| [Stocks — Anomaly Detection](stocks_anomaly.md) | csv → anomaly_detection → CSV | Per-ticker z-score outlier flagging |
| [Sensor Gap-Fill](sensor_gapfill.md) | synthetic → ts_filler → running_total → CSV | Fill missing hourly readings + cumulative metrics |
| [Synthetic Metrics + Anomalies](synthetic_metrics.md) | time_series_generator → anomaly_detection → CSV | No-upstream synthetic generator |

<a id="ml-pipelines"></a>
### ML pipelines [¶](#ml-pipelines)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Palmer Penguins](penguins.md) | csv → impute → onehot → scale → parquet | Canonical ML preprocessing |
| [Iris Unsupervised](iris_unsupervised.md) | csv → scale → pca → k_means → CSV | PCA + K-Means on the classic dataset |
| [Wine ML Pipeline](wine_ml_pipeline.md) | 8 components | Feature scaling → train/test split → decision tree + cross-validation |
| [Titanic Complete](titanic_complete.md) | 12 components | Full DS workflow: ingest → quality → ETL → model → 3 outputs |
| [Churn Prediction](churn.md) | synthetic → churn_prediction → CSV | Rule-based scoring with interpretable risk factors |
| [Market Basket](market_basket.md) | csv → market_basket_rules → filter → CSV | Apriori association rules with lift filter |

<a id="customer--subscription-analytics"></a>
### Customer + subscription analytics [¶](#customer--subscription-analytics)

| Demo | Pipeline | Highlights |
|---|---|---|
| [SaaS Metrics (synthetic Stripe)](saas_metrics.md) | csv → subscription_metrics → CSV | MRR / ARR / churn / LTV / ARPU |
| [Revenue Attribution](revenue_attribution.md) | csv × 2 → revenue_attribution → CSV | Linear attribution across marketing channels |
| [Retail LTV (CDP)](retail_ltv.md) | csv → cleanse → formula → ltv → CSV | LTV on 542k real transactions |
| [Subscription Survival](subscription_survival.md) | synthetic → survival_analysis → CSV | Kaplan-Meier survival on SaaS subscriptions |
| [Retail Analytics](retail_analytics.md) | 7 components, 3 parallel branches | RFM segmentation + cohort analysis + running spend |
| [A/B Full Pipeline](ab_full_pipeline.md) | 10 components | Assignment + analysis + trend + sample-size |

<a id="geospatial"></a>
### Geospatial [¶](#geospatial)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Airports Cluster](airports_cluster.md) | csv → spatial_cluster → CSV | DBSCAN on lat/lng (haversine, real km) |
| [US Cities Pairwise Distances](cities_distance.md) | csv × 2 → cross-join → distance_calculator → CSV | Haversine distance matrix |
| [Cities Nearest Neighbors](cities_nn.md) | csv → nearest_neighbors → CSV | Top-3 closest cities (sklearn KD-tree) |
| [West Coast Cities Filter](west_coast_cities.md) | csv → bounding_box_filter → CSV | Geographic bounding-box filter |
| [Store Coverage](store_coverage.md) | 9 components | Buffer + spatial_join + summarize coverage |

<a id="public-apis-no-auth"></a>
### Public APIs (no auth) [¶](#public-apis-no-auth)

| Demo | Pipeline | Highlights |
|---|---|---|
| [USGS Earthquakes](earthquakes.md) | rest → flatten → select → sort → JSON | REST + nested JSON |
| [Earthquakes Partitioned](partitioned_earthquakes.md) | same, daily-partitioned | Backfillable date range |
| [SpaceX Launches](spacex.md) | rest → select → datetime → rank → Excel | Datetime parsing + ranking |
| [SpaceX Multi-Source Join](spacex_join.md) | rest × 2 → dataframe_join → CSV | Fan-in two REST sources |
| [REST Countries](countries.md) | rest → formula → summarize → JSON | Computed columns + rollup |
| [NYC Weather](weather.md) | rest → datetime → running_total → transpose → CSV | Cumulative + pivot |
| [Dagster GitHub Releases](releases.md) | rest → select → datetime → filter → sort → parquet | Filter + sort + parquet |
| [GitHub Search JSONPath](github_jsonpath.md) | rest → nested_field_extractor → json_path_extractor → CSV | Two ways to flatten nested JSON |
| [HN RSS](hn_rss.md) | rest → regex × 2 → filter → CSV | XML feed → structured rows |
| [HN XML Parser](hn_xml.md) | rest → xml_parser → array_exploder → CSV | xpath all the way down |
| [Wikipedia Multi-page Scraper](wiki_scraper.md) | rest × N → html_parser → CSV | Scrape multiple wiki pages |
| [Books Scraper (partitioned)](books_scraper.md) | rest → html_parser → JSON | Multi-page HTML scrape, one partition per page |
| [arXiv PDF Extraction](arxiv_pdf.md) | csv → pdf_text_extractor → formula → CSV | Document → text → word counts |
| [Cars → SQL](cars_sql.md) | rest → datetime → formula → dataframe_to_table | Land DataFrame in SQLite |
| [Movies → SQL](movies_sql.md) | csv → type_coercer → formula → SQL | Real MovieLens Top 250 → SQLite |
| [NBA Scoreboard](nba_scoreboard.md) | http_poll_sensor → rest → json_path → CSV | [`http_poll_sensor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/http_poll_sensor) with targeted hashing |
| [RSS Sensor](rss_sensor.md) | rss_feed_sensor → rest → xml_parser → CSV | Sensor-driven HN frontpage ingestion |

<a id="ocsf--security"></a>
### OCSF / Security [¶](#ocsf--security)

| Demo | Pipeline | Highlights |
|---|---|---|
| [OCSF + Security Lake](ocsf_security_lake.md) | csv → ocsf_normalizer → validator → Parquet | Synthetic Dagster+ events through full OCSF pipeline (no AWS required) |

<a id="op-jobs-no-asset-materialized"></a>
### Op jobs (no asset materialized) [¶](#op-jobs-no-asset-materialized)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Shell Command Job](shell_command_job.md) | shell_command_job | Scheduled shell command, no asset |
| [Dynamic Fanout Job](dynamic_fanout_job.md) | dynamic_fanout_job | DynamicOut: discover N items, parallel process, optional collect |
| [Per-File Processor](per_file_processor.md) | per_file_processor_job | Inbox-style fan-out: list local CSVs, parse each, archive on success |

<a id="patterns"></a>
### Patterns [¶](#patterns)

| Demo | Pipeline | Highlights |
|---|---|---|
| [DuckDB Warehouse](duckdb_warehouse.md) | csv → duckdb_io_manager → summary → cron | IO manager round-trip + downstream asset + daily schedule |
| [External Scheduler](external_scheduler.md) | csv → summarize → csv (daily-partitioned) + bin/kick_off_run.sh | Pattern for keeping Control-M / Autosys / cron as master with Dagster as executor (GraphQL launchRun) |

---

<a id="azure-subscription-required"></a>
## Azure (subscription required) [¶](#azure-subscription-required)

Each Azure demo lists exact provisioning commands and teardown. Costs are noted
per-demo; all of these stay well under the free monthly allotments for personal
subscriptions.

<a id="storage--lakehouse"></a>
### Storage + lakehouse [¶](#storage--lakehouse)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [ADLS Round-Trip](adls_round_trip.md) | [`dataframe_to_adls`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_adls), [`external_adls_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/external_assets/external_adls_asset) | 1 storage account + container | <$0.05/mo |
| [ADLS Inbox](adls_inbox.md) | [`adls_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/adls_monitor) (sensor) → [`asset_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/schedules/asset_job) → [`adls_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/adls_to_database_asset) | same storage account, files in `demo/inbox/` | <$0.05/mo |
| [Bicep Self-Provision](bicep_self_provision.md) | [`bicep_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/bicep_asset) provisions storage; downstream uses it | none upfront — Bicep creates it | <$0.05/mo |

<a id="databases"></a>
### Databases [¶](#databases)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Cosmos DB Round-Trip](cosmosdb_round_trip.md) | [`cosmosdb_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/cosmosdb_writer) → [`cosmosdb_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/cosmosdb_reader) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | Cosmos DB account (free tier) | $0 |
| [Azure SQL Database](azure_sql.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) → [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) (mssql+pymssql) | SQL Server + serverless DB | <$0.05/mo idle |
| [Azure PostgreSQL Flexible](azure_postgres.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) → [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) (postgresql+psycopg2) | Flexible Server B1ms | ~$13/mo |
| [Azure MySQL Flexible](azure_mysql.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) → [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) (mysql+pymysql) | Flexible Server B1ms | ~$13/mo |
| [Azure Cache for Redis](azure_redis.md) | [`redis_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/redis_writer) (TLS) → [`redis_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/redis_reader) (TLS) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | Cache Basic C0 | ~$16/mo |

<a id="orchestration--workflow"></a>
### Orchestration + workflow [¶](#orchestration--workflow)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Azure Data Factory](azure_data_factory.md) | [`azure_data_factory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/azure_data_factory) (import + trigger ADF pipelines, capture per-activity metadata) | ADF instance + service principal | $0 idle, $0.001/activity |
| [Azure Synapse Analytics](azure_synapse.md) | [`azure_synapse`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/azure_synapse) (import + trigger Synapse pipelines; Spark/notebook discovery) | Synapse workspace + ADLS Gen2 storage + service principal | $0 idle, free serverless SQL <1TB/mo |
| [Synapse Serverless SQL (OPENROWSET)](azure_synapse_serverless.md) | [`dataframe_to_adls`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_adls) → [`dataframe_from_sql`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_sql) (no Synapse-specific component needed!) | Same Synapse workspace + a demo db with master key + db-scoped credential + external data source | $0 — first 1TB/mo scanned is free |

<a id="streaming--queues"></a>
### Streaming + queues [¶](#streaming--queues)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Azure Event Hubs Round-Trip](azure_eventhubs.md) | [`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) (NEW) → [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) → Postgres | EH Basic namespace + hub | ~$11/mo + $0.028/M events |

<a id="microsoft-fabric-next-gen-synapse"></a>
### Microsoft Fabric (next-gen Synapse) [¶](#microsoft-fabric-next-gen-synapse)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Fabric Full-Stack](fabric_full_stack.md) | All 6 Fabric components: [`fabric_workspace`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/fabric_workspace), [`fabric_workspace_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/fabric_workspace_resource), [`fabric_lakehouse_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/fabric_lakehouse_resource), [`fabric_lakehouse_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/fabric_lakehouse_io_manager), [`dataframe_to_fabric_lakehouse`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_fabric_lakehouse), [`fabric_pipeline_trigger_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/fabric_pipeline_trigger_job) (+ existing [`dataframe_from_sql`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_sql) for the Warehouse SQL endpoint) | F2 capacity + workspace + Lakehouse + Warehouse | ~$0.21/hr ($154/mo always-on) |

<a id="observability"></a>
### Observability [¶](#observability)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Dagster+ → Sentinel](dagster_plus_to_sentinel.md) **(Dagster+ + Azure)** | [`dagster_plus_audit_log_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/ingestion/dagster_plus_audit_log_ingestion) → [`ocsf_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/ocsf_normalizer) → [`audit_logs_to_sentinel`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sinks/audit_logs_to_sentinel) | Log Analytics workspace | $0 (5GB/mo free tier) |

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

<a id="auth-managed-identity-in-azure-compute"></a>
### Auth: managed identity in Azure compute [¶](#auth-managed-identity-in-azure-compute)

When running these in **Azure Container Apps** or **AKS** with a managed
identity attached, you can omit the env-var auth entirely. The Azure
components use `DefaultAzureCredential`, which falls back through
env vars → managed identity → `az login` automatically. Local development
uses env vars; production in Azure compute uses the attached identity.

---

<a id="google-cloud-gcp-subscription-required"></a>
## Google Cloud (GCP, subscription required) [¶](#google-cloud-gcp-subscription-required)

Each GCP demo is validated end-to-end against a real GCP project. Auth via
service-account JSON pointed at by `GOOGLE_APPLICATION_CREDENTIALS`.
Most APIs need to be enabled per-project on first use; the components
surface the exact activation URL on the first call. Costs are noted
per-demo; everything below stays well under the free tier for typical
demos.

<a id="workspace-drive-docs-sheets-calendar"></a>
### Workspace (Drive, Docs, Sheets, Calendar) [¶](#workspace-drive-docs-sheets-calendar)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Google Sheets](google_sheets.md) | [`google_sheets_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_sheets_ingestion) (single-component demo) | SA shared on a sheet, Sheets API enabled | $0 |
| [Google Calendar](google_calendar.md) | [`google_calendar_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_calendar_ingestion) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) + [`dataframe_to_bigquery`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_bigquery) | SA shared on a calendar, Calendar API enabled | $0 |
| [Drive + Docs + Gemini](google_drive_docs.md) | [`google_drive_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_drive_ingestion) → [`google_docs_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_docs_extractor) → [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm) → CSV | SA shared on a Drive folder, Drive + Docs + Generative Language APIs enabled | $0 |

<a id="warehouse-bigquery"></a>
### Warehouse (BigQuery) [¶](#warehouse-bigquery)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [BigQuery Query](bigquery_query.md) | [`bigquery_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_query_asset) against `bigquery-public-data.samples.shakespeare` (single-component demo) | BigQuery API enabled, `roles/bigquery.jobUser` | $0.0001 |
| [BigQuery ML Pipeline](bigquery_ml_pipeline.md) | [`bigquery_create_table_from_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/bigquery_create_table_from_query_asset) (CTAS) → [`bigquery_ml_train_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_train_asset) (LOGISTIC_REG) → [`bigquery_ml_predict_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_predict_asset) → CSV | BQ dataset, `roles/bigquery.dataEditor` + `jobUser` | <$0.001 |
| [BigQuery ↔ GCS Bulk Bridge](bigquery_bulk_bridge.md) | [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset) (EXTRACT) → [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) (LOAD JOB) — round-trip | BQ dataset + GCS bucket | $0 (extract + load free) |

<a id="ai--llm"></a>
### AI / LLM [¶](#ai--llm)

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Gemini LLM](gemini_llm.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) → [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm) (gemini-2.5-flash) | Gemini API key | $0 (free tier 5 RPM) |
| [Nano Banana](nano_banana.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) (image_prompts) → [`gemini_image_generation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_image_generation) (gemini-2.5-flash-image) | Gemini API key, billing enabled (image gen requires it) | ~$0.01–$0.10 |
| [Vertex AI Embeddings](vertex_ai_embeddings.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) (image_prompts) → [`vertex_ai_text_embeddings_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vertex_ai_text_embeddings_asset) (text-embedding-004) → [`dataframe_to_gcs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_gcs) (parquet) | Vertex AI API enabled, `roles/aiplatform.user` | $0 (under free tier) |
| [Vision + Translation](vision_translate.md) | [`synthetic_image_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/synthetic_image_generator) → [`vision_api_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vision_api_asset) (LABEL + OBJECT) → [`dataframe_extract_field`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_extract_field) (top label) → [`translation_api_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/translation_api_asset) (es/fr/de/ja) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | Vision + Translation APIs enabled | ~$0.005 |
| [Speech + Translation](speech_translate.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) (audio_samples) → [`speech_to_text_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/speech_to_text_asset) (Cloud Speech v2) → [`translation_api_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/translation_api_asset) (es/fr/de/ja) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | Speech + Translation APIs enabled | ~$0.001 |

<a id="real-pipeline-patterns-multi-component-chains"></a>
### Real-pipeline patterns (multi-component chains) [¶](#real-pipeline-patterns-multi-component-chains)

| Demo | Components exercised | Highlights |
|---|---|---|
| [HRIS Normalizer](hris_normalizer.md) | [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) (employees) → [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) (vendor-agnostic) → [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) | Synthetic vendor export → canonical schema mapped via `value_maps` (case-insensitive: `Active`/`active`/`ACTIVE` → `active`; `Full-Time`/`FT`/`FULL_TIME` → `full_time`) |

<a id="auth-workload-identity-in-gcp-compute"></a>
### Auth: workload identity in GCP compute [¶](#auth-workload-identity-in-gcp-compute)

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
- [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm) thinking_budget vs max_output_tokens (truncated output)
- [`google_sheets_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_sheets_ingestion) wrong import (`dlt.sources.google_sheets` isn't pip-installable)
- [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset) ExtractJob has no `total_bytes_processed`
- [`hris_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hris_normalizer) case-insensitive map didn't lowercase user-supplied keys
- Service account project's Sheets / Docs / Calendar APIs needing per-project enable

---

<a id="dagster-required"></a>
## Dagster+ required [¶](#dagster-required)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Dagster+ Audit → Security Lake](dagster_plus_security_lake.md) | dagster_plus_audit_log_ingestion → ocsf_normalizer → ocsf_validator → Parquet | Asset pipeline with full lineage; local Parquet by default. Validated with 176 real entries. |

---

<a id="catalog-lineage-sync--multi-target-no-auth-required-for-the-file-demo"></a>
## Catalog Lineage Sync — multi-target (no auth required for the file demo) [¶](#catalog-lineage-sync--multi-target-no-auth-required-for-the-file-demo)

| Demo | Components used | Highlights |
|---|---|---|
| [Catalog Lineage Sync](lineage_catalogs.md) | [`lineage_graph_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/lineage_graph_extractor) (source) → [`lineage_to_file`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_file) (sink) — swap in [`lineage_to_purview`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_purview), [`lineage_to_datahub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_datahub), [`lineage_to_alation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_alation), [`lineage_to_collibra`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_collibra), [`lineage_to_openlineage`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_openlineage), [`lineage_to_webhook`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_webhook) for real catalogs | Lock-step fan-out across N catalogs; per-sink change-detection skip via payload hashing. Validated locally end-to-end with file sink. |

---

<a id="how-a-demo-is-built"></a>
## How a demo is built [¶](#how-a-demo-is-built)

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

<a id="auth-required-demos-comprehensive-prereqs"></a>
## Auth-required demos: comprehensive prereqs [¶](#auth-required-demos-comprehensive-prereqs)

For demos that need credentials, every walkthrough now documents:

- **What to install** (Azure CLI, AWS CLI, etc.)
- **What providers/features to enable** (Microsoft.Storage, Microsoft.OperationalInsights, etc.)
- **What env vars to set** (with the `az` / `aws` commands to fetch them)
- **What it costs** (with realistic numbers for the demo's data volume)
- **How to tear down** (single-command deletion)

If a section is missing from any auth-required walkthrough, file an issue or PR.
