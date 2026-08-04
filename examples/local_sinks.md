# Local sinks — round-trip a DataFrame to 5 file/table formats
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Validated end-to-end** — RUN_SUCCESS in seconds. Same `orders`
DataFrame fans out to 5 sinks; output files all written and inspectable
locally.

```
orders (synthetic source: 30 rows × 5 cols)
       │
       ├── dataframe_to_csv      → /tmp/local_sinks_demo/orders.csv
       ├── dataframe_to_parquet  → /tmp/local_sinks_demo/orders.parquet
       ├── dataframe_to_json     → /tmp/local_sinks_demo/orders.json
       ├── dataframe_to_excel    → /tmp/local_sinks_demo/orders.xlsx
       └── dataframe_to_table    → SQLite at /tmp/local_sinks_demo/orders.db
                                      (table: orders)
```

## Cost

**$0.** Pure local file + SQLite writes. No network, no cloud creds.

## Components used

| Component | Output |
|---|---|
| `dataframe_to_csv` | CSV file |
| `dataframe_to_parquet` | Parquet (snappy compression by default) |
| `dataframe_to_json` | JSON (one record per row by default) |
| `dataframe_to_excel` | XLSX |
| `dataframe_to_table` | SQL table via SQLAlchemy (SQLite here, but works against PostgreSQL / MySQL / DuckDB / etc.) |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_local_sinks_demo.sh | bash
cd local-sinks-demo
uv run dg launch --assets '*'

ls /tmp/local_sinks_demo/
# orders.csv  orders.parquet  orders.json  orders.xlsx  orders.db

sqlite3 /tmp/local_sinks_demo/orders.db "SELECT count(*) FROM orders;"
# 30
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Cloud-backed sinks (separate demos required)

The full sink family has 23 components. The 18 not exercised here need
cloud credentials or specialized backends:

- **Object stores:** `dataframe_to_s3`, `dataframe_to_gcs`, `dataframe_to_adls`
- **Warehouses:** `dataframe_to_bigquery`, `dataframe_to_snowflake`,
  `dataframe_to_redshift`, `dataframe_to_databricks`, `dataframe_to_fabric_lakehouse`,
  `dataframe_to_kusto`, `dataframe_to_azure_table`
- **Streaming:** `dataframe_to_eventhub`, `dataframe_to_servicebus`
- **Observability:** `dataframe_to_dynatrace_events`, `dataframe_to_newrelic_logs`,
  `dataframe_to_otlp_logs`, `dataframe_to_otlp_metrics`, `dataframe_to_otlp_traces`,
  `dataframe_to_prometheus`
- **Generic:** `dlt_dataframe_writer`

Each is a candidate for its own integration-specific demo (most need
either a localstack/wiremock + cloud-credentials path or a real
cloud sandbox).

## See also

<!-- TODO: link related walkthroughs -->
