#!/usr/bin/env bash
# Elasticsearch demo — read seeded documents via elasticsearch_reader.
# No SaaS, single-node Elasticsearch via Docker.

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-elasticsearch-demo}"
ES_NAME=dg-es-demo
ES_PORT=9200
INDEX=products

echo ">>> 1/5  Starting Elasticsearch (single-node, security off) on :$ES_PORT"
docker rm -f "$ES_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$ES_NAME" \
  -p $ES_PORT:9200 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=false" \
  -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3 >/dev/null

echo "    Waiting for Elasticsearch to become ready (30-60s on first run)..."
for i in $(seq 1 30); do
  if curl -fs http://localhost:$ES_PORT >/dev/null 2>&1; then
    echo "    Elasticsearch up."
    break
  fi
  sleep 3
done

echo ">>> 2/5  Seeding index '$INDEX' with 5 documents"
for i in 1 2 3 4 5; do
  curl -fs -X POST http://localhost:$ES_PORT/$INDEX/_doc/$i -H 'Content-Type: application/json' \
    -d "{\"product_id\":$i,\"name\":\"Widget-$i\",\"category\":\"tools\",\"price\":$((10 + i * 5))}" >/dev/null
done
curl -fs -X POST http://localhost:$ES_PORT/$INDEX/_refresh >/dev/null
echo "    Indexed 5 products."

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'elasticsearch>=8.0.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 2 components"
for c in elasticsearch_resource elasticsearch_reader; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "elasticsearch_resource" "type: $PKG.components.elasticsearch_resource.component.ElasticsearchResourceComponent
attributes:
  resource_key: elasticsearch_resource
  hosts: http://localhost:$ES_PORT
  verify_certs: false"

write_yaml "elasticsearch_reader" "type: $PKG.components.elasticsearch_reader.component.ElasticsearchReaderComponent
attributes:
  asset_name: product_index_dump
  index_name: $INDEX
  host: http://localhost:$ES_PORT
  n_results: 50
  group_name: elasticsearch_demo"

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg launch --assets product_index_dump

Stop + clean up:
    docker rm -f $ES_NAME
MSG
