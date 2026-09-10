# elasticsearch_asset demo (Docker ES + synthetic upstream)
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires a container runtime for Elasticsearch. Docker required.

Proves `elasticsearch_asset` in `mode: index` end-to-end against a
single-node Docker Elasticsearch. An upstream Dagster asset produces
a pandas DataFrame; `elasticsearch_asset` bulk-indexes it as ES
documents with a chosen `id_field`. Different from
[`examples/elasticsearch.md`](elasticsearch.md), which exercises the
`elasticsearch_reader` component (ES → DataFrame, opposite direction).

```
synthetic_data_generator  (100 fake products → DataFrame)
  │
  │  upstream_asset_key
  ▼
elasticsearch_asset  (mode: index, chunk_size: 50, id_field: product_id)
  │
  ▼
Docker Elasticsearch  (http://localhost:9201, index: synthetic_products)
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `synthetic_data_generator` | ai | Produces 100 fake products (schema_type=products) as a pandas DataFrame |
| 2 | `elasticsearch_asset` | analytics | Bulk-indexes the upstream DataFrame into ES via `_bulk` API |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_elasticsearch_asset_demo.sh | bash
cd elasticsearch-asset-demo
export ELASTICSEARCH_URL=http://localhost:9201

uv run dg check defs
uv run dg launch --assets '*'
```

Expected in the run log:

```
synthetic_products - Generated DataFrame with shape (100, 10)
products_in_es - [Elasticsearch] Connected to http://localhost:9201. Mode: index.
products_in_es - [Elasticsearch] Received 100 rows from upstream asset. Indexing into 'synthetic_products' in chunks of 50 ...
products_in_es - [Elasticsearch] Indexed 100 documents into 'synthetic_products'.
products_in_es - ASSET_MATERIALIZATION - Materialized value products_in_es.
```

## Verify documents landed in ES

```bash
curl -s 'http://localhost:9201/synthetic_products/_count' ; echo
# {"count":100, ...}

curl -s 'http://localhost:9201/synthetic_products/_search?size=2&pretty' | head -30
```

Expected: 100 documents with `product_id` as `_id` and the full product
schema (`name`, `category`, `price`, `cost`, `margin_pct`,
`stock_quantity`, `rating`, `num_reviews`, `created_at`, `is_active`)
as `_source`.

## Full UI flow

```bash
ELASTICSEARCH_URL=http://localhost:9201 uv run dg dev
```

- **Assets** tab → `synthetic_products → products_in_es` chain; both materialize green on click.
- Kibana / any ES UI can browse `synthetic_products` for exploration.

## Retargeting to production

Swap `ELASTICSEARCH_URL` for a real cluster URL. If auth is required,
set `api_key_env_var: MY_ES_API_KEY` on the `elasticsearch_asset` YAML
and export `MY_ES_API_KEY` in the run environment. Same shape works
against Elastic Cloud / self-hosted / OpenSearch (via the ES-compatible
client).

## `mode: query` — the reverse direction

The same component also runs ES DSL queries and writes hits to a
database table (`mode: query` + `database_url_env_var` + `table_name`).
Not covered in this demo — see the component's [README](https://dagster-component-ui.vercel.app/c/elasticsearch_asset)
for the config surface.

## Cleanup

```bash
docker rm -f dg-es-asset-demo
```

## Client-server version compatibility

The demo pins `elasticsearch<9.0.0` for the Python client to match the
8.15.3 server image. If you point at a real ES 9.x cluster, drop the
`,<9.0.0` upper bound in your project's `pyproject.toml`.

## See also

- **[elasticsearch.md](elasticsearch.md)** — sibling demo for `elasticsearch_reader` (ES → DataFrame).
- **[elasticsearch_resource](https://dagster-component-ui.vercel.app/c/elasticsearch_resource)** — shared connection wrapper.
- Browse the [walkthrough index](README.md).
