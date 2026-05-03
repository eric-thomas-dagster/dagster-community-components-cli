#!/usr/bin/env bash
# DuckDB Warehouse demo — IO manager + cron schedule end-to-end.
#
# A REAL Dagster project (not just an asset graph): assets persisted to
# DuckDB via duckdb_io_manager, a downstream summary asset that loads
# the upstream DataFrame back through the IO manager, and a daily
# cron_schedule that re-materializes the chain at 02:00 local time.
#
# Pipeline (4 components, all autoloaded by `dg`):
#   csv_file_ingestion → duckdb_io_manager (resource)
#                     → iris_summary (Python asset, downstream)
#                     → cron_schedule (job + 02:00 schedule)
#
# Outputs land as TWO tables in /tmp/iris_warehouse.duckdb (queryable
# directly with the `duckdb` CLI), not as CSV files.

set -euo pipefail
PROJECT_DIR="${1:-duckdb-warehouse-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas dagster-duckdb-pandas duckdb
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 3 community components"
$CLI add csv_file_ingestion --auto-install
$CLI add duckdb_io_manager  --auto-install
$CLI add cron_schedule      --auto-install

echo ">>> Writing defs.yaml"

cat > "src/$PKG/defs/csv_file_ingestion/defs.yaml" <<EOF
type: $PKG.components.csv_file_ingestion.component.CSVFileIngestionComponent
attributes:
  asset_name: iris_table
  file_path: https://raw.githubusercontent.com/mwaskom/seaborn-data/master/iris.csv
  description: UCI Iris (3 species × 50 flowers, 4 numeric features) — persisted to DuckDB
  group_name: warehouse
EOF

cat > "src/$PKG/defs/duckdb_io_manager/defs.yaml" <<EOF
type: $PKG.components.duckdb_io_manager.component.DuckDBIOManagerComponent
attributes:
  resource_key: io_manager
  database: /tmp/iris_warehouse.duckdb
  schema_name: main
EOF

cat > "src/$PKG/defs/cron_schedule/defs.yaml" <<EOF
type: $PKG.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: nightly_iris_warehouse_refresh
  cron_expression: "0 2 * * *"
  asset_keys: [iris_table, iris_summary]
  execution_timezone: America/Los_Angeles
  default_status: STOPPED
  tags:
    team: data-platform
    purpose: nightly-refresh
EOF

# A Python asset (downstream) that reads iris_table BACK through the IO manager
# and computes per-species summary stats. Demonstrates round-trip IO.
cat > "src/$PKG/defs/iris_summary.py" <<'EOF'
"""Per-species iris summary — depends on iris_table loaded via the DuckDB IO manager."""
import pandas as pd
from dagster import asset


@asset(group_name="warehouse")
def iris_summary(iris_table: pd.DataFrame) -> pd.DataFrame:
    return iris_table.groupby("species").agg(
        n=("sepal_length", "count"),
        mean_sepal=("sepal_length", "mean"),
        mean_petal=("petal_length", "mean"),
    ).reset_index()
EOF

cat <<MSG

>>> Setup complete.

Materialize once (manual seed):
    cd $PROJECT_DIR && uv run dg launch --assets '*'

Inspect the warehouse directly:
    uv run python -c "import duckdb; print(duckdb.connect('/tmp/iris_warehouse.duckdb').execute('SHOW TABLES').fetchall()); print(duckdb.connect('/tmp/iris_warehouse.duckdb').execute('SELECT * FROM iris_summary').fetch_df())"

Then start the UI:
    cd $PROJECT_DIR && uv run dg dev
    # Jobs tab → 'nightly_iris_warehouse_refresh_job'
    # Schedules tab → 'nightly_iris_warehouse_refresh' (STOPPED — toggle to enable)
MSG
