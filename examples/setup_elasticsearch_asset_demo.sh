#!/usr/bin/env bash
# elasticsearch_asset demo — Docker Elasticsearch + synthetic upstream.
#
# Different from the existing elasticsearch.md demo (which uses
# elasticsearch_reader for RESULTS→DataFrame). This one exercises
# elasticsearch_asset in `mode: index` — an upstream Dagster asset
# produces a DataFrame, elasticsearch_asset bulk-indexes it as ES
# documents.
#
# Shape (2 components):
#   synthetic_data_generator  →  seed 100 fake products (DataFrame)
#     └── elasticsearch_asset (mode: index, target index: products)
#
# Single-node Elasticsearch via Docker (security off, discovery
# single-node — ~512MB heap). No auth, no SaaS.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-elasticsearch-asset-demo}"
ES_NAME=dg-es-asset-demo
ES_PORT=9201
INDEX=synthetic_products

echo ">>> 1/5  Starting Elasticsearch on :$ES_PORT"
docker rm -f "$ES_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$ES_NAME" \
  -p $ES_PORT:9200 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=false" \
  -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3 >/dev/null

echo "    Waiting for Elasticsearch to become ready (30-60s on first run)..."
for i in $(seq 1 40); do
  if curl -fs http://localhost:$ES_PORT >/dev/null 2>&1; then
    echo "    Elasticsearch up."
    break
  fi
  sleep 3
done

echo ">>> 2/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> 3/5  Adding deps"
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'elasticsearch>=8.0.0,<9.0.0' pandas faker   # pin to major 8 to match server image

CLI="uvx --refresh --from dagster-community-components-cli dagster-component --refresh"

echo ">>> 4/5  Installing 2 components"
$CLI add synthetic_data_generator --auto-install
$CLI add elasticsearch_asset      --auto-install

echo ">>> 5/5  Writing defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_products
  schema_type: products
  row_count: 100
  random_state: 42
  description: 100 fake products for the elasticsearch_asset demo
  group_name: seeds
EOF

cat > "src/$PKG/defs/elasticsearch_asset/defs.yaml" <<EOF
type: $PKG.components.elasticsearch_asset.component.ElasticsearchAssetComponent
attributes:
  asset_name: products_in_es
  upstream_asset_key: synthetic_products
  elasticsearch_url_env_var: ELASTICSEARCH_URL
  index_name: $INDEX
  mode: index
  id_field: product_id
  chunk_size: 50
  group_name: search
  description: Bulk-index the synthetic_products DataFrame into ES/$INDEX
EOF

cat <<MSG

>>> Setup complete.

Export the connection string:

    export ELASTICSEARCH_URL='http://localhost:$ES_PORT'

Validate + run:

    cd $PROJECT_DIR
    export ELASTICSEARCH_URL='http://localhost:$ES_PORT'
    uv run dg check defs
    uv run dg launch --assets '*'

Verify documents landed in ES:

    curl -s "http://localhost:$ES_PORT/$INDEX/_count" ; echo
    curl -s "http://localhost:$ES_PORT/$INDEX/_search?size=3&pretty" | head -60

Cleanup:

    docker rm -f $ES_NAME
MSG
