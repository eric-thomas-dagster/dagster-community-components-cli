# Cube Semantic Layer — Two Demos, One Docker Container

**Components:**
- `CubeQueryAssetComponent` (`assets/analytics/cube_query_asset`)
- `ExternalCubeMetricAsset` (`external_assets/external_cube_metric`)

**Scripts:**
- [`setup_cube_query_demo.sh`](./setup_cube_query_demo.sh) — simple: run 2 Cube queries as Dagster assets
- [`setup_cube_llm_demo.sh`](./setup_cube_llm_demo.sh) — advanced: Cube query → LangChain LLM narrative summary

**Cost:** **$0** for the simple demo; ~$0.01 for the LLM version.
**Validated:** 2026-07-06 — both scripts run end-to-end against a real local Cube dev server.

## What Cube is (30-second version)

Cube is an **open-source semantic layer** ([cube.dev](https://cube.dev/)). You define metrics once in Cube's YAML/JS model — `Orders.count`, `Orders.totalAmount`, `Orders.status` — and every downstream consumer (BI, LLM, ML, dashboards, reverse-ETL) queries the same governed definitions instead of writing raw SQL.

## Why Cube + Dagster is a powerful pair (and Cube + LLM is even better)

- **Cube alone**: governed metrics, but no orchestration. When do metrics get computed? What depends on them? Who watches them?
- **Cube + Dagster**: Cube metrics become Dagster assets. Schedule them, put them in lineage, gate downstream data assets on their freshness, materialize them into the warehouse.
- **Cube + Dagster + LLM**: LLMs are unreliable at raw SQL — they hallucinate columns, misuse joins, write unsafe queries. Give them Cube's semantic layer as the interface, and their job shrinks from "generate valid SQL" to "map a question to a small structured schema." Dramatically more reliable, and safer (Cube enforces governance).

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `docker` — Docker Desktop or engine
- For the LLM demo: `OPENAI_API_KEY`

## The simple demo (no auth, no LLM)

```bash
./setup_cube_query_demo.sh
```

What it does:
1. Spins up `cubejs/cube:latest` on `localhost:4000` via Docker with a sample `Orders` cube (12 rows of e-commerce orders).
2. Scaffolds a Dagster project + installs `dagster-community-components` + `requests` + `pandas`.
3. Writes three defs:
   - `cube_orders_summary` — `CubeQueryAssetComponent` querying total count + revenue + avg amount
   - `cube_orders_by_status` — same but grouped by `Orders.status`, sorted by revenue desc
   - `cube/orders/count` — `ExternalCubeMetricAsset`, declares the measure in the catalog with a clickable Playground link
4. Materializes both query assets against the live Cube server.

Validated run output:
```
[cube] returned 1 rows, columns=['count', 'totalAmount', 'avgAmount']    ← summary
[cube] returned 4 rows, columns=['status', 'count', 'totalAmount']       ← by status
RUN_SUCCESS
```

## The LLM demo (Cube as LLM safety layer)

```bash
export OPENAI_API_KEY=sk-...
./setup_cube_llm_demo.sh
```

The killer angle: **give the LLM Cube's schema, not raw table access**.

What it does:
1. Spins up Cube via Docker (same as above, richer schema with descriptions on each measure + dimension so the LLM has grounding text).
2. Prints Cube's `/v1/meta` — this is what the LLM sees as its schema context.
3. Scaffolds a Dagster project with:
   - `cube_customer_totals` — `CubeQueryAssetComponent` querying totalAmount + count per customer, sorted desc
   - `cube_narrative_answer` — `LangChainChainAssetComponent` calling gpt-4o-mini once per row, generating a natural-language sentence with exact numbers
4. Materializes both together.

The LLM never touches SQL. It gets typed, aggregated results from Cube's semantic layer and just narrates them. That's the pattern executives want but few can build without an orchestrator.

## The `strip_cube_prefix` field (LangChain friendliness)

Cube's REST API returns column names like `Orders.customerName`. That's fine for JSON but breaks LangChain's `{var}` template syntax (dots aren't valid identifiers). `CubeQueryAssetComponent` has `strip_cube_prefix: true` **on by default** — column names arrive as `customerName` / `totalAmount` / `count`. Set it to `false` if you want the fully-qualified Cube identifiers.

## Extending to full text-to-Cube-query

The LLM demo hard-codes the Cube query. To make it fully NL-driven:

1. Add an asset that fetches `/v1/meta` and passes it as context to a `langgraph_agent`.
2. The agent's prompt: "Here's the Cube schema. Convert this English question into a Cube JSON query."
3. Its output feeds a `CubeQueryAssetComponent` via a dynamic `query` field (needs component support — currently `query` is static YAML).

This is the natural next step. If you want it, the primitive is here.

## Components overview

`CubeQueryAssetComponent` fields:

| Field | Type | Description |
|---|---|---|
| `asset_name` | string | Output asset name. |
| `query` | dict | Cube JSON query: measures / dimensions / timeDimensions / filters / order / limit / offset. |
| `api_url_env_var` | string | Cube base URL env var. Default `CUBE_URL`. |
| `api_token_env_var` | string | Optional JWT env var for Cube Cloud / production. |
| `strip_cube_prefix` | bool | Default `true`. Strips `<CubeName>.` prefix from columns. |
| `request_timeout_seconds` | int | Default 60. |

`ExternalCubeMetricAsset` fields:

| Field | Type | Description |
|---|---|---|
| `asset_key` | string | Dagster asset key, `/`-separated. |
| `cube_name` | string | Cube name (e.g. `Orders`). |
| `measure_name` | string | Cube measure like `Orders.count`. Set this OR `dimension_name`. |
| `dimension_name` | string | Cube dimension like `Orders.status`. |
| `metric_type` | string | `count` / `sum` / `avg` / `string` / `time` / `number` / `boolean`. |
| `cube_playground_url` / `cube_docs_url` | string | Clickable links surfaced in the catalog. |

## Auth modes

| Cube setup | How to configure |
|---|---|
| Cube Core local dev (no auth) | `api_url_env_var: CUBE_URL` → `http://localhost:4000`. Omit token. |
| Cube Cloud | `api_url_env_var: CUBE_API_URL` → `https://<tenant>.cubecloud.dev/cubejs-api`. `api_token_env_var: CUBE_API_TOKEN` (JWT). |
| Self-hosted Cube in prod | Same as Cube Cloud — set both `api_url_env_var` and `api_token_env_var` with a JWT signed by your Cube secret. |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_cube_query_demo.sh \
  -o setup_cube_query_demo.sh
bash setup_cube_query_demo.sh
```

## See also

- [dbt + ML + dbt (mid-DAG Python)](./dbt_ml_pipeline.md) — same "Python between SQL layers" pattern, but for dbt (warehouse) instead of Cube (semantic layer).
- [LangGraph Agent](./langgraph_agent.md) — multi-step LLM pipeline; would be the "NL query generator" in the extended text-to-Cube flow.
