# Examples

End-to-end Dagster pipelines built entirely from community components.
Each demo:

- Runs end-to-end with a single `curl | bash` then `dg launch`
- Lists exactly what it needs (auth, infra, env vars, cost)
- Has been **validated against real systems**, not just type-checked

The demos are grouped by what they need to run.

---

## No auth required (synthetic or public data)

The biggest section — these run offline against synthetic data or public APIs.
Useful for onboarding, CI smoke tests, and proving a component works.

### Core ETL patterns

| Demo | Pipeline | Highlights |
|---|---|---|
| [Kitchen Sink](kitchen_sink.md) | 21 components | The breadth showcase — ingest × 3 → quality × 4 → join → transform × 3 → analytics × 4 → sink × 5 → schedule |
| [Pivot ↔ Unpivot](pivot_unpivot.md) | csv → pivot → unpivot → CSVs | Long↔wide round-trip on monthly sales |
| [Router](router.md) | csv → router → 3× CSV | Multi-output conditional split (high/medium/low value orders) |
| [Detect Changes](detect_changes.md) | csv × 2 → detect_changes → CSV | CDC: classify rows insert/update/delete/unchanged |
| [SCD Type 2](scd_type_2.md) | csv × 2 → scd_type_2 → CSV | History-tracking dimension load |
| [Window Calculation](window_calculation.md) | csv → window_calculation → CSV | Every supported window function on stock prices |
| [Regional Orders Union](regional_orders.md) | csv × 3 → dataframe_union → CSV | Merge multi-region order extracts with mismatched columns |

### Time series + forecasting

| Demo | Pipeline | Highlights |
|---|---|---|
| [Airline Passengers Forecast](passengers_forecast.md) | csv → datetime → ets_forecast → CSV | ETS / Holt-Winters time-series forecasting |
| [Forecast Comparison](forecast_comparison.md) | time_series → ARIMA + ETS → ts_compare | Head-to-head ARIMA vs ETS on the same series |
| [Stocks — Anomaly Detection](stocks_anomaly.md) | csv → anomaly_detection → CSV | Per-ticker z-score outlier flagging |
| [Sensor Gap-Fill](sensor_gapfill.md) | synthetic → ts_filler → running_total → CSV | Fill missing hourly readings + cumulative metrics |
| [Synthetic Metrics + Anomalies](synthetic_metrics.md) | time_series_generator → anomaly_detection → CSV | No-upstream synthetic generator |

### ML pipelines

| Demo | Pipeline | Highlights |
|---|---|---|
| [Palmer Penguins](penguins.md) | csv → impute → onehot → scale → parquet | Canonical ML preprocessing |
| [Iris Unsupervised](iris_unsupervised.md) | csv → scale → pca → k_means → CSV | PCA + K-Means on the classic dataset |
| [Wine ML Pipeline](wine_ml_pipeline.md) | 8 components | Feature scaling → train/test split → decision tree + cross-validation |
| [Titanic Complete](titanic_complete.md) | 12 components | Full DS workflow: ingest → quality → ETL → model → 3 outputs |
| [Churn Prediction](churn.md) | synthetic → churn_prediction → CSV | Rule-based scoring with interpretable risk factors |
| [Market Basket](market_basket.md) | csv → market_basket_rules → filter → CSV | Apriori association rules with lift filter |

### Customer + subscription analytics

| Demo | Pipeline | Highlights |
|---|---|---|
| [SaaS Metrics (synthetic Stripe)](saas_metrics.md) | csv → subscription_metrics → CSV | MRR / ARR / churn / LTV / ARPU |
| [Revenue Attribution](revenue_attribution.md) | csv × 2 → revenue_attribution → CSV | Linear attribution across marketing channels |
| [Retail LTV (CDP)](retail_ltv.md) | csv → cleanse → formula → ltv → CSV | LTV on 542k real transactions |
| [Subscription Survival](subscription_survival.md) | synthetic → survival_analysis → CSV | Kaplan-Meier survival on SaaS subscriptions |
| [Retail Analytics](retail_analytics.md) | 7 components, 3 parallel branches | RFM segmentation + cohort analysis + running spend |
| [A/B Full Pipeline](ab_full_pipeline.md) | 10 components | Assignment + analysis + trend + sample-size |

