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

## Component reference

### snowpark_pipeline

| Field | Type | Required | Description |
|---|---|---|---|
| `asset_name` | string | yes | Output Dagster asset name |
| `connection` | dict | yes | Snowpark `Session.builder.configs(...)` fields. Any key suffixed `_env_var` is resolved from the environment at runtime |
| `source` | dict | yes | `{kind: table\|sql, table\|query: ...}` |
| `operations` | list[dict] | yes | Ordered Snowpark DataFrame ops compiled to one Snowflake SQL statement |
| `sink` | dict | yes | `{kind: table\|none, table: ..., mode: overwrite\|append}` |
| `group_name`, `description`, `asset_tags`, `kinds`, `owners`, `deps` | (standard) | no | Standard Dagster metadata fields |

`connection` fields (any of these can be `<field>_env_var: ENV_NAME` for env-var sourcing):

| Field | Required | Description |
|---|---|---|
| `account` | yes | Snowflake account identifier (`<org>-<account>` or full URL) |
| `user` | yes | Username |
| `password` | one of | Password (literal or `_env_var`) |
| `private_key` | one of | Private key for key-pair auth (literal or `_env_var`) |
| `private_key_passphrase` | optional | Passphrase for encrypted private key (literal or `_env_var`) |
| `authenticator` | one of | `externalbrowser` (SSO), `oauth`, etc. (literal or `_env_var`) |
| `token` | conditional | Required when `authenticator: oauth` (literal or `_env_var`) |
| `role` | recommended | Snowflake role for the session |
| `warehouse` | recommended | Snowflake compute warehouse |
| `database` | recommended | Default database for the session |
| `schema` | recommended | Default schema |

`source` kinds:

| `source.kind` | Required fields | Notes |
|---|---|---|
| `table` | `table` | Reads via `session.table(<name>)` |
| `sql` | `query` | Reads via `session.sql(<query>)` |

`sink` kinds:

| `sink.kind` | Required fields | Behavior |
|---|---|---|
| `table` | `table`, optional `mode` (overwrite/append) | Snowpark writes via `save_as_table(...)`. Result lives in Snowflake |
| `none` | (none) | Collects to pandas via `.to_pandas()`. Use only for small results |

Supported `operations[*].op` values:

| `op` | Params | Notes |
|---|---|---|
| `filter` | `predicate: "<SQL>"` | Snowpark `F.sql_expr(...)` filter |
| `select` | `columns: [a, b]` | `.select(...)` |
| `drop` | `columns: [a, b]` | `.drop(...)` |
| `rename` | `mapping: {old: new}` | Renames via repeated `.rename({old: new})` |
| `with_columns` | `expressions: {name: <SQL expr>}` | `.with_column(name, F.sql_expr(...))` |
| `group_by` | `group_by: [cols]`, `aggregations: {out: {col, agg}}` | `.group_by(...).agg(...)` |
| `sort` | `by: [cols]`, `descending: bool/list` | `.sort(...)` |
| `limit` | `n: int` | `.limit(n)` |
| `distinct` | (none) | `.distinct()` |
| `drop_nulls` | `subset: [cols]` (optional) | `.na.drop(subset=...)` |
| `join` | `right_table: <name>`, `how:`, `on:` (or `left_on`+`right_on`) | Reads right side via `session.table(...)` then `.join(...)` |

Supported `agg` values: `sum / mean / avg / min / max / count / count_distinct / stddev / variance`.

### Component README (full reference)

[snowpark_pipeline](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/transforms/snowpark_pipeline/README.md)

## Demo notes

- **Compute is 100% server-side.** Snowpark builds a query plan from your op list and compiles it to ONE Snowflake SQL statement at the sink. The Python process only submits the SQL and reads back row count. No data leaves Snowflake.
- **Universal `*_env_var` resolution.** Any key in `connection:` suffixed `_env_var` is resolved from `os.environ` at runtime. So all credentials — account / user / password / private_key / authenticator / token — can be sourced from env vars uniformly. Literal values pass through unchanged.
- **Session is per-asset.** Each materialization opens a new Snowpark `Session`, runs the chain, then closes the session. For long-running cluster reuse, use a shared Snowflake session resource (out of scope for this single-asset component).
- **`sink.kind: none` calls `.to_pandas()`.** Useful for downstream Dagster assets that are pandas-shaped. For large results, write to a Snowflake table — collecting to pandas brings everything through the Python wire format.
- **Cost.** Charged to your Snowflake compute warehouse. The demo's workload (small filter / group_by / sort / limit) runs on an XS warehouse in well under a minute — fractions of a credit.
