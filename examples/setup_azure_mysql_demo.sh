#!/usr/bin/env bash
# Azure Database for MySQL Flexible Server demo.
#
# WHAT THIS DEMONSTRATES
#   Synthetic e-commerce orders → dataframe_to_table writes to an Azure
#   MySQL Flexible Server (Burstable B1ms, ~$13/mo). Same pipeline shape as
#   azure_sql / azure_postgres — flip the URL prefix, same components.
#
# Pipeline (2 components):
#   synthetic_data_generator → dataframe_to_table → Azure MySQL ('orders')
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.DBforMySQL provider registered:
#        az provider register --namespace Microsoft.DBforMySQL --wait
#   3. A Flexible Server + database — see "Provisioning" below.
#
# REQUIRED ENV VARS
#   DATABASE_URL   mysql+pymysql://<user>:<urlencoded-pass>@<server>.mysql.database.azure.com:3306/demo
#
# NOTE: This demo disables `require_secure_transport` on the server to keep
# the URL simple. For production:
#   mysql+pymysql://...?ssl_ca=/path/to/DigiCertGlobalRootCA.crt.pem
# (Azure publishes the cert at https://learn.microsoft.com/azure/mysql/...)
#
# COST while running
#   B1ms Burstable: ~$0.018/hr (~$13/mo). Cannot auto-pause; stop manually or
#   delete the RG when done.
#
# TEARDOWN
#   az mysql flexible-server delete -g dagster-demo-rg -n <server> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-mysql-demo}"

if [ -z "${DATABASE_URL:-}" ]; then
  cat <<'NEED'
ERROR: DATABASE_URL not set.

To provision an Azure MySQL Flexible Server + build the URL:
    RG=dagster-demo-rg
    MY_SERVER=dgmy$(openssl rand -hex 4)
    MY_USER=dagsteradmin
    MY_PASS="P$(openssl rand -hex 12)!Aa"

    az group create --name "$RG" --location eastus 2>/dev/null || true
    az mysql flexible-server create -g "$RG" -n "$MY_SERVER" \
        --admin-user "$MY_USER" --admin-password "$MY_PASS" \
        --sku-name Standard_B1ms --tier Burstable \
        --storage-size 32 --version 8.0.21 \
        --public-access 0.0.0.0 --yes
    az mysql flexible-server db create -g "$RG" -s "$MY_SERVER" -d demo

    MYIP=$(curl -s https://api.ipify.org)
    az mysql flexible-server firewall-rule create -g "$RG" -n "$MY_SERVER" \
        --rule-name AllowMyIP --start-ip-address "$MYIP" --end-ip-address "$MYIP"

    # Demo simplification: disable require_secure_transport so we can use a
    # simple pymysql URL without bundling a DigiCert root CA file.
    az mysql flexible-server parameter set -g "$RG" --server-name "$MY_SERVER" \
        --name require_secure_transport --value OFF

    PASS_ENC=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$MY_PASS")
    export DATABASE_URL="mysql+pymysql://$MY_USER:$PASS_ENC@$MY_SERVER.mysql.database.azure.com:3306/demo"
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy pymysql cryptography
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
  asset_name: orders_in_mysql
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
    az mysql flexible-server delete -g dagster-demo-rg -n <server> --yes
MSG
