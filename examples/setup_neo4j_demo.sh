#!/usr/bin/env bash
# Neo4j demo — exercise the Neo4j family against a local neo4j:5-community
# container. No SaaS, no auth (other than the default password).

set -euo pipefail
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker daemon not running."; exit 1; fi

PROJECT_DIR="${1:-neo4j-demo}"
NEO4J_NAME=dg-neo4j-demo
NEO4J_BOLT_PORT=7687
NEO4J_HTTP_PORT=7474
NEO4J_PASS=dagsterdemo

echo ">>> 1/5  Starting Neo4j in Docker (bolt:$NEO4J_BOLT_PORT, http:$NEO4J_HTTP_PORT)"
docker rm -f "$NEO4J_NAME" >/dev/null 2>&1 || true
sleep 1
docker run -d --name "$NEO4J_NAME" \
  -p $NEO4J_BOLT_PORT:7687 -p $NEO4J_HTTP_PORT:7474 \
  -e NEO4J_AUTH=neo4j/$NEO4J_PASS \
  neo4j:5-community >/dev/null

echo "    Waiting for Neo4j to become ready..."
for i in $(seq 1 30); do
  if docker exec "$NEO4J_NAME" cypher-shell -u neo4j -p $NEO4J_PASS 'RETURN 1' >/dev/null 2>&1; then
    echo "    Neo4j up."
    break
  fi
  sleep 2
done

echo ">>> 2/5  Seeding 5 Person nodes + 8 KNOWS edges"
docker exec "$NEO4J_NAME" cypher-shell -u neo4j -p $NEO4J_PASS "
  CREATE (a:Person {name:'Alice', dept:'engineering'}),
         (b:Person {name:'Bob',   dept:'sales'}),
         (c:Person {name:'Carol', dept:'engineering'}),
         (d:Person {name:'Dan',   dept:'marketing'}),
         (e:Person {name:'Eve',   dept:'engineering'}),
         (a)-[:KNOWS]->(b), (a)-[:KNOWS]->(c),
         (b)-[:KNOWS]->(d), (c)-[:KNOWS]->(a),
         (c)-[:KNOWS]->(e), (d)-[:KNOWS]->(b),
         (e)-[:KNOWS]->(a), (e)-[:KNOWS]->(c);
" >/dev/null
echo "    Seeded graph."

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'neo4j>=5.0.0' pandas

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 4 components"
for c in synthetic_data_generator neo4j_resource neo4j_reader neo4j_writer; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

write_yaml "neo4j_resource" "type: $PKG.components.neo4j_resource.component.Neo4jResourceComponent
attributes:
  resource_key: neo4j_resource
  uri: bolt://localhost:$NEO4J_BOLT_PORT
  username: neo4j
  password_env_var: NEO4J_PASSWORD
  database: neo4j"

write_yaml "neo4j_reader" "type: $PKG.components.neo4j_reader.component.Neo4jReaderComponent
attributes:
  asset_name: knows_graph
  uri_env_var: NEO4J_URI
  username_env_var: NEO4J_USERNAME
  password_env_var: NEO4J_PASSWORD
  query: 'MATCH (n:Person)-[:KNOWS]->(m:Person) RETURN n.name AS person, m.name AS knows ORDER BY person, knows'
  database: neo4j
  group_name: neo4j_demo"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: new_people
  schema_type: customers
  row_count: 10
  random_state: 7
  group_name: neo4j_demo"

write_yaml "neo4j_writer" "type: $PKG.components.neo4j_writer.component.Neo4jWriterComponent
attributes:
  asset_name: write_new_people
  upstream_asset_key: new_people
  uri_env_var: NEO4J_URI
  username_env_var: NEO4J_USERNAME
  password_env_var: NEO4J_PASSWORD
  node_label: Customer
  id_column: customer_id
  merge: true
  database: neo4j
  group_name: neo4j_demo"

cat > .env.demo <<EOF
export NEO4J_URI='bolt://localhost:$NEO4J_BOLT_PORT'
export NEO4J_USERNAME='neo4j'
export NEO4J_PASSWORD='$NEO4J_PASS'
EOF

cat <<MSG

>>> Setup complete.
    cd $PROJECT_DIR && source .env.demo
    uv run dg check defs
    uv run dg launch --assets '*'

After the run:
    docker exec $NEO4J_NAME cypher-shell -u neo4j -p $NEO4J_PASS 'MATCH (n) RETURN labels(n) AS label, COUNT(*) ORDER BY label;'

Stop + clean up:
    docker rm -f $NEO4J_NAME
MSG
