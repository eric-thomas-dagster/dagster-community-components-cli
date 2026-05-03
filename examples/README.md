# Examples

End-to-end Dagster pipelines built entirely from community components — no
custom Python beyond `model_validate({...})` calls. Each demo:

- Hits a **public dataset or API** (no auth, no API key).
- Installs every component via `dagster-component add`.
- Materializes a small but real pipeline you can run locally in under a minute.

| Demo | Pipeline | Highlights |
|---|---|---|
| [Palmer Penguins](penguins.md) | csv → impute → onehot → scale → parquet | Canonical ML preprocessing |
| [USGS Earthquakes](earthquakes.md) | rest → flatten → select → sort → json | REST + nested JSON |
| [Earthquakes (partitioned)](partitioned_earthquakes.md) | same, daily-partitioned | Backfillable date range |
| [SpaceX Launches](spacex.md) | rest → select → datetime → rank → excel | Datetime parsing + ranking |
| [REST Countries](countries.md) | rest → formula → summarize → json | Computed columns + rollup |
| [NYC Weather](weather.md) | rest → datetime → running_total → transpose → csv | Columnar API + cumulative + pivot |
| [Dagster GitHub Releases](releases.md) | rest → select → datetime → filter → sort → parquet | Filter + sort + parquet |
| [Vintage Cars → SQL](cars_sql.md) | rest → datetime → formula → dataframe_to_table | Land a DataFrame in SQLite (or any SQLAlchemy DB) |
| [Airline Passengers — Forecast](passengers_forecast.md) | csv → datetime → ets_forecast → csv | Time-series forecasting (ETS / Holt-Winters) |
| [Iris — Unsupervised (PCA + K-Means)](iris_unsupervised.md) | csv → scale → pca → k_means → csv | Dimensionality reduction + clustering on the classic dataset |
| [SpaceX — Multi-Source Join](spacex_join.md) | rest × 2 → dataframe_join → select → csv | Fan-in two REST sources, join on a FK |
| [Stocks — Anomaly Detection](stocks_anomaly.md) | csv → anomaly_detection → csv | Per-ticker z-score outlier flagging |
| [Books — Partitioned Web Scraper](books_scraper.md) | rest (text) → html_parser → json | Multi-page HTML scrape, one partition per page |
| [UCI Retail — LTV (CDP)](retail_ltv.md) | csv → cleanse → formula → ltv → csv | Customer lifetime value on 542k real transactions |
| [Airports — Spatial Clustering](airports_cluster.md) | csv → spatial_cluster → csv | DBSCAN on lat/lng (haversine, real km) |
| [Hacker News — RSS Parsing](hn_rss.md) | rest (text) → regex (split) → regex (extract) → filter → csv | XML feed → structured rows |
| [arXiv — PDF Extraction](arxiv_pdf.md) | csv → pdf_text_extractor → formula → csv | Document → text → word counts |
| [SaaS Metrics (synthetic Stripe)](saas_metrics.md) | csv → subscription_metrics → csv | MRR / ARR / churn / LTV / ARPU |
| [Revenue Attribution](revenue_attribution.md) | csv × 2 → revenue_attribution → csv | Linear attribution across marketing channels |
| [Synthetic Time-Series + Anomalies](synthetic_metrics.md) | time_series_generator → anomaly_detection → csv | No-upstream synthetic data via the registry's generator |
| [Hacker News (xml_parser)](hn_xml.md) | rest (text) → xml_parser (findall) → array_exploder → csv | Same as the regex variant, but xpath all the way down |
| [GitHub Search — JSONPath](github_jsonpath.md) | rest → nested_field_extractor → json_path_extractor → csv | Two ways to flatten nested JSON |
| [US Cities — Pairwise Distances](cities_distance.md) | csv × 2 → cross-join → distance_calculator → filter → sort → csv | Haversine distance matrix from a 6-component pipeline |
| [Churn Prediction (synthetic)](churn.md) | csv → churn_prediction → csv | Rule-based scoring with interpretable risk factors |
| [Cities — Nearest Neighbors](cities_nn.md) | csv → nearest_neighbors → csv | Top-3 closest cities per row (sklearn KD-tree) |
| [Movies — SQL Source](movies_sql.md) | dataframe_from_sql → transforms → csv | Read from SQLite, fan into multiple sinks |
| [Wikipedia — Multi-page Scraper](wiki_scraper.md) | rest × N → html_parser → csv | Scrape a list of wiki pages in parallel |
| [Subscription Survival](subscription_survival.md) | synthetic_data_generator → survival_analysis → csv | Kaplan-Meier survival on synthetic SaaS subscriptions |
| [Regional Orders Union](regional_orders.md) | csv × 3 → dataframe_union → csv | Merge multi-region order extracts with mismatched columns |
| [Sensor Gap-Fill](sensor_gapfill.md) | synthetic_data_generator → ts_filler → running_total → csv | Fill missing hourly readings + cumulative metrics |
| [A/B Full Pipeline](ab_full_pipeline.md) | 10 components | Assignment + analysis + trend + sample-size for next experiment |
| [Forecast Comparison](forecast_comparison.md) | time_series → arima + ets → ts_compare | Head-to-head ARIMA vs ETS on the same series |
| [Market Basket](market_basket.md) | csv → market_basket_rules → filter → csv | Apriori association rules with lift > 1.5 filter |
| [Retail Analytics](retail_analytics.md) | 7 components, 3 parallel branches | RFM segmentation + cohort analysis + running spend |
| [Titanic Complete](titanic_complete.md) | 12 components | Full DS workflow: ingest → quality → ETL → model → 3 outputs |
| [Wine ML Pipeline](wine_ml_pipeline.md) | 8 components | Feature scaling → train/test split → decision tree + cross-validation |
| [Store Coverage (geospatial)](store_coverage.md) | 9 components | Buffer + spatial_join + summarize: customer-to-store coverage |
| [West Coast Cities Filter](west_coast_cities.md) | csv → bounding_box_filter → csv | Geographic filter to a lat/lng bounding box |
| [RSS Sensor](rss_sensor.md) | rss_feed_sensor → rest → xml_parser → csv | Sensor-driven HN frontpage ingestion (no auth) |
| [NBA Scoreboard](nba_scoreboard.md) | http_poll_sensor → rest → json_path → csv | `http_poll_sensor` with targeted hashing — fires on real score changes, not server-timestamp churn. Hits an undocumented public JSON endpoint. |

