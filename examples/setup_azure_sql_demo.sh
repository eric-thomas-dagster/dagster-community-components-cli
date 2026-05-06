#!/usr/bin/env bash
# Azure SQL Database demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → dataframe_to_table writes to an Azure SQL
#   serverless database via SQLAlchemy + pymssql. Same pipeline shape as the
#   movies_sql / cars_sql demos — flip DATABASE_URL to mysql:// or postgresql://
#   and the same components land data there.
#
# Pipeline (3 components):
#   synthetic_data_generator → dataframe_to_table → (verify via az sql db query)
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.Sql provider registered:
#        az provider register --namespace Microsoft.Sql --wait
#   3. An Azure SQL serverless database — see "Provisioning" below.
#
# REQUIRED ENV VARS
#   DATABASE_URL   mssql+pymssql://<user>:<urlencoded-pass>@<server>.database.windows.net:1433/<db>
#
# COST while running
#   Serverless GP_S_Gen5_1 with auto-pause after 60min idle. Resumes
#   automatically when the demo connects. ~$5-15/mo if used heavily; near $0
#   when idle (storage only).
#
# TEARDOWN
#   az sql db delete -g dagster-demo-rg -s <server> -n demo --yes
#   az sql server delete -g dagster-demo-rg -n <server> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-sql-demo}"

if [ -z "${DATABASE_URL:-}" ]; then
  cat <<'NEED'
ERROR: DATABASE_URL not set.

To provision an Azure SQL Database serverless instance + build the URL:
    RG=dagster-demo-rg
    SQL_SERVER=dgsql$(openssl rand -hex 4)
    SQL_USER=dagsteradmin
    SQL_PASS="P$(openssl rand -hex 12)!Aa"

    az group create --name "$RG" --location eastus
    # Region capacity varies — try westus3, eastus2, centralus if eastus fails:
    az sql server create -g "$RG" -n "$SQL_SERVER" -l westus3 \
        --admin-user "$SQL_USER" --admin-password "$SQL_PASS"

    # Firewall: allow Azure services + your current IP
    az sql server firewall-rule create -g "$RG" -s "$SQL_SERVER" -n AllowAzure \
        --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0
    MYIP=$(curl -s https://api.ipify.org)
    az sql server firewall-rule create -g "$RG" -s "$SQL_SERVER" -n AllowMyIP \
        --start-ip-address "$MYIP" --end-ip-address "$MYIP"

    az sql db create -g "$RG" -s "$SQL_SERVER" -n demo \
        --edition GeneralPurpose --family Gen5 --capacity 1 \
        --compute-model Serverless --auto-pause-delay 60

    # URL-encode the password and build DATABASE_URL
    PASS_ENC=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$SQL_PASS'))")
    export DATABASE_URL="mssql+pymssql://$SQL_USER:$PASS_ENC@$SQL_SERVER.database.windows.net:1433/demo"
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy pymssql
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
  asset_name: orders_in_azure_sql
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
    SQL_SERVER=<your-server>; SQL_PASS=<your-pass>
    az sql db query -g dagster-demo-rg -s "\$SQL_SERVER" -n demo \\
        --auth-type SqlPassword --username dagsteradmin --password "\$SQL_PASS" \\
        --query-file <(echo 'SELECT TOP 5 * FROM orders ORDER BY total DESC')

Or via Python directly:
    uv run python -c "import pandas, sqlalchemy; \\
        eng = sqlalchemy.create_engine('\$DATABASE_URL'); \\
        print(pandas.read_sql('SELECT TOP 5 * FROM orders ORDER BY total DESC', eng))"

Teardown:
    az sql db delete -g dagster-demo-rg -s <server> -n demo --yes
    az sql server delete -g dagster-demo-rg -n <server> --yes
MSG