### Geospatial

| Demo | Pipeline | Highlights |
|---|---|---|
| [Airports Cluster](airports_cluster.md) | csv → spatial_cluster → CSV | DBSCAN on lat/lng (haversine, real km) |
| [US Cities Pairwise Distances](cities_distance.md) | csv × 2 → cross-join → distance_calculator → CSV | Haversine distance matrix |
| [Cities Nearest Neighbors](cities_nn.md) | csv → nearest_neighbors → CSV | Top-3 closest cities (sklearn KD-tree) |
| [West Coast Cities Filter](west_coast_cities.md) | csv → bounding_box_filter → CSV | Geographic bounding-box filter |
| [Store Coverage](store_coverage.md) | 9 components | Buffer + spatial_join + summarize coverage |

### Public APIs (no auth)

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
| [NBA Scoreboard](nba_scoreboard.md) | http_poll_sensor → rest → json_path → CSV | `http_poll_sensor` with targeted hashing |
| [RSS Sensor](rss_sensor.md) | rss_feed_sensor → rest → xml_parser → CSV | Sensor-driven HN frontpage ingestion |

### OCSF / Security

| Demo | Pipeline | Highlights |
|---|---|---|
| [OCSF + Security Lake](ocsf_security_lake.md) | csv → ocsf_normalizer → validator → Parquet | Synthetic Dagster+ events through full OCSF pipeline (no AWS required) |

### Op jobs (no asset materialized)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Shell Command Job](shell_command_job.md) | shell_command_job | Scheduled shell command, no asset |
| [Dynamic Fanout Job](dynamic_fanout_job.md) | dynamic_fanout_job | DynamicOut: discover N items, parallel process, optional collect |
| [Per-File Processor](per_file_processor.md) | per_file_processor_job | Inbox-style fan-out: list local CSVs, parse each, archive on success |

### Patterns

| Demo | Pipeline | Highlights |
|---|---|---|
| [DuckDB Warehouse](duckdb_warehouse.md) | csv → duckdb_io_manager → summary → cron | IO manager round-trip + downstream asset + daily schedule |
| [External Scheduler](external_scheduler.md) | csv → summarize → csv (daily-partitioned) + bin/kick_off_run.sh | Pattern for keeping Control-M / Autosys / cron as master with Dagster as executor (GraphQL launchRun) |

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

## Dagster+ required (no Azure)

| Demo | Pipeline | Highlights |
|---|---|---|
| [Dagster+ Audit → Security Lake](dagster_plus_security_lake.md) | dagster_plus_audit_log_ingestion → ocsf_normalizer → ocsf_validator → Parquet | Asset pipeline with full lineage; local Parquet by default. Validated with 176 real entries. |

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

## Components verified by examples

Each component below is referenced — and exercised — by at least one of the walkthroughs above. Click the component to see its source / schema / example, or click a demo to see the chain it appears in.

**Total: 167 components verified by 80 walkthroughs.** Updated 2026-05-06.

### Counts by category

| Category | Verified | Total in registry | % verified |
|---|---|---|---|
| transformation | 39 | 76 | 51% |
| analytics | 34 | 90 | 38% |
| sink | 29 | 50 | 58% |
| source | 14 | 28 | 50% |
| resource | 14 | 70 | 20% |
| infrastructure | 13 | 57 | 23% |
| ingestion | 7 | 62 | 11% |
| sensor | 5 | 40 | 12% |
| io_manager | 5 | 33 | 15% |
| integration | 3 | 30 | 10% |
| ai | 2 | 73 | 3% |
| external | 1 | 21 | 5% |
| check | 1 | 11 | 9% |
| **Total** | **167** | **662** | **25%** |

### resource (14)

