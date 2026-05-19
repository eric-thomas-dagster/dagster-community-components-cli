#!/usr/bin/env bash
# Azure Database for PostgreSQL Flexible Server demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → dataframe_to_table writes to an Azure
#   PostgreSQL Flexible Server (Burstable B1ms, ~$13/mo). Same pipeline shape
#   as azure_sql / mysql / movies_sql — flip the URL prefix, ship to a
#   different SQL backend.
#
# Pipeline (2 components):
#   synthetic_data_generator → dataframe_to_table → Azure PostgreSQL ('orders')
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.DBforPostgreSQL provider registered:
#        az provider register --namespace Microsoft.DBforPostgreSQL --wait
#   3. A Flexible Server + database — see "Provisioning" below.
#
# REQUIRED ENV VARS
#   DATABASE_URL   postgresql+psycopg2://<user>:<urlencoded-pass>@<server>.postgres.database.azure.com:5432/demo?sslmode=require
#
# COST while running
#   B1ms Burstable: ~$0.018/hr (~$13/mo). Cannot auto-pause; stop it manually
#   or delete the RG when done.
#
# TEARDOWN
#   az postgres flexible-server delete -g dagster-demo-rg -n <server> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-postgres-demo}"

if [ -z "${DATABASE_URL:-}" ]; then
  cat <<'NEED'
ERROR: DATABASE_URL not set.

To provision an Azure PostgreSQL Flexible Server + build the URL:
    RG=dagster-demo-rg
    PG_SERVER=dgpg$(openssl rand -hex 4)
    PG_USER=dagsteradmin
    PG_PASS="P$(openssl rand -hex 12)!Aa"

    az group create --name "$RG" --location eastus 2>/dev/null || true
    az postgres flexible-server create -g "$RG" -n "$PG_SERVER" \
        --admin-user "$PG_USER" --admin-password "$PG_PASS" \
        --sku-name Standard_B1ms --tier Burstable \
        --storage-size 32 --version 16 \
        --public-access 0.0.0.0 --yes
    az postgres flexible-server db create -g "$RG" -s "$PG_SERVER" -d demo

    # Allow your current IP
    MYIP=$(curl -s https://api.ipify.org)
    az postgres flexible-server firewall-rule create -g "$RG" -n "$PG_SERVER" \
        --rule-name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

    PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$PG_PASS")
    export DATABASE_URL="postgresql+psycopg2://$PG_USER:$PASS_ENC@$PG_SERVER.postgres.database.azure.com:5432/demo?sslmode=require"
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy psycopg2-binary
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 2 community components"
$CLI add synthetic_data_generator --auto-install
$CLI add dataframe_to_table       --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 100
  random_state: 42
  description: 100 synthetic e-commerce orders
  group_name: ingest
EOF

cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: orders_in_postgres
  upstream_asset_key: orders_raw
  table_name: orders
  database_url_env_var: DATABASE_URL
  if_exists: replace
  drop_timezone: true
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify the table landed:
    uv run python -c "import pandas, sqlalchemy; \\
        eng = sqlalchemy.create_engine('\$DATABASE_URL'); \\
        print(pandas.read_sql('SELECT * FROM orders ORDER BY total DESC LIMIT 5', eng))"

Teardown:
    az postgres flexible-server delete -g dagster-demo-rg -n <server> --yes
MSG
