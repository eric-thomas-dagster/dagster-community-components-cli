# Cassandra end-to-end — local docker, no auth

Read from Cassandra via the community-component family against a single-node Cassandra container.

## Components used

| Component | Source | Role |
|---|---|---|
| `cassandra_resource` | community | Shared connection — `hosts` / `port` / `keyspace` / auth env vars |
| `cassandra_reader` | community | Run a CQL query → DataFrame asset |
| `cassandra_writer` | community | DataFrame → Cassandra table (**not validated** — see Known issues) |

## Run it

```bash
bash setup_cassandra_demo.sh
cd cassandra-demo
uv run dg check defs
uv run dg launch --assets cassandra_events_read
# 10 rows read from dagster_demo.events
```

Cleanup: `docker rm -f dg-cassandra-demo`.

## YAML shape

```yaml
type: dagster_component_templates.CassandraReaderComponent
attributes:
  asset_name: cassandra_events_read
  hosts: [localhost]               # or list of nodes for a real cluster
  port: 9042
  keyspace: dagster_demo
  query: 'SELECT event_id, user, event_type, amount FROM events'
  # username_env_var: CASSANDRA_USERNAME    # optional
  # password_env_var: CASSANDRA_PASSWORD    # optional
```

## Known issues

- **`cassandra_writer` placeholder bug.** Component currently emits CQL with `%s` placeholders (psycopg-style) instead of Cassandra's `?` placeholders. Crashes with `SyntaxException: ... no viable alternative at input '%'`. Not promoted to live until fixed.

## Production retargeting

```yaml
# DataStax Astra
hosts: ['<region>.aws.astra.datastax.com']
port: 29042
username_env_var: ASTRA_CLIENT_ID
password_env_var: ASTRA_CLIENT_SECRET
# Also requires secure_connect_bundle file — component currently doesn't
# expose that field. For Astra production, extend the component.
```

## See also

- [`mongodb.md`](mongodb.md), [`neo4j.md`](neo4j.md), [`elasticsearch.md`](elasticsearch.md) — sibling NoSQL families
