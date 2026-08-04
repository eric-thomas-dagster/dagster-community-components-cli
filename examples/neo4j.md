# Neo4j end-to-end — local docker, no SaaS
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Read / write the Neo4j graph database via the community-component family. Same components target AuraDB / self-hosted clusters unchanged — only `uri` + auth env vars change.

## Components used

| Component | Source | Role |
|---|---|---|
| `neo4j_resource` | community | Shared connection — `uri` / `username` / `password_env_var` / `database` |
| `neo4j_reader` | community | Run a Cypher query → DataFrame asset |
| `neo4j_writer` | community | DataFrame → labeled nodes (with optional MERGE-by-key upsert) |
| `synthetic_data_generator` | community | Upstream of the writer — generates 10 customer rows |

## Architecture

```
   ┌──────────────────────────┐
   │ Neo4j (local Docker)     │
   │   bolt://localhost:7687  │
   │   pre-seeded:            │
   │     5 :Person nodes      │
   │     8 :KNOWS edges       │
   └─────────┬──────────┬─────┘
             │ Cypher   │ MERGE on customer_id
             ▼          ▲
   ┌──────────────────┐ │ ┌────────────────────┐
   │ knows_graph      │ │ │ write_new_people   │
   │ (reader)         │ │ │ (writer → :Customer)
   └──────────────────┘ │ └─────────┬──────────┘
                        │           │
                        │   ┌───────┴─────────┐
                        │   │ new_people      │
                        │   │ (synthetic gen) │
                        │   └─────────────────┘
```

## Run

```bash
bash setup_neo4j_demo.sh
cd neo4j-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'

# Verify both label populations:
docker exec dg-neo4j-demo cypher-shell -u neo4j -p dagsterdemo \
  'MATCH (n) RETURN labels(n) AS label, COUNT(*) AS cnt ORDER BY label;'
# ["Customer"], 10   ← from neo4j_writer
# ["Person"], 5      ← seeded
```

Cleanup: `docker rm -f dg-neo4j-demo`.

## YAML shape

```yaml
type: dagster_component_templates.Neo4jReaderComponent
attributes:
  asset_name: knows_graph
  uri_env_var: NEO4J_URI                # bolt:// or neo4j+s://
  username_env_var: NEO4J_USERNAME
  password_env_var: NEO4J_PASSWORD
  query: 'MATCH (n:Person)-[:KNOWS]->(m:Person) RETURN n.name AS person, m.name AS knows'
  database: neo4j
```

```yaml
type: dagster_component_templates.Neo4jWriterComponent
attributes:
  asset_name: write_new_people
  upstream_asset_key: new_people         # any DataFrame upstream
  uri_env_var: NEO4J_URI
  username_env_var: NEO4J_USERNAME
  password_env_var: NEO4J_PASSWORD
  node_label: Customer
  id_column: customer_id                  # column used as merge key
  merge: true                             # MERGE not CREATE — idempotent
  database: neo4j
```

## Production retargeting

```yaml
# AuraDB
export NEO4J_URI='neo4j+s://abcd1234.databases.neo4j.io'
export NEO4J_USERNAME='neo4j'
export NEO4J_PASSWORD='...'
```

## See also

- [`mongodb.md`](mongodb.md), [`redis.md`](redis.md) — sibling database families
- [`local_transforms.md`](local_transforms.md) — pure-pandas chain
