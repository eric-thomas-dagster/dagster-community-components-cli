# BigQuery Query — ad-hoc SELECT against the public Shakespeare dataset

**Validated end-to-end against `bigquery-public-data.samples.shakespeare`.**
Real BQ query, 20 rows returned, downstream pandas word_summary
aggregation, all in ~3s.

```
top_shakespeare_words   ← bigquery_query_asset (public BQ dataset)
        │
        └── word_summary  ← pandas (occurrences-by-word-length)
```

## Components covered (1)

| Component | What it does |
|---|---|
| [`bigquery_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_query_asset) | Run a SQL query against BigQuery, return a DataFrame. Drop-in peer of [`duckdb_query_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/duckdb_query_reader) (DuckDB) and [`database_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/database_query) (any SQLAlchemy DB). For multi-asset BQ entity import, use [`google_bigquery`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/google_bigquery). |

## Validation status

[`bigquery_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/bigquery_query_asset) validation: **live**. RUN_SUCCESS materializing
the top-20 most-frequent Shakespeare words via the public BQ sample
dataset, plus a downstream pandas summary by word length:

```
       word  occurrences  word_length
      shall        3282            5
      would        2147            5
      their        2135            5
      Enter        1977            5
        ...
```

Materialization metadata exposes `bytes_processed`, `bytes_billed`,
`cache_hit`, `slot_millis`, and the rendered query post-placeholder
substitution.

## Cost

**~$0.0001** per run. The query scans ~2.6 MB; on-demand pricing is
~$5/TB (and the first 1 TB/month is free).

## Required env var

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

The SA needs `roles/bigquery.jobUser` on the project (or `roles/owner`).
The query hits a public dataset, so no extra read role is needed —
the cost goes to the SA's project, not the public-data project.

## Run it

```bash
./setup_bigquery_query_demo.sh
cd bigquery-query-demo
uv run dg launch --assets '*'
```

## Drop-in extensions

Switch to your own table by changing the query:

```yaml
attributes:
  asset_name: daily_orders
  query: |
    SELECT customer_id, SUM(amount) AS total
    FROM `my-project.analytics.orders`
    WHERE DATE(created_at) = '{partition_key}'
    GROUP BY customer_id
  partition_type: daily
  partition_start: "2026-04-01"
```

When materializing partition `2026-05-01`, the query becomes
`WHERE DATE(created_at) = '2026-05-01'`.

Set `dry_run: true` to validate the query without scanning data —
the asset materialization metadata then shows `bytes_processed` and
an estimated USD cost. Useful in CI / linting.

## Sister components

- [`bigquery_create_table_from_query_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/bigquery_create_table_from_query_asset) — CTAS, materialize a query as a real BQ table (the transform layer).
- [`bigquery_ml_train_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_train_asset) / [`bigquery_ml_predict_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/bigquery_ml_predict_asset) — BQML: ML in pure SQL.
- [`bigquery_export_to_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/sinks/bigquery_export_to_gcs_asset) / [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) — BQ ↔ GCS bulk bridge.
- [`duckdb_query_reader`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/duckdb_query_reader) — same shape, against DuckDB.
- [`database_query`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/database_query) — generic SQL via SQLAlchemy.
