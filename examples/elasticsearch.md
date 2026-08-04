# Elasticsearch end-to-end — local docker, no auth
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Read documents from an Elasticsearch index via the community-component family. Same components target Elastic Cloud / self-hosted clusters unchanged — only `hosts` / `api_key_env_var` change.

## Components used

| Component | Source | Role |
|---|---|---|
| `elasticsearch_resource` | community | Shared connection config |
| `elasticsearch_reader` | community | Run a search query → DataFrame asset (one row per hit) |

## Run

```bash
bash setup_elasticsearch_demo.sh
cd elasticsearch-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets product_index_dump
# Retrieved 5 documents (total matches: 5)
```

Cleanup: `docker rm -f dg-es-demo`.

## YAML shape

```yaml
type: dagster_component_templates.ElasticsearchReaderComponent
attributes:
  asset_name: product_index_dump
  index_name: products
  host_env_var: ELASTICSEARCH_URL          # http://localhost:9200 or https://....es.io
  api_key_env_var: ELASTICSEARCH_API_KEY   # optional
  # query: { ... full query DSL ... }      # optional
  # search_text: "wireless"                # convenience: multi_match
  # search_fields: [title, description]    # which fields to search
  n_results: 50
```

## Demo notes

- **Single-node mode + security disabled** for the demo (`xpack.security.enabled=false`) — production must set up auth.
- **Client version pinning:** Pin `elasticsearch>=8.0.0,<9.0.0` to match Elasticsearch 8.x servers. The default `pip install elasticsearch` pulls v9, which sends an `Accept: compatible-with=9` header that 8.x servers reject.

## Known component fix this session

`elasticsearch_reader` previously called `es.search(..., source=source_fields)` where `source_fields=None` when no projection was supplied — newer `elasticsearch-py` serializes that into `_source: null` and ES 8.x rejects with `parsing_exception`. Now only passes `source=` if it's non-empty.

## Production retargeting

```yaml
# Elastic Cloud
export ELASTICSEARCH_URL='https://abc123.es.us-east-1.aws.elastic.cloud:9243'
export ELASTICSEARCH_API_KEY='<base64-api-key>'

elasticsearch_resource:
  attributes:
    hosts: https://abc123.es.us-east-1.aws.elastic.cloud:9243
    api_key_env_var: ELASTICSEARCH_API_KEY
    verify_certs: true
```

## See also

- [`mongodb.md`](mongodb.md), [`neo4j.md`](neo4j.md) — sibling NoSQL families
