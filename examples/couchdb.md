# CouchDB — reader + writer + resource round-trip

Docker-local end-to-end for the CouchDB document-DB component set. Reader uses a Mango-style selector to pull active docs; a Python transform bumps them into a premium tier; the writer upserts them into a target database. All three shipped CouchDB components validated in one demo.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_couchdb_demo.sh \
  -o setup_couchdb_demo.sh
bash setup_couchdb_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + Docker. Cost: $0.

## What gets validated

| Component | Role |
|---|---|
| `couchdb_resource` | Shared connection handle (url + auth) |
| `couchdb_reader` | Mango selector → DataFrame source |
| `couchdb_writer` | DataFrame → upsert docs into a database |

## The chain

```
CouchDB container (couchdb:3)
   ├─ database: orders           ← 5 seed docs (4 active, 1 cancelled)
   └─ database: orders_processed ← empty (target for upsert)

┌──────────────────────────┐
│ active_orders            │
│ (couchdb_reader          │
│  selector: {status:      │
│  active}, fields: [...]) │
│ → DataFrame of 4 rows    │
└──────────────┬───────────┘
               │
               ▼
┌──────────────────────────┐
│ active_orders_upserted   │
│ (couchdb_writer upserts  │
│  into orders_processed,  │
│  keyed by order_id)      │
└──────────────────────────┘
```

## Docker-image note

`couchdb:3` (official image). ~350 MB. First-boot is <10 seconds — the setup script polls the readiness endpoint before proceeding.

## Teardown

```bash
docker stop dagster_couchdb_demo && docker rm dagster_couchdb_demo
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_couchdb_demo.sh \
  -o setup_couchdb_demo.sh
bash setup_couchdb_demo.sh
```

## See also

- [mongodb](mongodb.md), [neo4j](neo4j.md), [cassandra](cassandra.md) — same round-trip pattern for other NoSQL stores
