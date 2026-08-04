# MongoDB end-to-end — local docker, no auth
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Read, write, and resource the MongoDB community-component family against a single-container MongoDB running locally. The same components work unchanged against MongoDB Atlas / EC2-hosted / on-prem replica sets — just swap `connection_string_env_var` + `tls`.

## Components used

| Component | Source | Role |
|---|---|---|
| `mongodb_resource` | community | Shared connection — centralizes URI + database + TLS for downstream components |
| `mongodb_io_manager` | community | Project default IO manager — every DataFrame-returning asset auto-serialized as a MongoDB collection |
| `mongodb_reader` | community | Read a collection → DataFrame asset (with query + projection + sort + limit) |
| `mongodb_writer` | community | Write a DataFrame to a named collection (`append` / `replace` / upsert by key) |
| `mongodb_ingestion` | community | dlt-based multi-collection extract → in-memory DuckDB → DataFrame |
| `synthetic_data_generator` | community | Upstream of the writer — generates synthetic orders |

## Architecture

```
                          ┌────────────────────┐
                          │ synthetic_orders   │
                          │ (generator)        │
                          └─────────┬──────────┘
                                    │ DataFrame (20 rows)
                                    ▼
                          ┌────────────────────┐
                          │ orders_in_mongo    │
                          │ (mongodb_writer)   │
                          └─────────┬──────────┘
                                    │ INSERT
                                    ▼
   ┌─────────────────────────────────────────────┐
   │ MongoDB (local Docker, port 27017)          │
   │   database: dagster_demo                    │
   │   pre-seeded: users (5 docs), products (3)  │
   │   written by demo: orders (20 docs)         │
   └─────────┬───────────────────────────────────┘
             │ find({active: true}) sorted by created_at desc
             ▼
   ┌──────────────────┐
   │ users_from_mongo │
   │ (mongodb_reader) │
   └──────────────────┘
```

Three flows share the same database:
- **Explicit write:** `synthetic_orders → orders_in_mongo` (writes the synthetic DataFrame into the `orders` collection via mongodb_writer)
- **Explicit read:** `MongoDB.users → users_from_mongo` (queries the pre-seeded `users` collection back as a DataFrame via mongodb_reader)
- **Multi-collection ingest:** `mongodb_ingestion` extracts both `users` and `products` via dlt → in-memory DuckDB → unified DataFrame (`all_collections_extract`, with `_resource_type` column to distinguish source collections)
- **Auto-serialize via IO manager:** every DataFrame asset (`synthetic_orders`, `users_from_mongo`, `all_collections_extract`) is also auto-written to its own collection by `mongodb_io_manager` (set as the project default `io_manager`). Sinks that return `Output(value=None)` (like `mongodb_writer`) are no-ops through this path.

## Prerequisites

- **Docker daemon running.** The setup script exits early with a clear message if it's not.

## Run

```bash
bash setup_mongodb_demo.sh
cd mongodb-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'
```

Expect `RUN_SUCCESS`. Then verify what landed in MongoDB:

```bash
docker exec dg-mongodb-demo mongosh dagster_demo --quiet --eval '
  print("users:", db.users.countDocuments());
  print("orders:", db.orders.countDocuments());
  printjson(db.orders.findOne());
'
# users: 5
# orders: 20
# { _id: ..., order_id: 'ORD00000001', customer_id: '...', total: 168.96, status: 'shipped' }
```

## YAML shape

### Resource — centralized connection

```yaml
type: dagster_component_templates.MongoDBResourceComponent
attributes:
  resource_key: mongodb_resource
  connection_string_env_var: MONGODB_URI    # mongodb://localhost:27017
  database: dagster_demo
  tls: false                                 # set true for Atlas srv URIs
```

### Reader — collection → DataFrame

```yaml
type: dagster_component_templates.MongodbReaderComponent
attributes:
  asset_name: users_from_mongo
  connection_string_env_var: MONGODB_URI
  database: dagster_demo
  collection: users
  query: { active: true }                     # MongoDB query filter
  projection: { _id: 0, name: 1, email: 1 }   # optional field selection
  limit: 100
  sort_field: created_at
  sort_direction: -1                          # 1 asc / -1 desc
```

### Writer — DataFrame → collection

```yaml
type: dagster_component_templates.MongodbWriterComponent
attributes:
  asset_name: orders_in_mongo
  upstream_asset_key: synthetic_orders
  connection_string_env_var: MONGODB_URI
  database: dagster_demo
  collection: orders
  if_exists: replace        # 'append' | 'replace' | 'upsert'
  # upsert_key: order_id    # required when if_exists: upsert
```

## Trade-offs & gotchas

- **Single env var, multiple components.** All downstream components read `MONGODB_URI` from the same env var. To target a different cluster per component, set a different `connection_string_env_var:`.
- **No data passed via mongodb_resource.** The resource carries config only — readers and writers each construct their own pymongo client (with the same URI). Skipping the resource and pointing readers/writers at `MONGODB_URI` directly is also fine; the resource is right when many downstream components share a connection.
- **IO manager coexists with explicit writer.** `mongodb_io_manager` is the project default, so every DataFrame asset gets auto-serialized to a collection named after the asset key. `mongodb_writer` runs alongside it for the cases when you want a custom collection name + `if_exists`/upsert semantics. The IO manager skips `Output(value=None)` outputs so the writer's no-op return doesn't trip it.
- **dlt destination on `mongodb_ingestion`.** Default is in-memory DuckDB → DataFrame. To persist to a real warehouse instead, set `destination: snowflake | bigquery | postgres | redshift | databricks | ...` and add the matching `dlt[<dest>]` extra to requirements.

## Production retargeting

Atlas:

```yaml
# .env (not committed)
export MONGODB_URI='mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority'
```

```yaml
mongodb_resource:
  attributes:
    connection_string_env_var: MONGODB_URI
    database: production
    tls: true
```

Self-hosted replica set:

```yaml
export MONGODB_URI='mongodb://host-a:27017,host-b:27017,host-c:27017/?replicaSet=rs0&readPreference=secondaryPreferred'
```

Component graph stays the same — `mongodb_resource` is the only thing that changes.

## See also

- [`composition_primitives.md`](composition_primitives.md) — `warehouse_maintenance_job` / `sql_command_job` for the SQL-side equivalent
- [`local_transforms.md`](local_transforms.md) — pure-pandas filter/summarize/sink chain
- [`kafka.md`](kafka.md) — another Docker-backed local demo
