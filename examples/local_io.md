# Local IO managers — 9 IO managers + 3 source/sink components round-tripping

**Validated end-to-end** — RUN_SUCCESS materializing through every IO
manager configured in the demo. $0 cost — all local.

```
seed_csv_file / seed_duckdb / writer_input_df  (synthetic sources)
       │
       ├── csv_data           ← DataframeFromCsvComponent (file-based source)
       ├── orders_in_duckdb   ← DuckDBTableWriterComponent (writes to local DuckDB)
       └── duckdb_query_result ← DuckDBQueryReaderComponent (reads back from DuckDB)

IO managers configured (all local; pick one to swap behavior):
  - local_csv_io_manager       — CSV files at /tmp/local_io_demo
  - local_json_io_manager      — JSON files
  - local_parquet_io_manager   — Parquet files (snappy)
  - polars_io_manager          — Polars round-trip
  - duckdb_polars_io_manager   — DuckDB-backed Polars
  - delta_lake_io_manager      — Delta Lake (LocalConfig)
  - deltalake_polars_io_manager — Delta Lake + Polars
  - iceberg_io_manager         — Iceberg with PyArrow
  - duckdb_io_manager          — DuckDB-backed pandas/polars
```

## Components covered (12)

The 9 `*_io_manager` components let you swap how Dagster persists asset
values without changing your asset code. The 3 `dataframe_*` /
`duckdb_*` components are file/SQL-shaped sources and sinks that pair
naturally with the IO managers.

## Cost

**$0.** Pure local file + DuckDB writes. No network.

## Run it

```bash
./setup_local_io_demo.sh
cd local-io-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## What got fixed in the registry to get this green

Validating this demo surfaced two real component bugs in the registry,
both fixed:

- **[`iceberg_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/iceberg_io_manager)**: import path was wrong
  (`dagster_iceberg.io_manager.pyarrow.IcebergPyarrowIOManager` → the
  actual `dagster_iceberg.io_manager.arrow.PyArrowIcebergIOManager`).
- **[`deltalake_polars_io_manager`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/io_managers/deltalake_polars_io_manager)**: `storage_options` was being passed
  as `None` or `{}`, both of which the underlying `deltalake-storage`
  rejects via discriminated-union validation. Now picks
  `LocalConfig` / `S3Config` / `AzureConfig` / `GcsConfig` based on
  the URI scheme + credential env vars.

## Cloud-backed IO managers (separate setups required)

The remaining cloud IO managers (`s3_*`, `gcs_*`, `adls_*`,
`bigquery_*`, `snowflake_*`) need real cloud credentials. Each is a
candidate for its own focused demo with localstack / wiremock or a
real cloud sandbox.
