# Snowpark Pipeline — full compute pushdown to Snowflake

Multi-step Snowpark DataFrame chain compiled into ONE Snowflake SQL statement. The whole pipeline runs inside the Snowflake compute warehouse — no data ever flows through Python.

## When to use

- Source + sink both live in Snowflake
- You want Snowflake's optimizer (not Python) to plan the work
- The pipeline is complex (multi-step filter / join / aggregate / window) — too complex for the single-step `warehouse_summarize` DSL
- You want the full Snowpark DataFrame API, not just SQL CTAS templates

For portable warehouse pipelines (Snowflake + BigQuery + Redshift + Databricks + DuckDB + Postgres + MSSQL), use `warehouse_pipeline` instead — same op vocabulary, compiles to a CTAS+CTE chain, no Snowflake-specific dependency.

## Components exercised (1)

| Component | What it does |
|---|---|
| `snowpark_pipeline` | Opens a Snowpark `Session` with the configured connection params, reads `RAW.ORDERS`, runs a 5-op chain (filter → with_columns → group_by → sort → limit), writes `ANALYTICS.TOP_5_CATEGORIES_BY_REVENUE`. The chain compiles to one Snowflake SQL statement at the sink. |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowpark_pipeline_demo.sh | bash
cd snowpark-pipeline-demo

# Validate the project loads cleanly (no creds needed)
uv run dg check defs

# To actually materialize, set Snowflake creds + run launch
export SNOWFLAKE_ACCOUNT='myorg-myaccount'   # or full URL
export SNOWFLAKE_USER='ETL_USER'
export SNOWFLAKE_PASSWORD='...'
uv run dg launch --assets '*'
```

## Requires

- A Snowflake account (free trial at snowflake.com/start is fine)
- `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` env vars
- Source table at `RAW.ORDERS` in your target database (seed snippet in the setup script's trailing MSG)

## Validation status

This demo is **code-level validated** — `dg check defs` confirms the YAML loads and resolves correctly. End-to-end materialization requires real Snowflake credentials and isn't run as part of the community CI. The component itself is the same shape as the per-engine `pyspark_pipeline` (which IS end-to-end validated locally), so the YAML contract is well-tested.

If you have a Snowflake trial account, this demo will run start-to-finish in well under a minute on the smallest warehouse (XS).

## YAML

```yaml
type: dagster_component_templates.SnowparkPipelineComponent
attributes:
  asset_name: top_5_categories_by_revenue
  connection:
    account_env_var:  SNOWFLAKE_ACCOUNT
    user_env_var:     SNOWFLAKE_USER
    password_env_var: SNOWFLAKE_PASSWORD
    role:      TRANSFORMER
    warehouse: COMPUTE_WH
    database:  ANALYTICS
    schema:    STAGING
  source:
    kind: table
    table: RAW.ORDERS
  operations:
    - op: filter
      predicate: "STATUS = 'paid' AND AMOUNT > 100"
    - op: with_columns
      expressions:
        is_high_value: "CASE WHEN AMOUNT > 1000 THEN TRUE ELSE FALSE END"
    - op: group_by
      group_by: [REGION, CATEGORY]
      aggregations:
        revenue:     {col: AMOUNT,   agg: sum}
        order_count: {col: ORDER_ID, agg: count}
    - op: sort
      by: [REGION, revenue]
      descending: [false, true]
    - op: limit
      n: 5
  sink:
    kind: table
    table: ANALYTICS.TOP_5_CATEGORIES_BY_REVENUE
    mode: overwrite
```

## Connection

The `connection:` dict accepts any Snowpark `Session.builder.configs(...)` field. Any key suffixed `_env_var` is resolved from the environment at runtime; literal values pass through. So:

```yaml
connection:
  account_env_var:  SNOWFLAKE_ACCOUNT     # → account = os.environ['SNOWFLAKE_ACCOUNT']
  user:             ETL_USER              # → user = 'ETL_USER' (literal)
  password_env_var: SNOWFLAKE_PASSWORD    # → password = os.environ['SNOWFLAKE_PASSWORD']
  role: TRANSFORMER
  warehouse: COMPUTE_WH
  database: ANALYTICS
  schema: STAGING
```

### Auth alternatives

- **Key-pair auth**: `private_key_env_var` + optional `private_key_passphrase_env_var`
- **SSO**: `authenticator: externalbrowser`
- **OAuth**: `authenticator: oauth` + `token_env_var: SNOWFLAKE_OAUTH_TOKEN`

## How vs `warehouse_pipeline`

Both compile a list-of-ops DSL into a single SQL statement run by the engine:

| | `warehouse_pipeline` | `snowpark_pipeline` |
|---|---|---|
| Engine | Any SQLAlchemy-supported warehouse | Snowflake-only |
| Compilation | CTE-WITH chain → one CTAS | Snowpark plan → one SQL statement |
| Op DSL | Custom (filter / group_by / etc.) | Same op vocabulary |
| Native joins | LEFT/INNER/FULL/CROSS via `join` op | Same, plus Snowpark's full DataFrame join semantics |
| Snowflake-specific funcs | Inline as SQL fragments in expressions | Snowpark API has typed wrappers |
| Portability | Snowflake / BigQuery / Redshift / Databricks / DuckDB / Postgres / MSSQL / MySQL | Snowflake only |

Use `snowpark_pipeline` when you're Snowflake-native and want the rich DataFrame API; use `warehouse_pipeline` when you want one config that retargets across warehouses.

## Comparison with the polars/pyspark pipelines

- `polars_pipeline` — multi-step LazyFrame in one Dagster asset. Data flows through Python (memory-bound).
- `pyspark_pipeline` — multi-step Spark DataFrame in one asset. Compute pushed to Spark cluster; data flows through Spark workers.
- `snowpark_pipeline` — multi-step Snowpark DataFrame in one asset. Compute pushed to Snowflake; **no data through Python at all** — Snowflake-resident the whole time.

Pick by where the data already lives.
