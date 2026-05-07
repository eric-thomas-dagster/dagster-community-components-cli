#!/usr/bin/env bash
# Azure Key Vault demo — secrets-driven SQL connection.
#
# WHAT THIS DEMONSTRATES
#   The right enterprise pattern for credentials: load DB password from
#   Key Vault at runtime via the key_vault_resource. Avoids env-var
#   sprawl for sensitive values; the SP just needs the "Key Vault Secrets
#   User" role (RBAC) to fetch.
#
#   Pipeline (3 components):
#     synthetic_data_generator
#         │
#         ▼  custom op uses key_vault_resource to fetch postgres password
#     dataframe_to_table     (Azure Postgres URL built from the secret)
#         │
#         ▼
#     orders table in Postgres
#
# PREREQS
#   1. Azure Key Vault (RBAC mode), with:
#      - 'Key Vault Secrets User' role granted to the principal
#      - A secret named 'postgres-password' with the DB password
#   2. Azure Postgres / SQL Server / etc. — anywhere SQLAlchemy speaks
#
# REQUIRED ENV VARS
#   KV_VAULT_URL              https://<vault>.vault.azure.net
#   AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET   (or use mngd identity)
#   PG_HOST, PG_USER          host + user (PASSWORD comes from KV)
#   PG_DATABASE
#
# COST
#   ~$0.03 per 10K secret operations + $0 vault storage. Pennies.

set -euo pipefail
PROJECT_DIR="${1:-key-vault-demo}"

missing=()
[ -z "${KV_VAULT_URL:-}" ]   && missing+=("KV_VAULT_URL")
[ -z "${PG_HOST:-}" ]        && missing+=("PG_HOST")
[ -z "${PG_USER:-}" ]        && missing+=("PG_USER")
[ -z "${PG_DATABASE:-}" ]    && missing+=("PG_DATABASE")
if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars.

Quick provisioning (re-uses existing KV + Postgres if you have them):

    KV=mykv-$(openssl rand -hex 3)
    az keyvault create -g dagster-demo-rg -n "$KV" -l eastus --enable-rbac-authorization true
    ME=$(az ad signed-in-user show --query id -o tsv)
    SUB=$(az account show --query id -o tsv)
    az role assignment create --assignee "$ME" \
        --role "Key Vault Secrets Officer" \
        --scope "/subscriptions/$SUB/resourceGroups/dagster-demo-rg/providers/Microsoft.KeyVault/vaults/$KV"
    sleep 30  # RBAC propagation
    az keyvault secret set --vault-name "$KV" --name "postgres-password" --value "<your-pg-password>"

    export KV_VAULT_URL="https://$KV.vault.azure.net"
    export PG_HOST=<your-pg-host>
    export PG_USER=<your-pg-user>
    export PG_DATABASE=<your-pg-db>
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas sqlalchemy psycopg2-binary azure-identity azure-keyvault-secrets
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 2 community components + a custom op for KV-driven URL"
$CLI add synthetic_data_generator   --auto-install
$CLI add key_vault_resource         --auto-install

# Custom op: fetch secret + materialize URL as an asset other components can read
mkdir -p "src/$PKG/defs/db_url_builder"
cat > "src/$PKG/defs/db_url_builder/__init__.py" <<EOF
import os, urllib.parse
import dagster as dg
from $PKG.components.key_vault_resource.component import KeyVaultResource

@dg.asset(group_name="secrets", kinds={"azure", "keyvault"})
def postgres_url(key_vault: KeyVaultResource) -> str:
    """Build a SQLAlchemy URL using a password fetched from Key Vault."""
    pwd = key_vault.get("postgres-password")
    pwd_enc = urllib.parse.quote(pwd, safe="")
    user = os.environ["PG_USER"]
    host = os.environ["PG_HOST"]
    db = os.environ["PG_DATABASE"]
    url = f"postgresql+psycopg2://{user}:{pwd_enc}@{host}:5432/{db}?sslmode=require"
    os.environ["DATABASE_URL"] = url    # set for the downstream sink
    return url

defs = dg.Definitions(assets=[postgres_url])
EOF

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 50
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/key_vault_resource/defs.yaml" <<EOF
type: $PKG.components.key_vault_resource.component.KeyVaultResourceComponent
attributes:
  resource_key: key_vault
  vault_url: $KV_VAULT_URL
  tenant_id_env_var: AZURE_TENANT_ID
  client_id_env_var: AZURE_CLIENT_ID
  client_secret_env_var: AZURE_CLIENT_SECRET
EOF

# Use the existing dataframe_to_table component, configured to pick up DATABASE_URL
$CLI add dataframe_to_table --auto-install

cat > "src/$PKG/defs/dataframe_to_table/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_table.component.DataframeToTableComponent
attributes:
  asset_name: orders_in_postgres
  upstream_asset_key: orders_raw
  table_name: orders
  database_url_env_var: DATABASE_URL    # set by the postgres_url asset above
  if_exists: replace
  drop_timezone: true
  deps: [postgres_url]                   # ensure URL is built first
  group_name: sink
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

The pipeline will:
  1. Materialize 50 synthetic orders
  2. Fetch the postgres password from KV
  3. Build DATABASE_URL with the secret embedded
  4. Run dataframe_to_table to land orders in Postgres

Verify:
    psql "$DATABASE_URL" -c 'SELECT COUNT(*) FROM orders'
MSG
