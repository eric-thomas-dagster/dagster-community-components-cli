#!/usr/bin/env bash
# OData ingestion → summarize → parquet demo.
# Validated end-to-end against the public Northwind sample at services.odata.org.
# Zero credentials required.
#
# Pipeline:
#
#   [services.odata.org/V4/Northwind/Customers]
#                  │
#                  ▼  odata_ingestion (GET, $select, $top=10)
#   northwind_customers      (10 rows × 5 cols pandas DataFrame)
#                  │
#                  ▼  summarize (group_by Country, count)
#   customers_by_country
#                  │
#                  ▼  dataframe_to_parquet
#   ./output/customers_by_country.parquet

set -euo pipefail

PROJECT_DIR="${1:-odata-pipeline-demo}"

echo ">>> 1/4  Scaffolding Dagster project ($PROJECT_DIR)"
uvx create-dagster project "$PROJECT_DIR" --uv-sync >/dev/null

cd "$PROJECT_DIR"
PKG=$(python -c 'import os; print(os.path.basename(os.getcwd()).replace("-","_"))')

echo ">>> 2/4  Installing community components: odata_ingestion + summarize + dataframe_to_parquet"
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add odata_ingestion >/dev/null
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add summarize >/dev/null
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
  dagster-component add dataframe_to_parquet >/dev/null

echo ">>> 3/4  Writing defs.yaml — 3 components, all autoloaded by dg"

mkdir -p "src/$PKG/defs/northwind_customers" \
         "src/$PKG/defs/customers_by_country" \
         "src/$PKG/defs/customers_by_country_parquet"

# 1. Ingest from Northwind (no auth, $top=10 keeps it fast)
cat > "src/$PKG/defs/northwind_customers/defs.yaml" <<EOF
type: $PKG.components.odata_ingestion.component.ODataIngestionComponent
attributes:
  asset_name: northwind_customers
  service_url: https://services.odata.org/V4/Northwind/Northwind.svc
  entity_set: Customers
  odata_version: v4
  auth_type: none
  top: 10
  select: CustomerID,CompanyName,ContactName,Country,City
  group_name: northwind
  kinds: [odata]
EOF

# 2. Group by Country, count customers
cat > "src/$PKG/defs/customers_by_country/defs.yaml" <<EOF
type: $PKG.components.summarize.component.SummarizeComponent
attributes:
  asset_name: customers_by_country
  upstream_asset_key: northwind_customers
  group_by: [Country]
  aggregations:
    n_customers: {col: CustomerID, agg: count}
  group_name: northwind
EOF

# 3. Write curated parquet
cat > "src/$PKG/defs/customers_by_country_parquet/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_parquet.component.DataframeToParquetComponent
attributes:
  asset_name: customers_by_country_parquet
  upstream_asset_key: customers_by_country
  file_path: "./output/customers_by_country.parquet"
  compression: snappy
  group_name: northwind
EOF

echo ">>> 4/4  Running dg check defs"
uv run dg check defs

echo ""
echo "==============================================="
echo "Demo project ready in ./$PROJECT_DIR"
echo ""
echo "Run end-to-end:"
echo "  cd $PROJECT_DIR"
echo "  uv run dg launch --assets customers_by_country_parquet"
echo ""
echo "Or launch the UI:"
echo "  cd $PROJECT_DIR"
echo "  uv run dg dev   # then http://localhost:3000"
echo "==============================================="
