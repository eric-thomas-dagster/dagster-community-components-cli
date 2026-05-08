#!/usr/bin/env bash
# Local IO + sources/sinks demo — validates IO managers and source/sink
# components that don't require any cloud creds.
#
# COST: $0 — entirely local

set -euo pipefail
PROJECT_DIR="${1:-local-io-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas duckdb polars deltalake pyiceberg pylance pyarrow
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing local IO + source/sink components"
for c in local_csv_io_manager local_json_io_manager local_parquet_io_manager \
         duckdb_polars_io_manager polars_io_manager delta_lake_io_manager \
         deltalake_polars_io_manager iceberg_io_manager lance_io_manager \
         dataframe_from_csv duckdb_query_reader duckdb_table_writer; do
  $CLI add $c --auto-install || echo "FAILED: $c"
done

echo ">>> Writing inline source data"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import os
import pandas as pd
import dagster as dg


@dg.asset(group_name="ingest")
def seed_csv_file() -> str:
    """Seeder: write a CSV that dataframe_from_csv can read."""
    out = "/tmp/local_io_demo_seed.csv"
    pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Carol", "Dave", "Eve"],
        "amount": [100.0, 200.0, 300.0, 400.0, 500.0],
    }).to_csv(out, index=False)
    return out


@dg.asset(group_name="ingest")
def seed_duckdb() -> str:
    """Seeder: write a DuckDB file with a sample table for duckdb_query_reader."""
    import duckdb
    path = "/tmp/local_io_demo.duckdb"
    if os.path.exists(path):
        os.remove(path)
    con = duckdb.connect(path)
    df = pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "category": ["a", "b", "a", "c", "b"],
        "amount": [10, 20, 30, 40, 50],
    })
    con.register("df", df)
    con.execute("CREATE TABLE sample AS SELECT * FROM df")
    con.close()
    return path


@dg.asset(group_name="ingest")
def writer_input_df() -> pd.DataFrame:
    """DataFrame to feed duckdb_table_writer."""
    return pd.DataFrame({
        "order_id": [101, 102, 103],
        "status": ["ok", "ok", "fail"],
        "value": [99.9, 88.8, 77.7],
    })


defs = dg.Definitions(assets=[seed_csv_file, seed_duckdb, writer_input_df])
PYEOF

echo ">>> Writing IO manager defs (configured-only validation)"
write_yaml() {
  local d="$1"; local body="$2"
  mkdir -p "src/$PKG/defs/$d"
  echo -e "$body" > "src/$PKG/defs/$d/defs.yaml"
}

# --- IO managers (configuration only — each binds a unique resource_key) ---
write_yaml "local_csv_io_manager" "type: $PKG.components.local_csv_io_manager.component.LocalCsvIOManagerComponent
attributes:
  resource_key: csv_io
  base_dir: /tmp/local_io_demo/csv
  create_dir: true"

write_yaml "local_json_io_manager" "type: $PKG.components.local_json_io_manager.component.LocalJsonIOManagerComponent
attributes:
  resource_key: json_io
  base_dir: /tmp/local_io_demo/json
  create_dir: true"

write_yaml "local_parquet_io_manager" "type: $PKG.components.local_parquet_io_manager.component.LocalParquetIOManagerComponent
attributes:
  resource_key: parquet_io
  base_dir: /tmp/local_io_demo/parquet
  create_dir: true"

write_yaml "duckdb_polars_io_manager" "type: $PKG.components.duckdb_polars_io_manager.component.DuckDBPolarsIOManagerComponent
attributes:
  resource_key: duckdb_polars_io
  database: /tmp/local_io_demo/duckdb_polars.duckdb"

write_yaml "polars_io_manager" "type: $PKG.components.polars_io_manager.component.PolarsIOManagerComponent
attributes:
  resource_key: polars_io
  base_dir: /tmp/local_io_demo/polars"

write_yaml "delta_lake_io_manager" "type: $PKG.components.delta_lake_io_manager.component.DeltaLakeIOManagerComponent
attributes:
  resource_key: delta_io
  root_uri: /tmp/local_io_demo/delta"

write_yaml "deltalake_polars_io_manager" "type: $PKG.components.deltalake_polars_io_manager.component.DeltaLakePolarsIOManagerComponent
attributes:
  resource_key: delta_polars_io
  root_uri: /tmp/local_io_demo/delta_polars"

write_yaml "iceberg_io_manager" "type: $PKG.components.iceberg_io_manager.component.IcebergIOManagerComponent
attributes:
  resource_key: iceberg_io
  catalog_name: default
  namespace: demo"

write_yaml "lance_io_manager" "type: $PKG.components.lance_io_manager.component.LanceIOManagerComponent
attributes:
  resource_key: lance_io
  base_path: /tmp/local_io_demo/lance"

# --- Source/sink components in active round-trips ---
write_yaml "dataframe_from_csv" "type: $PKG.components.dataframe_from_csv.component.DataframeFromCsvComponent
attributes:
  asset_name: csv_data
  file_path: /tmp/local_io_demo_seed.csv
  deps:
    - seed_csv_file
  group_name: round_trip"

write_yaml "duckdb_query_reader" "type: $PKG.components.duckdb_query_reader.component.DuckDBQueryReaderComponent
attributes:
  asset_name: duckdb_query_result
  database_path: /tmp/local_io_demo.duckdb
  query: 'SELECT category, SUM(amount) AS total FROM sample GROUP BY category'
  deps:
    - seed_duckdb
  group_name: round_trip"

write_yaml "duckdb_table_writer" "type: $PKG.components.duckdb_table_writer.component.DuckDBTableWriterComponent
attributes:
  asset_name: orders_in_duckdb
  upstream_asset_key: writer_input_df
  database_path: /tmp/local_io_demo_writer.duckdb
  table_name: orders
  write_mode: replace
  group_name: round_trip"

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

9 local IO managers (configured) + 3 source/sink components in active round-trips.
\$0 cost — all local.
MSG
