#!/usr/bin/env bash
# MongoDB demo — read, write, and dlt-ingest against a local MongoDB
# instance running in Docker. No SaaS, no auth, no managed cluster.
#
# WHAT THIS DEMONSTRATES
#   The full MongoDB community-component family in one project:
#     - mongodb_resource      (shared connection)
#     - mongodb_io_manager    (project IO manager — every DataFrame asset
#                              auto-serialized to a MongoDB collection)
#     - mongodb_reader        (read a collection → DataFrame asset)
#     - mongodb_writer        (DataFrame → collection sink — named explicitly)
#     - mongodb_ingestion     (dlt-based multi-collection extract → DataFrame)
#     - synthetic_data_generator (upstream, already validated — supplies orders)
#
# Asset graph:
#   users_from_mongo                ← mongodb_reader (read seeded `users` collection)
#   synthetic_orders                ← synthetic_data_generator
#         │
#         ▼
#   orders_in_mongo                 ← mongodb_writer (writes orders to MongoDB)
#
# REQUIRES: Docker daemon running.
# COST: \$0 — single mongo:7 container, ~200 MB image, runs locally.

set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running."
  echo "Start Docker Desktop (or 'colima start') and re-run."
  exit 1
fi

PROJECT_DIR="${1:-mongodb-demo}"
MONGO_NAME=dg-mongodb-demo
MONGO_PORT=27017
MONGO_DB=dagster_demo
MONGO_URI="mongodb://localhost:${MONGO_PORT}"

echo ">>> 1/5  Starting MongoDB in Docker ($MONGO_NAME:$MONGO_PORT)"
docker rm -f "$MONGO_NAME" >/dev/null 2>&1 || true
docker run -d --name "$MONGO_NAME" -p $MONGO_PORT:27017 mongo:7 >/dev/null

echo "    Waiting for MongoDB to become ready..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if docker exec "$MONGO_NAME" mongosh --quiet --eval 'db.adminCommand({ping:1}).ok' 2>/dev/null | grep -q 1; then
    echo "    MongoDB up."
    break
  fi
  sleep 2
done

echo ">>> 2/5  Seeding $MONGO_DB.users (5 docs) and $MONGO_DB.products (3 docs)"
docker exec -i "$MONGO_NAME" mongosh "$MONGO_DB" --quiet >/dev/null <<'MONGOSH'
db.users.insertMany([
  { user_id: 1, name: 'Alice',   email: 'alice@example.com',   active: true,  created_at: new Date('2026-01-15') },
  { user_id: 2, name: 'Bob',     email: 'bob@example.com',     active: true,  created_at: new Date('2026-02-03') },
  { user_id: 3, name: 'Carol',   email: 'carol@example.com',   active: false, created_at: new Date('2026-02-20') },
  { user_id: 4, name: 'Dan',     email: 'dan@example.com',     active: true,  created_at: new Date('2026-03-10') },
  { user_id: 5, name: 'Eve',     email: 'eve@example.com',     active: true,  created_at: new Date('2026-04-01') }
]);
db.products.insertMany([
  { sku: 'P-001', name: 'Widget',  category: 'tools',     price: 9.99  },
  { sku: 'P-002', name: 'Gizmo',   category: 'gadgets',   price: 14.50 },
  { sku: 'P-003', name: 'Sprocket',category: 'tools',     price: 3.25  }
]);
print('Seeded users:', db.users.countDocuments());
print('Seeded products:', db.products.countDocuments());
MONGOSH

echo ">>> 3/5  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q 'pymongo>=4.0.0' pandas 'dlt>=0.4.0' 'dlt[duckdb]' duckdb

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 4/5  Installing 6 components"
for c in synthetic_data_generator mongodb_resource mongodb_io_manager \
         mongodb_reader mongodb_writer mongodb_ingestion; do
  $CLI add $c --auto-install
done

echo ">>> 5/5  Writing defs.yaml for the MongoDB pipeline"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Shared resource — connection details centralized
write_yaml "mongodb_resource" "type: $PKG.components.mongodb_resource.component.MongoDBResourceComponent
attributes:
  resource_key: mongodb_resource
  connection_string_env_var: MONGODB_URI
  database: $MONGO_DB
  tls: false"

# Project IO manager — every DataFrame asset's output gets auto-serialized
# to a MongoDB collection (collection name = asset key). Sinks that return
# Output(value=None) (like mongodb_writer) are no-ops through this IO manager.
write_yaml "mongodb_io_manager" "type: $PKG.components.mongodb_io_manager.component.MongoDBIOManagerComponent
attributes:
  resource_key: io_manager
  connection_uri_env_var: MONGODB_URI
  database: $MONGO_DB
  if_exists: replace"

# Read seeded users collection
write_yaml "mongodb_reader" "type: $PKG.components.mongodb_reader.component.MongodbReaderComponent
attributes:
  asset_name: users_from_mongo
  connection_string_env_var: MONGODB_URI
  database: $MONGO_DB
  collection: users
  query:
    active: true
  limit: 100
  sort_field: created_at
  sort_direction: -1
  group_name: mongodb_demo"

# Synthetic upstream for the writer
write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 20
  random_state: 42
  group_name: mongodb_demo"

# Write synthetic_orders to MongoDB
write_yaml "mongodb_writer" "type: $PKG.components.mongodb_writer.component.MongodbWriterComponent
attributes:
  asset_name: orders_in_mongo
  upstream_asset_key: synthetic_orders
  connection_string_env_var: MONGODB_URI
  database: $MONGO_DB
  collection: orders
  if_exists: replace
  group_name: mongodb_demo"

# dlt-based multi-collection extract → in-memory DuckDB → DataFrame
write_yaml "mongodb_ingestion" "type: $PKG.components.mongodb_ingestion.component.MongoDBIngestionComponent
attributes:
  asset_name: all_collections_extract
  connection_url: $MONGO_URI
  database: $MONGO_DB
  collection_names: [users, products]
  group_name: mongodb_demo
  deps: [orders_in_mongo]"

echo "export MONGODB_URI='$MONGO_URI'" > .env.demo

cat <<MSG

>>> Setup complete.

Source the env var + validate everything loaded:
    cd $PROJECT_DIR
    source .env.demo
    uv run dg check defs
    uv run dg list defs   # 4 assets, 1 resource

Materialize the pipeline:
    uv run dg launch --assets '*'

After the run, verify what's in MongoDB:
    docker exec -it $MONGO_NAME mongosh $MONGO_DB --eval 'db.users.countDocuments()'    # 5
    docker exec -it $MONGO_NAME mongosh $MONGO_DB --eval 'db.orders.countDocuments()'   # 20 (written by mongodb_writer)

Browse the asset graph:
    uv run dg dev   # http://localhost:3000 → Assets

Stop + clean up:
    docker rm -f $MONGO_NAME

To retarget at MongoDB Atlas:
  - mongodb_resource: tls: true + connection_string_env_var with srv URI
  - all components reference the same env var; nothing else changes
MSG
