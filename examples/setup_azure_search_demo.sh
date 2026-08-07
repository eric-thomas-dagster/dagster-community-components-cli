#!/usr/bin/env bash
# Azure AI Search round-trip demo.
#
# WHAT THIS DEMONSTRATES
#   30 synthetic products → DataFrame → Azure AI Search index → query (text +
#   filter) → CSV report. Foundation for RAG, semantic search, enterprise
#   knowledge bases.
#
# Pipeline (4 components):
#   synthetic_data_generator → azure_search_indexer → azure_search_query → dataframe_to_csv
#
# PREREQS
#   1. Azure subscription, az CLI signed in.
#   2. Microsoft.Search provider registered.
#   3. Search service + index — see "Provisioning".
#
# REQUIRED ENV VARS
#   AZURE_SEARCH_ENDPOINT   https://<service>.search.windows.net
#   AZURE_SEARCH_ADMIN_KEY  primary admin key
#   AZURE_SEARCH_INDEX_NAME index to use (must exist)
#
# COST while running
#   Free tier: $0/mo for 50 MB / 3 indexes. This demo fits comfortably.
#
# TEARDOWN
#   az search service delete -g dagster-demo-rg -n <service> --yes

set -euo pipefail
PROJECT_DIR="${1:-azure-search-demo}"
INDEX_NAME="${AZURE_SEARCH_INDEX_NAME:-products-demo}"

missing=()
[ -z "${AZURE_SEARCH_ENDPOINT:-}" ]  && missing+=("AZURE_SEARCH_ENDPOINT")
[ -z "${AZURE_SEARCH_ADMIN_KEY:-}" ] && missing+=("AZURE_SEARCH_ADMIN_KEY")

if [ ${#missing[@]} -gt 0 ]; then
  cat <<'NEED'
ERROR: missing env vars.

To provision a Free-tier Azure AI Search service + index:

    RG=dagster-demo-rg
    AS=dgsearch$(openssl rand -hex 4)

    az group create -n "$RG" -l eastus 2>/dev/null || true
    az provider register --namespace Microsoft.Search --wait
    az search service create -g "$RG" -n "$AS" -l eastus --sku free

    export AZURE_SEARCH_ENDPOINT="https://$AS.search.windows.net"
    export AZURE_SEARCH_ADMIN_KEY=$(az search admin-key show -g "$RG" --service-name "$AS" --query primaryKey -o tsv)

    # Create an index for the demo
    python3 -c "
    import os
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import SearchIndex, SimpleField, SearchableField, SearchFieldDataType
    from azure.core.credentials import AzureKeyCredential
    cred = AzureKeyCredential(os.environ['AZURE_SEARCH_ADMIN_KEY'])
    ic = SearchIndexClient(endpoint=os.environ['AZURE_SEARCH_ENDPOINT'], credential=cred)
    fields = [
        SimpleField(name='id', type=SearchFieldDataType.String, key=True),
        SearchableField(name='order_id', type=SearchFieldDataType.String),
        SearchableField(name='customer_id', type=SearchFieldDataType.String, filterable=True),
        SearchableField(name='category', type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name='total', type=SearchFieldDataType.Double, filterable=True, sortable=True),
        SearchableField(name='status', type=SearchFieldDataType.String, filterable=True),
    ]
    ic.create_index(SearchIndex(name='products-demo', fields=fields))
    print('Created index: products-demo')
    "
NEED
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
mkdir -p out
PKG="$(ls src/ | head -1)"

uv add -q pandas azure-search-documents azure-identity
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 4 community components"
$CLI add synthetic_data_generator   --auto-install
$CLI add azure_search_indexer       --auto-install
$CLI add azure_search_query         --auto-install
$CLI add dataframe_to_csv           --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: orders_raw
  schema_type: orders
  row_count: 30
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/azure_search_indexer/defs.yaml" <<EOF
type: $PKG.components.azure_search_indexer.component.AzureSearchIndexerComponent
attributes:
  asset_name: orders_in_search
  upstream_asset_key: orders_raw
  endpoint: $AZURE_SEARCH_ENDPOINT
  index_name: $INDEX_NAME
  api_key_env_var: AZURE_SEARCH_ADMIN_KEY
  action: mergeOrUpload
  batch_size: 1000
  group_name: search
EOF

cat > "src/$PKG/defs/azure_search_query/defs.yaml" <<EOF
type: $PKG.components.azure_search_query.component.AzureSearchQueryComponent
attributes:
  asset_name: high_value_search_results
  endpoint: $AZURE_SEARCH_ENDPOINT
  index_name: $INDEX_NAME
  api_key_env_var: AZURE_SEARCH_ADMIN_KEY
  search_text: "*"
  filter_query: "total gt 500"
  top: 100
  order_by: ["total desc"]
  deps: [orders_in_search]
  group_name: query
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: search_results_report
  upstream_asset_key: high_value_search_results
  file_path: out/azure_search_results.csv
  group_name: report
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Verify:
    head $PROJECT_ABS/out/azure_search_results.csv

Teardown:
    az search service delete -g dagster-demo-rg -n <service> --yes
MSG