- [`cosmosdb_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/cosmosdb_resource) — [cosmosdb_round_trip](cosmosdb_round_trip.md)
- [`dynatrace_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/dynatrace_resource) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`fabric_lakehouse_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/fabric_lakehouse_resource) — [fabric_full_stack](fabric_full_stack.md)
- [`fabric_workspace_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/fabric_workspace_resource) — [fabric_full_stack](fabric_full_stack.md)
- [`intercom_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/intercom_resource) — [enterprise_saas](enterprise_saas.md)
- [`key_vault_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/key_vault_resource) — [key_vault](key_vault.md)
- [`marketo_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/marketo_resource) — [enterprise_saas](enterprise_saas.md)
- [`mssql_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mssql_resource) — [azure_postgres](azure_postgres.md)
- [`mysql_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/mysql_resource) — [azure_mysql](azure_mysql.md), [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)
- [`newrelic_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/newrelic_resource) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`plaid_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/plaid_resource) — [enterprise_saas](enterprise_saas.md)
- [`postgres_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/postgres_resource) — [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)
- [`sap_hana_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/sap_hana_resource) — [sap_hana](sap_hana.md)
- [`workday_resource`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/resources/workday_resource) — [enterprise_saas](enterprise_saas.md)

### io_manager (5)

- [`duckdb_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/duckdb_io_manager) — [duckdb_warehouse](duckdb_warehouse.md)
- [`fabric_lakehouse_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/fabric_lakehouse_io_manager) — [fabric_full_stack](fabric_full_stack.md)
- [`mssql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mssql_io_manager) — [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)
- [`mysql_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/mysql_io_manager) — [azure_mysql](azure_mysql.md), [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)
- [`postgres_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/postgres_io_manager) — [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)

### source (14)

