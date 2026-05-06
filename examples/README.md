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

### Orchestration + workflow

| Demo | Components exercised | Infra needed | ~Cost |
|---|---|---|---|
| [Azure Data Factory](azure_data_factory.md) | `azure_data_factory` (import + trigger ADF pipelines, capture per-activity metadata) | ADF instance + service principal | $0 idle, $0.001/activity |

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

## Component coverage

Across the **55 demos**, these exercise **100+ distinct components** spanning
every category in the registry. The validator harness at
[`tools/validate_demos.py`](../tools/validate_demos.py) runs every no-auth demo
end-to-end on each release; the auth-required ones get manual validation
notes recorded in their walkthroughs.