## Component coverage

Across the **40 demos**, these exercise **70 distinct components** spanning 6 categories:

- **ingestion** (2) — `csv_file_ingestion`, `rest_api_fetcher`
- **transformation** (31) — `arima_forecast`, `array_exploder`, `create_samples`, `data_cleansing`, `dataframe_join`, `dataframe_union`, `datetime_parser`, `ets_forecast`, `feature_scaler`, `filter`, `formula`, `html_parser`, `imputation`, `json_flatten`, `json_path_extractor`, `nested_field_extractor`, `one_hot_encoding`, `outlier_clipper`, `pdf_text_extractor`, `rank`, `regex_parser`, `running_total`, `select_columns`, `sort`, `summarize`, `tile_binning`, `transpose`, `ts_filler`, `type_coercer`, `unique_dedup`, `xml_parser`
- **analytics** (29) — `ab_controls`, `ab_test_analysis`, `ab_treatments`, `ab_trend`, `anomaly_detection`, `bounding_box_filter`, `buffer`, `churn_prediction`, `cohort_analysis`, `create_points`, `cross_validation`, `decision_tree_model`, `distance_calculator`, `k_means_clustering`, `logistic_regression_model`, `ltv_prediction`, `make_grid`, `market_basket_rules`, `nearest_neighbors`, `pca`, `revenue_attribution`, `rfm_segmentation`, `smooth`, `spatial_cluster`, `spatial_join`, `subscription_metrics`, `survival_analysis`, `time_series_generator`, `ts_compare`
- **ai** (1) — `synthetic_data_generator`
- **sensor** (2) — `http_poll_sensor`, `rss_feed_sensor`
- **sink** (5) — `dataframe_to_csv`, `dataframe_to_excel`, `dataframe_to_json`, `dataframe_to_parquet`, `dataframe_to_table`

## How they're built

Each demo is a single Bash script (`setup_*.sh`) that:

1. `uvx create-dagster project <name>` — scaffolds a canonical Dagster project
2. `uv add`s any format-specific libs (pyarrow, openpyxl, etc.)
3. `dagster-component add <id> --auto-install`s each component. The
   class files (`component.py`, `schema.json`, `README.md`) land in
   `src/<pkg>/components/<id>/`; the configured instance lands in
   `src/<pkg>/defs/<id>/defs.yaml` — the canonical `create-dagster` split.
4. Writes a `defs.yaml` per component with demo-specific attributes —
   `dg`'s autoloader picks them up; no `definitions.py` glue
5. Prints the run command (`dg launch --assets '*'`) + an inspect snippet

Run any demo:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_<name>_demo.sh | bash
cd <name>-demo
uv run dg launch --assets '*'
```

## Why these exist

Each demo doubles as an **integration test** that exercises a different
combination of source / transform / sink. Several real component bugs
(silent NaN on tz-aware datetimes in Excel, columnar dict misinterpretation
in the REST fetcher, silent expression failures in `multi_field_formula`)
were surfaced and fixed by the act of building these.