- [`aws_cloudwatch_logs_insights_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/aws_cloudwatch_logs_insights_query) — [aws_cloudwatch](aws_cloudwatch.md)
- [`aws_cloudwatch_metrics_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/aws_cloudwatch_metrics_query) — [aws_cloudwatch](aws_cloudwatch.md)
- [`azure_log_analytics_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/azure_log_analytics_query) — [azure_data_explorer](azure_data_explorer.md), [azure_log_analytics](azure_log_analytics.md)
- [`azure_search_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/azure_search_query) — [azure_search](azure_search.md)
- [`azure_table_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/azure_table_reader) — [azure_tables](azure_tables.md)
- [`cosmosdb_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/cosmosdb_reader) — [cosmosdb_round_trip](cosmosdb_round_trip.md)
- [`dataframe_from_kusto`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_kusto) — [azure_data_explorer](azure_data_explorer.md)
- [`dataframe_from_prometheus`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_prometheus) — [prometheus_demo](prometheus_demo.md)
- [`dataframe_from_sql`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_sql) — [azure_synapse_serverless](azure_synapse_serverless.md), [fabric_full_stack](fabric_full_stack.md), [sap_hana](sap_hana.md)
- [`dataframe_from_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dataframe_from_table) — [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md)
- [`dynatrace_metrics_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/dynatrace_metrics_query) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`lineage_graph_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/lineage_graph_extractor) — [lineage_catalogs](lineage_catalogs.md)
- [`newrelic_nrql_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/newrelic_nrql_query) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`redis_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sources/redis_reader) — [azure_redis](azure_redis.md)

### sink (29)

- [`audit_logs_to_sentinel`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sinks/audit_logs_to_sentinel) — [azure_log_analytics](azure_log_analytics.md), [dagster_plus_to_sentinel](dagster_plus_to_sentinel.md)
- [`azure_search_indexer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/azure_search_indexer) — [azure_search](azure_search.md)
- [`cosmosdb_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/cosmosdb_writer) — [cosmosdb_round_trip](cosmosdb_round_trip.md)
- [`dataframe_to_adls`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_adls) — [adls_round_trip](adls_round_trip.md), [azure_synapse_serverless](azure_synapse_serverless.md), [bicep_self_provision](bicep_self_provision.md)
- [`dataframe_to_azure_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_azure_table) — [azure_tables](azure_tables.md)
- [`dataframe_to_csv`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_csv) — [ab_full_pipeline](ab_full_pipeline.md), [airports_cluster](airports_cluster.md), [arxiv_pdf](arxiv_pdf.md), [azure_redis](azure_redis.md), [azure_search](azure_search.md), [azure_tables](azure_tables.md), [churn](churn.md), [cities_distance](cities_distance.md), [cities_nn](cities_nn.md), [cosmosdb_round_trip](cosmosdb_round_trip.md), [detect_changes](detect_changes.md), [external_scheduler](external_scheduler.md), [forecast_comparison](forecast_comparison.md), [github_jsonpath](github_jsonpath.md), [hn_rss](hn_rss.md), [hn_xml](hn_xml.md), [iris_unsupervised](iris_unsupervised.md), [kitchen_sink](kitchen_sink.md), [market_basket](market_basket.md), [nba_scoreboard](nba_scoreboard.md), [passengers_forecast](passengers_forecast.md), [pivot_unpivot](pivot_unpivot.md), [prometheus_demo](prometheus_demo.md), [regional_orders](regional_orders.md), [retail_analytics](retail_analytics.md), [retail_ltv](retail_ltv.md), [revenue_attribution](revenue_attribution.md), [router](router.md), [rss_sensor](rss_sensor.md), [saas_metrics](saas_metrics.md), [scd_type_2](scd_type_2.md), [sensor_gapfill](sensor_gapfill.md), [spacex_join](spacex_join.md), [stocks_anomaly](stocks_anomaly.md), [store_coverage](store_coverage.md), [subscription_survival](subscription_survival.md), [synthetic_metrics](synthetic_metrics.md), [titanic_complete](titanic_complete.md), [weather](weather.md), [west_coast_cities](west_coast_cities.md), [window_calculation](window_calculation.md), [wine_ml_pipeline](wine_ml_pipeline.md)
- [`dataframe_to_dynatrace_events`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_dynatrace_events) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`dataframe_to_eventhub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_eventhub) — [azure_eventhubs](azure_eventhubs.md)
- [`dataframe_to_excel`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_excel) — [spacex](spacex.md)
- [`dataframe_to_fabric_lakehouse`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_fabric_lakehouse) — [fabric_full_stack](fabric_full_stack.md)
- [`dataframe_to_json`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_json) — [books_scraper](books_scraper.md), [countries](countries.md), [earthquakes](earthquakes.md), [partitioned_earthquakes](partitioned_earthquakes.md), [wiki_scraper](wiki_scraper.md)
- [`dataframe_to_kusto`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_kusto) — [azure_data_explorer](azure_data_explorer.md)
- [`dataframe_to_newrelic_logs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_newrelic_logs) — [newrelic_dynatrace](newrelic_dynatrace.md)
- [`dataframe_to_otlp_logs`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_logs) — [newrelic_dynatrace](newrelic_dynatrace.md), [opentelemetry_demo](opentelemetry_demo.md)
- [`dataframe_to_otlp_metrics`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_metrics) — [newrelic_dynatrace](newrelic_dynatrace.md), [opentelemetry_demo](opentelemetry_demo.md)
- [`dataframe_to_otlp_traces`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_otlp_traces) — [opentelemetry_demo](opentelemetry_demo.md)
- [`dataframe_to_parquet`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_parquet) — [dagster_plus_security_lake](dagster_plus_security_lake.md), [ocsf_security_lake](ocsf_security_lake.md), [penguins](penguins.md), [releases](releases.md)
- [`dataframe_to_prometheus`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_prometheus) — [prometheus_demo](prometheus_demo.md)
- [`dataframe_to_security_lake`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sinks/dataframe_to_security_lake) — [dagster_plus_security_lake](dagster_plus_security_lake.md), [ocsf_security_lake](ocsf_security_lake.md)
- [`dataframe_to_servicebus`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_servicebus) — [azure_servicebus](azure_servicebus.md)
- [`dataframe_to_table`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/dataframe_to_table) — [azure_mysql](azure_mysql.md), [azure_postgres](azure_postgres.md), [azure_sql](azure_sql.md), [azure_synapse_serverless](azure_synapse_serverless.md), [cars_sql](cars_sql.md), [fabric_full_stack](fabric_full_stack.md), [movies_sql](movies_sql.md), [sap_hana](sap_hana.md)
- [`lineage_to_alation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_alation) — [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_collibra`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_collibra) — [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_datahub`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_datahub) — [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_file`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_file) — [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_openlineage`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_openlineage) — [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_purview`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_purview) — [fabric_full_stack](fabric_full_stack.md), [lineage_catalogs](lineage_catalogs.md)
- [`lineage_to_webhook`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/lineage_to_webhook) — [lineage_catalogs](lineage_catalogs.md)
- [`redis_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/redis_writer) — [azure_eventhubs](azure_eventhubs.md), [azure_redis](azure_redis.md)

### ingestion (7)

- [`adls_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/adls_to_database_asset) — [adls_inbox](adls_inbox.md), [adls_round_trip](adls_round_trip.md)
- [`csv_file_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/csv_file_ingestion) — [airports_cluster](airports_cluster.md), [arxiv_pdf](arxiv_pdf.md), [churn](churn.md), [cities_distance](cities_distance.md), [cities_nn](cities_nn.md), [detect_changes](detect_changes.md), [duckdb_warehouse](duckdb_warehouse.md), [external_scheduler](external_scheduler.md), [iris_unsupervised](iris_unsupervised.md), [market_basket](market_basket.md), [movies_sql](movies_sql.md), [ocsf_security_lake](ocsf_security_lake.md), [passengers_forecast](passengers_forecast.md), [penguins](penguins.md), [pivot_unpivot](pivot_unpivot.md), [regional_orders](regional_orders.md), [retail_ltv](retail_ltv.md), [revenue_attribution](revenue_attribution.md), [router](router.md), [saas_metrics](saas_metrics.md), [scd_type_2](scd_type_2.md), [stocks_anomaly](stocks_anomaly.md), [store_coverage](store_coverage.md), [titanic_complete](titanic_complete.md), [west_coast_cities](west_coast_cities.md), [window_calculation](window_calculation.md), [wine_ml_pipeline](wine_ml_pipeline.md)
- [`dagster_plus_audit_log_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/ingestion/dagster_plus_audit_log_ingestion) — [dagster_plus_security_lake](dagster_plus_security_lake.md), [dagster_plus_to_sentinel](dagster_plus_to_sentinel.md)
- [`eventhubs_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/eventhubs_to_database_asset) — [azure_eventhubs](azure_eventhubs.md)
- [`redis_streams_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/redis_streams_to_database_asset) — [azure_redis](azure_redis.md)
- [`rest_api_fetcher`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/rest_api_fetcher) — [books_scraper](books_scraper.md), [cars_sql](cars_sql.md), [countries](countries.md), [earthquakes](earthquakes.md), [github_jsonpath](github_jsonpath.md), [hn_rss](hn_rss.md), [hn_xml](hn_xml.md), [nba_scoreboard](nba_scoreboard.md), [partitioned_earthquakes](partitioned_earthquakes.md), [releases](releases.md), [rss_sensor](rss_sensor.md), [spacex](spacex.md), [spacex_join](spacex_join.md), [weather](weather.md), [wiki_scraper](wiki_scraper.md)
- [`servicebus_to_database_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/servicebus_to_database_asset) — [azure_servicebus](azure_servicebus.md)

### transformation (39)

- [`alter_row`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/alter_row) — [detect_changes](detect_changes.md)
- [`arima_forecast`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/arima_forecast) — [forecast_comparison](forecast_comparison.md), [passengers_forecast](passengers_forecast.md)
- [`array_exploder`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/array_exploder) — [hn_xml](hn_xml.md), [wiki_scraper](wiki_scraper.md)
- [`create_samples`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/create_samples) — [wine_ml_pipeline](wine_ml_pipeline.md)
- [`data_cleansing`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/data_cleansing) — [kitchen_sink](kitchen_sink.md), [retail_ltv](retail_ltv.md), [titanic_complete](titanic_complete.md)
- [`dataframe_join`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_join) — [adls_round_trip](adls_round_trip.md), [cities_distance](cities_distance.md), [kitchen_sink](kitchen_sink.md), [spacex_join](spacex_join.md)
- [`dataframe_union`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_union) — [regional_orders](regional_orders.md)
- [`datetime_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/datetime_parser) — [cars_sql](cars_sql.md), [hn_rss](hn_rss.md), [passengers_forecast](passengers_forecast.md), [releases](releases.md), [retail_analytics](retail_analytics.md), [spacex](spacex.md), [weather](weather.md)
- [`detect_changes`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/detect_changes) — [detect_changes](detect_changes.md)
- [`ets_forecast`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/ets_forecast) — [forecast_comparison](forecast_comparison.md), [passengers_forecast](passengers_forecast.md)
- [`feature_scaler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/feature_scaler) — [iris_unsupervised](iris_unsupervised.md), [penguins](penguins.md), [wine_ml_pipeline](wine_ml_pipeline.md)
- [`filter`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/filter) — [cities_distance](cities_distance.md), [hn_rss](hn_rss.md), [kitchen_sink](kitchen_sink.md), [market_basket](market_basket.md), [releases](releases.md), [retail_ltv](retail_ltv.md), [titanic_complete](titanic_complete.md)
- [`formula`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/formula) — [arxiv_pdf](arxiv_pdf.md), [cars_sql](cars_sql.md), [countries](countries.md), [movies_sql](movies_sql.md), [retail_ltv](retail_ltv.md)
- [`html_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/html_parser) — [books_scraper](books_scraper.md), [wiki_scraper](wiki_scraper.md)
- [`imputation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/imputation) — [penguins](penguins.md), [titanic_complete](titanic_complete.md)
- [`json_flatten`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_flatten) — [earthquakes](earthquakes.md), [partitioned_earthquakes](partitioned_earthquakes.md)
- [`json_path_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_path_extractor) — [github_jsonpath](github_jsonpath.md), [nba_scoreboard](nba_scoreboard.md)
- [`nested_field_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/nested_field_extractor) — [github_jsonpath](github_jsonpath.md)
- [`ocsf_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/ocsf_normalizer) — [dagster_plus_security_lake](dagster_plus_security_lake.md), [dagster_plus_to_sentinel](dagster_plus_to_sentinel.md), [ocsf_security_lake](ocsf_security_lake.md)
- [`one_hot_encoding`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/one_hot_encoding) — [penguins](penguins.md), [titanic_complete](titanic_complete.md)
- [`outlier_clipper`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/outlier_clipper) — [kitchen_sink](kitchen_sink.md), [titanic_complete](titanic_complete.md)
- [`pdf_text_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/pdf_text_extractor) — [arxiv_pdf](arxiv_pdf.md)
- [`pivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/pivot) — [pivot_unpivot](pivot_unpivot.md)
- [`rank`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/rank) — [kitchen_sink](kitchen_sink.md), [spacex](spacex.md)
- [`regex_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/regex_parser) — [arxiv_pdf](arxiv_pdf.md), [books_scraper](books_scraper.md), [hn_rss](hn_rss.md)
- [`router`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/router) — [router](router.md)
- [`running_total`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/running_total) — [retail_analytics](retail_analytics.md), [sensor_gapfill](sensor_gapfill.md), [weather](weather.md)
- [`scd_type_2`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/scd_type_2) — [scd_type_2](scd_type_2.md)
- [`select_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/select_columns) — [earthquakes](earthquakes.md), [kitchen_sink](kitchen_sink.md), [partitioned_earthquakes](partitioned_earthquakes.md), [releases](releases.md), [spacex](spacex.md), [spacex_join](spacex_join.md)
- [`sort`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/sort) — [cities_distance](cities_distance.md), [earthquakes](earthquakes.md), [hn_rss](hn_rss.md), [kitchen_sink](kitchen_sink.md), [partitioned_earthquakes](partitioned_earthquakes.md), [releases](releases.md)
- [`summarize`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/summarize) — [adls_inbox](adls_inbox.md), [churn](churn.md), [countries](countries.md), [detect_changes](detect_changes.md), [external_scheduler](external_scheduler.md), [kitchen_sink](kitchen_sink.md), [market_basket](market_basket.md), [store_coverage](store_coverage.md), [titanic_complete](titanic_complete.md)
- [`tile_binning`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/tile_binning) — [titanic_complete](titanic_complete.md)
- [`transpose`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/transpose) — [pivot_unpivot](pivot_unpivot.md), [weather](weather.md)
- [`ts_filler`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/ts_filler) — [forecast_comparison](forecast_comparison.md), [sensor_gapfill](sensor_gapfill.md)
- [`type_coercer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/type_coercer) — [kitchen_sink](kitchen_sink.md), [movies_sql](movies_sql.md), [titanic_complete](titanic_complete.md)
- [`unique_dedup`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/unique_dedup) — [kitchen_sink](kitchen_sink.md), [titanic_complete](titanic_complete.md)
- [`unpivot`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/unpivot) — [pivot_unpivot](pivot_unpivot.md), [prometheus_demo](prometheus_demo.md)
- [`window_calculation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/window_calculation) — [window_calculation](window_calculation.md)
- [`xml_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/xml_parser) — [hn_xml](hn_xml.md), [rss_sensor](rss_sensor.md)

### sensor (5)

- [`adls_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/adls_monitor) — [adls_inbox](adls_inbox.md)
- [`eventhubs_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/eventhubs_monitor) — [azure_eventhubs](azure_eventhubs.md)
- [`http_poll_sensor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/http_poll_sensor) — [nba_scoreboard](nba_scoreboard.md)
- [`rss_feed_sensor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/rss_feed_sensor) — [rss_sensor](rss_sensor.md)
- [`servicebus_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/sensors/servicebus_monitor) — [azure_servicebus](azure_servicebus.md)

