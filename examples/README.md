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
  - [Industry data standards](#industry-data-standards)
  - [Media transforms](#media-transforms)
  - [AI / NLP (no auth required)](#ai--nlp-no-auth-required)
  - [Cloud observability + enterprise SaaS](#cloud-observability--enterprise-saas)
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
- [Cross-vendor blueprints (not validated)](#cross-vendor-blueprints-not-validated)
- [SAP family + OData ecosystem (one component covers many vendors)](#sap-family--odata-ecosystem-one-component-covers-many-vendors)
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

### Patterns

| Demo | Components | Highlights |
|---|---|---|
| [DuckDB Warehouse](duckdb_warehouse.md) | `file_ingestion`, `duckdb_io_manager`, `cron_schedule` | IO manager round-trip + downstream asset + daily schedule |
| [External Scheduler](external_scheduler.md) | `file_ingestion`, `summarize`, `dataframe_to_csv`, `asset_job` | Pattern for keeping Control-M / Autosys / cron as master with Dagster as executor (GraphQL launchRun) |
| [Data Quality](data_quality.md) | `pandas_dataframe_check`, `pandera_asset_check`, `enhanced_data_quality_checks`, `freshness_check`, `great_expectations_check` | 4 asset_check components on a synthetic orders asset |
| [Data Quality Checks](data_quality_checks.md) | `synthetic_data_generator`, `dataframe_to_csv`, `enhanced_data_quality_checks`, `pandas_dataframe_check`, `pandera_asset_check`, `freshness_check` | End-to-end DQ pipeline |
| [Email Round-Trip (SMTP + IMAP)](email_roundtrip.md) | `synthetic_data_generator`, `smtp_send_asset`, `imap_inbox_source` | Fan out via SMTP, pull replies via IMAP. Local aiosmtpd + pure-Python IMAP stub for offline testing |
| [External Assets](external_assets.md) | 21 external-system integrations as declared assets | Declare external systems without owning their execution (Sigma / Hex / Tableau / Looker / etc.) |
| [HTTP External Asset](http_external_asset.md) | `http_external_asset` | Wraps any HTTP-driven external job runner as a Dagster asset |
| [Local IO Managers](local_io.md) | 9 IO managers + 3 source/sink components — duckdb / sqlite / lance / polars / parquet / csv / json | Round-trip a DataFrame through every local format |
| [Local Sinks](local_sinks.md) | `dataframe_to_csv`, `dataframe_to_parquet`, `dataframe_to_json`, `dataframe_to_excel`, `dataframe_to_table` | 5 file/table format sinks side by side |
| [Document Extractors](document_extractors.md) | 13 document-source components — PDF / Word / HTML / RST / Markdown / etc. | Mega-demo: every shipped document-extraction component |
| [S3 Dynamic-Partition Pipeline](s3_pipeline.md) | `s3_monitor` (dynamic_partition mode), `file_ingestion` (partitioned), `summarize`, `dataframe_to_parquet` | Sensor-driven round-trip on **local Minio S3** (Docker). Each detected file becomes a tracked dynamic partition → processed → parquet back to S3. Real S3 / GCS / ADLS by swapping the URI scheme. |

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

### AI / NLP (no auth required)

| Demo | Components | Highlights |
|---|---|---|
| [AI without LLMs](ai_no_llm.md) | `synthetic_data_generator`, `keyword_extractor`, `language_detector`, `pii_detector`, `pii_redactor`, `embeddings_generator` | Rule-based + local-model AI — no API keys needed |
| [Local NLP Mega-Demo](local_nlp.md) | 13 components — `document_chunker`, `text_chunker`, `part_of_speech_tagger`, … | Full local NLP toolbox (spaCy / nltk / regex / heuristics) |
| [Content Moderation](content_moderation.md) | `moderation_scorer`, `text_moderator` | Rule-based + ML-based moderation side by side |
| [Ollama (local LLM)](ollama.md) | `synthetic_data_generator`, `ollama_inference_asset` | Local Llama / Mistral / etc. inference — zero API cost (requires Ollama installed) |
| [NLP Utilities](nlp_utilities.md) | 6 standalone transforms — `document_chunker`, `word_cloud`, `part_of_speech_tagger`, `topic_modeler`, `text_similarity` | Drop-in NLP helpers |
| [Text Extraction](text_extraction.md) | `json_flatten`, `json_path_extractor`, `nested_field_extractor`, `xml_parser`, `html_parser`, `regex_parser` | Pull structured fields from semi-structured columns |

### Cloud observability + enterprise SaaS

| Demo | Components | Highlights |
|---|---|---|
| [AWS CloudWatch](aws_cloudwatch.md) | `cloudwatch_metrics_query`, `cloudwatch_logs_insights` | Pull metrics + Logs Insights query results into a DataFrame |
| [New Relic + Dynatrace](newrelic_dynatrace.md) | `newrelic_event_sink`, `dynatrace_metric_sink` | Emit pipeline-row events to APM SaaS |
| [OpenTelemetry Full-Stack](opentelemetry_demo.md) | `otel_metrics_emitter`, `otel_logs_emitter`, `otel_traces_emitter` | Metrics + logs + traces in one demo |
| [Prometheus](prometheus_demo.md) | `prometheus_push_gateway`, `prometheus_query_asset` | Push + pull patterns side by side |
| [Enterprise SaaS Resources](enterprise_saas.md) | `workday_resource`, `marketo_resource`, `intercom_resource`, `plaid_resource` | Declare 4 SaaS APIs in one code location |
| [SAP HANA via SQLAlchemy](sap_hana.md) | `dataframe_to_table` (mssql adapter pattern) | SAP HANA via SQLAlchemy |
| [Precisely Connect ETL](precisely_validation.md) | `precisely_connect_run`, `precisely_data_integrity_run` | Validated against public Precisely docs (no live cluster needed) |

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
| [LLM Execution Mega-Demo](llm_execution.md) | 13 LLM components — OpenAI / LiteLLM / prompt-executor / batch / etc. | OpenAI API key | usage-priced |
| [Vector / RAG](vector_rag.md) | `embeddings_generator`, `vector_store_writer`, `vector_store_query`, `reranker`, `rag_pipeline`, `conversation_memory` | Vector store + embeddings model | usage-priced (~$0.0001/doc) |
| [AI with LLMs](ai_with_llm.md) | `synthetic_data_generator`, `text_classifier`, `entity_extractor`, `sentiment_analyzer`, `document_summarizer`, `data_enricher` | OpenAI / Azure OpenAI key | usage-priced |
| [Multi-modal AI](multimodal_ai.md) | `image_captioner`, `image_llm_extractor`, `litellm_embedding_batch` | OpenAI API key (vision-capable) | usage-priced |

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
| **[OData generic — Northwind](odata_pipeline.md)** ✅ validated | `odata_ingestion` → `summarize` → `dataframe_to_parquet`. Public `services.odata.org/V4/Northwind`. **Validated end-to-end 2026-05-13 — 10 rows × 5 cols, RUN_SUCCESS.** | Nothing — no credentials, no infra. The fastest way to prove the OData component works. |
| **[Dremio (Docker, end-to-end)](dremio_pipeline.md)** | `dremio_ingestion` → `summarize` → `dataframe_to_parquet`. Docker Dremio OSS + PAT auth. | Docker Desktop. The setup script handles everything else (~2 min UI clicks for first-user + PAT). |
| **[SAP S/4HANA](sap_s4hana_pipeline.md)** | `odata_ingestion` against S/4HANA — three modes documented: API Business Hub sandbox (free signup), Cloud basic auth (Communication User), Cloud OAuth (XSUAA). Plus `dataframe_to_odata` for write-back with CSRF handling. | Free SAP account at api.sap.com (sandbox); OR an S/4HANA tenant. |
| **[SAP SuccessFactors](sap_successfactors_pipeline.md)** | `odata_ingestion` against SuccessFactors HRIS — employees, jobs, departments, comp, performance reviews. `User@CompanyID` basic auth or SAML-assertion OAuth. | SuccessFactors API user credentials. |
| **[SAP Concur](sap_concur_pipeline.md)** | `oauth_token_resource` (refresh_token grant + writeback rotation) → `oauth_rest_ingestion` (next_url pagination). Full headless flow with AWS SM / Azure KV / Vault writeback patterns documented. | Concur OAuth app credentials. |
| **[SAP Ariba](sap_ariba_pipeline.md)** | `oauth_token_resource` (client_credentials — easy headless) → `oauth_rest_ingestion` (cursor pagination). Operational Reporting / Sourcing / Supplier endpoints. | Ariba Developer Portal app with the right scopes. |
| **[SAP Datasphere](sap_datasphere_pipeline.md)** | `odata_ingestion` over Datasphere consumption APIs (analytic models). XSUAA OAuth flow. | Datasphere tenant + Space + OAuth client. |
| **[SAP HANA](sap_hana_pipeline.md)** | `sap_hana_ingestion` for direct SQL — Cloud + on-prem + Calculation Views in `_SYS_BIC`. Pairs with `sap_hana_resource`. | HANA Cloud or on-prem tenant. |
| **[Microsoft Dynamics 365 / Dataverse](dynamics365_pipeline.md)** | `odata_ingestion` v4 against Dataverse. Azure AD app permissions + workload identity option. | Azure AD app registration with Dataverse access. |
| **[Microsoft Graph](msgraph_pipeline.md)** | `odata_ingestion` v4 against `graph.microsoft.com`. Users, mail, calendar, Teams, OneDrive, SharePoint. App permissions + workload identity. | Azure AD app registration with Graph application permissions. |

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