### external (1)

- [`external_adls_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/external_assets/external_adls_asset) — [adls_round_trip](adls_round_trip.md)

### integration (3)

- [`azure_data_factory`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/azure_data_factory) — [azure_data_factory](azure_data_factory.md), [azure_synapse](azure_synapse.md), [bicep_self_provision](bicep_self_provision.md)
- [`azure_synapse`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/azure_synapse) — [azure_synapse](azure_synapse.md)
- [`fabric_workspace`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/fabric_workspace) — [fabric_full_stack](fabric_full_stack.md)

### infrastructure (13)

- [`asset_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/schedules/asset_job) — [adls_inbox](adls_inbox.md), [external_scheduler](external_scheduler.md)
- [`bicep_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/bicep_asset) — [bicep_self_provision](bicep_self_provision.md)
- [`cloudformation_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/cloudformation_asset) — [bicep_self_provision](bicep_self_provision.md)
- [`cron_schedule`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/schedules/cron_schedule) — [bicep_self_provision](bicep_self_provision.md), [dagster_plus_to_sentinel](dagster_plus_to_sentinel.md), [duckdb_warehouse](duckdb_warehouse.md), [kitchen_sink](kitchen_sink.md)
- [`dagster_plus_to_siem_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/dagster_plus_to_siem_job) — [dagster_plus_to_sentinel](dagster_plus_to_sentinel.md)
- [`dynamic_fanout_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/dynamic_fanout_job) — [dynamic_fanout_job](dynamic_fanout_job.md)
- [`fabric_pipeline_trigger_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/fabric_pipeline_trigger_job) — [fabric_full_stack](fabric_full_stack.md)
- [`gcp_deployment_manager_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/gcp_deployment_manager_asset) — [bicep_self_provision](bicep_self_provision.md)
- [`per_file_processor_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/per_file_processor_job) — [per_file_processor](per_file_processor.md)
- [`shell_command_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/shell_command_asset) — [shell_command_job](shell_command_job.md)
- [`shell_command_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/shell_command_job) — [shell_command_job](shell_command_job.md)
- [`synapse_sql_pool_admin_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/synapse_sql_pool_admin_job) — [fabric_full_stack](fabric_full_stack.md)
- [`terraform_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/terraform_asset) — [bicep_self_provision](bicep_self_provision.md)

### analytics (34)

- [`ab_controls`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_controls) — [ab_full_pipeline](ab_full_pipeline.md)
- [`ab_test_analysis`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_test_analysis) — [ab_full_pipeline](ab_full_pipeline.md)
- [`ab_treatments`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_treatments) — [ab_full_pipeline](ab_full_pipeline.md)
- [`ab_trend`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ab_trend) — [ab_full_pipeline](ab_full_pipeline.md)
- [`anomaly_detection`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/anomaly_detection) — [stocks_anomaly](stocks_anomaly.md), [synthetic_metrics](synthetic_metrics.md)
- [`bounding_box_filter`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/bounding_box_filter) — [west_coast_cities](west_coast_cities.md)
- [`buffer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/buffer) — [store_coverage](store_coverage.md)
- [`churn_prediction`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/churn_prediction) — [churn](churn.md), [retail_ltv](retail_ltv.md)
- [`cohort_analysis`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/cohort_analysis) — [retail_analytics](retail_analytics.md)
- [`create_points`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/create_points) — [store_coverage](store_coverage.md)
- [`cross_validation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/cross_validation) — [wine_ml_pipeline](wine_ml_pipeline.md)
- [`customer_journey_mapping`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/customer_journey_mapping) — [retail_ltv](retail_ltv.md)
- [`customer_segmentation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/customer_segmentation) — [churn](churn.md)
- [`decision_tree_model`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/decision_tree_model) — [wine_ml_pipeline](wine_ml_pipeline.md)
- [`distance_calculator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/distance_calculator) — [cities_distance](cities_distance.md)
- [`k_means_clustering`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/k_means_clustering) — [iris_unsupervised](iris_unsupervised.md)
- [`logistic_regression_model`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/logistic_regression_model) — [titanic_complete](titanic_complete.md)
- [`ltv_prediction`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ltv_prediction) — [retail_ltv](retail_ltv.md)
- [`make_grid`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/make_grid) — [store_coverage](store_coverage.md)
- [`market_basket_rules`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/market_basket_rules) — [market_basket](market_basket.md)
- [`multi_touch_attribution`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/multi_touch_attribution) — [retail_ltv](retail_ltv.md)
- [`nearest_neighbors`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/nearest_neighbors) — [cities_nn](cities_nn.md)
- [`pca`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/pca) — [iris_unsupervised](iris_unsupervised.md)
- [`random_forest_model`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/random_forest_model) — [spacex_join](spacex_join.md)
- [`revenue_attribution`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/revenue_attribution) — [revenue_attribution](revenue_attribution.md)
- [`rfm_segmentation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/rfm_segmentation) — [kitchen_sink](kitchen_sink.md), [retail_analytics](retail_analytics.md), [retail_ltv](retail_ltv.md)
- [`smooth`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/smooth) — [store_coverage](store_coverage.md)
- [`spatial_cluster`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/spatial_cluster) — [airports_cluster](airports_cluster.md)
- [`spatial_join`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/spatial_join) — [store_coverage](store_coverage.md)
- [`subscription_metrics`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/subscription_metrics) — [saas_metrics](saas_metrics.md)
- [`survival_analysis`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/survival_analysis) — [subscription_survival](subscription_survival.md)
- [`text_preprocessing`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/text_preprocessing) — [arxiv_pdf](arxiv_pdf.md)
- [`time_series_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/time_series_generator) — [churn](churn.md), [forecast_comparison](forecast_comparison.md), [synthetic_metrics](synthetic_metrics.md)
- [`ts_compare`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/analytics/ts_compare) — [forecast_comparison](forecast_comparison.md)

### ai (2)

- [`embeddings_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/embeddings_generator) — [azure_search](azure_search.md)
- [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) — [ab_full_pipeline](ab_full_pipeline.md), [adls_round_trip](adls_round_trip.md), [azure_eventhubs](azure_eventhubs.md), [azure_mysql](azure_mysql.md), [azure_postgres](azure_postgres.md), [azure_redis](azure_redis.md), [azure_search](azure_search.md), [azure_sql](azure_sql.md), [azure_synapse_serverless](azure_synapse_serverless.md), [azure_tables](azure_tables.md), [bicep_self_provision](bicep_self_provision.md), [cosmosdb_round_trip](cosmosdb_round_trip.md), [kitchen_sink](kitchen_sink.md), [opentelemetry_demo](opentelemetry_demo.md), [prometheus_demo](prometheus_demo.md), [retail_analytics](retail_analytics.md), [sensor_gapfill](sensor_gapfill.md), [subscription_survival](subscription_survival.md)

### check (1)

- [`ocsf_validator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/ocsf_validator) — [dagster_plus_security_lake](dagster_plus_security_lake.md), [ocsf_security_lake](ocsf_security_lake.md)
