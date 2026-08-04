# Warehouse migration — the one-time lift+shift from a legacy SQL DB to a modern warehouse
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

> **Replication vs. migration:** replication is the recurring data sync (see [replication.md](replication.md)). **This walkthrough is about migration** — the broader one-time project: schema + data + constraints + views, all the database-resident structure your new warehouse needs.

A real warehouse migration (Oracle → Snowflake, Db2 → BigQuery, MSSQL → Redshift, etc.) isn't just `COPY INTO`. It has five legs:

1. **Discovery** — what's in the source DB?
2. **Assessment** — pre-flight dry-run: what will work, what needs review, what will fail, before any state changes
3. **Schema** — recreate the tables on target with their full DDL (types + PKs + FKs + NOT NULL + DEFAULT + CHECK + UNIQUE)
4. **Data** — stream the rows
5. **Logic** — recreate views and rewrite stored procs / triggers / scheduled jobs

This walkthrough shows the Dagster components that automate the deterministic 80% (1, 2, 3, 4, and views) and where the LLM-assisted-rewrite 20% lives.

## Why Dagster for the migration

A warehouse migration isn't built into Dagster's core value prop — Dagster is for recurring pipelines, not one-shot lift+shifts. But for teams already running on Dagster (or adopting it for the destination), doing the migration *in Dagster* turns out to be the highest-leverage path. Three reasons:

**1. Iteration speed on the dialect-failure loop.**

Migration is mostly an iteration cycle: run → discover that 3 of your 200 views use Oracle `CONNECT BY` → rewrite → re-run → discover that 1 of your 50 tables has an `XMLTYPE` column → override → re-run. The faster that loop runs, the faster the migration completes.

The components emit per-item logs at INFO + WARNING that show up in the same Dagster UI the team uses every day. Failures land in a status DataFrame with the specific error and the source DDL it tried. Fix the rewrite, plug it into `table_ddl_overrides` or `view_ddl_overrides`, re-run. The "what's left to fix?" answer is one CSV away — not a grep across shell-script output.

**2. Status reports as first-class artifacts.**

Every migration component emits a DataFrame: `{object_type, schema, name, status, error_message, ddl}`. `dataframe_union` them, `dataframe_to_csv`, you have the migration completion artifact — the document the migration team submits at the end of the project saying "100% of inventoried objects are accounted for: X migrated, Y skipped due to target limitations, Z manually rewritten."

That artifact is not a side effect of using Dagster — it's a deliverable. With manual scripts or AWS SCT, the team builds it by hand.

**3. The components keep paying after the migration.**

`database_schema_inventory` becomes ongoing schema drift detection — point it at production and re-run weekly, diff against last week. `database_views_migration` re-runs when source views evolve (some teams maintain dual-write for a few months post-cutover). The migration code becomes the audit code.

Compared with the dedicated migration tools (AWS SCT, Microsoft SSMA, manual `pg_dump` + scripts, vendor professional-services engagements), Dagster doesn't move data faster or convert PL/SQL more accurately. What it does is fold the migration into the same observability, lineage, and iteration model the team already uses for production pipelines. After cutover, the components stay live.

For teams who'll be running their analytics on Dagster *after* the cutover anyway, doing the migration there too removes a tool boundary.

## Components used

| Component | Role |
|---|---|
| `database_schema_inventory` | Lists every object (tables/views/procs/functions/sequences/triggers/jobs) in the source DB — the migration scope |
| `database_migration_assessment` | **NEW.** Pre-flight dry-run for tables + views. Returns `{status, complexity, dialect_markers, proposed_ddl}` per object. No state changes on target. Inspired by AWS SCT Assessment Report + Microsoft SSMA test mode. |
| `database_tables_migration` | **DDL-first** workflow: builds portable `CREATE TABLE` from source `INFORMATION_SCHEMA` (types + PKs + FKs + NOT NULL + DEFAULT + CHECK + UNIQUE) and executes on target. Supports `dry_run`. |
| `database_constraints_migration` | **Data-first** workflow: applies PKs / FKs / NOT NULL / DEFAULTs / CHECKs / UNIQUEs to tables Sling already created. Supports `dry_run`. Auto-skips constraint types the target can't ALTER. |
| `database_replication` | Moves table data via Sling. With `mode: truncate` it preserves any DDL you set up first. |
| `database_views_migration` | Bulk-migrates views to target with table-ref + function-name substitutions. Supports `dry_run`. |
| `dataframe_to_csv` | Dumps the per-component status DataFrames to CSV — the migration completion report |

All migration components accept `dry_run: true` — try every DDL inside a transaction that ROLLS BACK. Use it before the real run to validate without touching target state.

Every "migration" component emits a per-row status DataFrame (`status`, `error_message`, plus context). Pipe to CSV and the migration team has a real artifact.

## Step 0: assess before you migrate

Before touching target state, run `database_migration_assessment`. It dry-runs every CREATE TABLE + CREATE VIEW inside a transaction that rolls back, then returns one DataFrame:

```yaml
type: dagster_component_templates.DatabaseMigrationAssessmentComponent
attributes:
  asset_name: migration_assessment
  source_connection_env_var: ORACLE_DB_URL
  target_connection_env_var: SNOWFLAKE_DB_URL
  source_type: oracle
  target_type: snowflake
  schemas: [HR, FINANCE]
  target_schema: RAW
  table_replacements:
    HR.EMPLOYEES: RAW.EMPLOYEES
  function_replacements:
    NVL: COALESCE
```

What it gives you:

```
| object_type | name              | status            | complexity | dialect_markers       | reason                  |
|-------------|-------------------|-------------------|------------|-----------------------|-------------------------|
| table       | HR.EMPLOYEES      | auto_convertible  | simple     |                       |                         |
| table       | HR.XML_DOCUMENTS  | will_fail         | complex    | XMLTYPE               | unsupported column type |
| view        | HR.V_DEPT_HIER    | will_fail         | complex    | CONNECT BY            | syntax error            |
| table       | FINANCE.STG_TEMP  | needs_review      | medium     | NVL, DECODE           |                         |
```

Plus asset metadata: `auto_convertible_pct`, `complexity_simple/medium/complex` counts, `estimated_manual_effort` (rough person-hours). This is your migration scope document — the SCT Assessment Report equivalent. **Run this first.**

When the assessment shows ≥ 95% auto-convertible + no critical `will_fail` rows, move to the real migration.

## Two workflows — pick one

### Workflow A: DDL-first (recommended for new migrations)

```
inventory ─▶ tables_migration ─▶ replication (mode: truncate) ─▶ views_migration
                                                                       │
                                                                       ▼
                                                          migration_completion.csv
```

1. **Inventory** — `database_schema_inventory` lists what exists. Output piped to `dataframe_to_csv` is the scope artifact.
2. **Tables (DDL-first)** — `database_tables_migration` recreates target tables with full DDL.
3. **Data** — `database_replication` with `mode: truncate` wipes the data in the pre-shaped target tables and reloads (truncate preserves DDL; `full_refresh` would drop and recreate without your constraints).
4. **Views** — `database_views_migration` reads + applies substitutions + recreates on target.

Bad rows fail at INSERT time (where you can see them). Constraints are present before any data lands. Tight audit story: one CREATE TABLE per table.

### Workflow B: Data-first (faster initial setup, recover constraints after)

```
inventory ─▶ replication (mode: full_refresh) ─▶ constraints_migration ─▶ views_migration
                                                          │
                                                          ▼
                                              completion_csvs (per step)
```

1. **Inventory** — same.
2. **Data** — `database_replication` with `mode: full_refresh` (Sling creates tables from type inference + loads data).
3. **Constraints** — `database_constraints_migration` reads PKs/FKs/NOT NULLs/DEFAULTs from source and applies them as `ALTER TABLE` on target.
4. **Views** — same.

Easier setup; FK failures show the orphan rows (clean them, re-run). Type fidelity is worse than DDL-first (Sling infers types from data — `NUMBER(38,10)` may become `NUMERIC(38,18)`).

**This demo runs Workflow A.** The setup script also writes out the YAML for Workflow B as a comment so you can swap.

## Architecture (this demo)

```
                  ┌───────────────────────────────────────┐
                  │      legacy_app (Postgres)            │
                  │  2 tables (with PK + FK + NOT NULL +  │
                  │  DEFAULTs), 1 view, 2 functions,      │
                  │  1 sequence, 1 trigger                │
                  └─────────────────┬─────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
 ┌────────────────┐    ┌────────────────────┐    ┌────────────────────┐
 │ schema_        │    │ tables_migration   │    │ views_migration    │
 │   inventory    │    │ (CREATE TABLE w/   │    │ (CREATE OR REPLACE │
 │ (DataFrame)    │    │  PK+FK+NOT NULL+   │    │  VIEW + substitutes)│
 │                │    │  DEFAULT)          │    │                    │
 └────────┬───────┘    └─────────┬──────────┘    └─────────┬──────────┘
          ▼                      ▼                          │
 ┌────────────────┐    ┌────────────────────┐               │
 │ to_csv:        │    │ replication        │               │
 │ migration_plan │    │ (mode: truncate)   │               │
 │ .csv           │    └─────────┬──────────┘               │
 └────────────────┘              ▼                          │
                       ┌────────────────────────────────────┘
                       ▼
              ┌─────────────────────────────────┐
              │       DuckDB warehouse          │
              │  raw.customers (50 rows, PK)    │
              │  raw.orders (200 rows, PK + FK) │
              │  raw.v_orders_summary           │
              └─────────────────────────────────┘
```

`deps:` chains step the pipeline through inventory → DDL → data → views. Each migration component emits a status DataFrame.

## Run

```bash
bash setup_warehouse_migration_demo.sh
cd warehouse-migration-demo
source .env.demo

uv run dg check defs
uv run dg launch --assets '*'
```

Verify:

```bash
# Tables created with PK/FK/NOT NULL/DEFAULT
duckdb /tmp/wm-warehouse.duckdb -c "DESCRIBE raw.orders; SELECT * FROM information_schema.table_constraints WHERE table_schema='raw';"

# Data loaded
duckdb /tmp/wm-warehouse.duckdb -c "
SELECT 'customers' AS tbl, COUNT(*) FROM raw.customers
UNION ALL SELECT 'orders', COUNT(*) FROM raw.orders
UNION ALL SELECT 'v_orders_summary', COUNT(*) FROM raw.v_orders_summary;"

# Migration completion report
cat /tmp/legacy_db_migration_plan.csv
```

Cleanup: `docker rm -f dg-wm-postgres && rm -f /tmp/wm-warehouse.duckdb /tmp/legacy_db_migration_plan.csv`.

## What every component delivers — the status DataFrame

Each migration component returns a DataFrame, so you can pipe any of them to `dataframe_to_csv` for the corresponding artifact:

| Component | Row shape |
|---|---|
| `database_schema_inventory` | `object_type, schema_name, object_name, definition, row_count` |
| `database_tables_migration` | `schema_name, table_name, target_table, status, error_message, n_columns, has_primary_key, n_foreign_keys, ddl_preview` |
| `database_constraints_migration` | `schema_name, table_name, target_table, constraint_type, constraint_name, columns, status, error_message, ddl` |
| `database_views_migration` | `schema_name, view_name, target_view, status, error_message, ddl_chars` |

The "failed" rows are concrete and actionable — Oracle `CONNECT BY` shows up with its error, FK violations show the orphan rows, type translations that don't fit show up with the DuckDB error message. Migration team has a real punchlist.

## What still rebuilds manually — and the LLM-assist pattern

Some source objects can't auto-translate because the syntax / language fundamentally differs in the target:

- **Stored procedures** (PL/SQL / T-SQL / PL/pgSQL): Snowflake JS UDFs, BigQuery JS/Python UDFs, or Dagster `sql_transform` assets that run the equivalent SQL
- **Scheduled jobs** (Oracle DBMS_SCHEDULER, SQL Server Agent): Dagster `@schedule` or `AutomationCondition`
- **Triggers** (`BEFORE INSERT/UPDATE`): usually fold into ETL — your warehouse doesn't need them
- **Dialect-quirky views** (Oracle `(+)` outer joins, MSSQL `CROSS APPLY`): rewrite as ANSI SQL, then run through `database_view_migration` or rebuild as `sql_transform`

**For the manual rewrites, the LLM is your power tool — outside Dagster.** Open Claude / ChatGPT / your editor's AI assistant with this prompt template:

> Rewrite this Oracle PL/SQL procedure for Snowflake. Use SQL where possible, JavaScript UDF only when SQL can't express it. Preserve the parameter signature and semantics. Output only the new procedure body.

Then either:

1. Apply the result manually and verify
2. Or save the rewrites as `defs/views/*.sql` files and pipe through `database_view_migration` (with the `function_replacements` config doing the easy bits and the LLM-rewritten SQL covering what `function_replacements` can't)

The components stay deterministic and asset-producing. The judgment-call work lives in the LLM / human review loop. Clean split.

## Production retargeting

### Oracle → Snowflake migration

```yaml
# defs/tables_ddl/defs.yaml
type: dagster_component_templates.DatabaseTablesMigrationComponent
attributes:
  source_type: oracle
  target_type: snowflake
  source_connection_env_var: ORACLE_URL
  target_connection_env_var: SNOWFLAKE_URL
  schemas: [HR, FINANCE, BILLING]
  target_schema: RAW
  drop_if_exists: true
```

```yaml
# defs/views/defs.yaml
type: dagster_component_templates.DatabaseViewsMigrationComponent
attributes:
  source_type: oracle
  target_type: snowflake
  # …
  function_replacements:
    NVL: COALESCE
    SYSDATE: CURRENT_TIMESTAMP
    DECODE: IFF      # rough — review the failures
  table_replacements:
    HR.EMPLOYEES: RAW.EMPLOYEES
```

```bash
export ORACLE_URL='oracle+oracledb://migrator:****@oracle-prod:1521/?service_name=PRD'
export SNOWFLAKE_URL='snowflake://etl_user:****@account.snowflakecomputing.com/RAW_DB/PUBLIC?warehouse=LOAD_WH'
```

### Db2 → Snowflake / Snowflake → BigQuery / Postgres → Redshift

Same shape — `source_type` / `target_type` + connection URLs change. Everything else identical.

## Demo notes

- **DDL-first preserves type fidelity.** Sling's type inference can lose precision (`NUMBER(38,10)` → `NUMERIC(38,18)`). DDL-first uses source `INFORMATION_SCHEMA` types directly via the translation map.
- **`mode: truncate` is what makes DDL-first work.** With `mode: full_refresh`, Sling drops and recreates the table (wiping your DDL). With `mode: truncate`, it preserves the table structure and just wipes data.
- **`CREATE SCHEMA IF NOT EXISTS` is automatic.** When `target_schema` is set, the component creates it on first run.
- **Re-runs are safe.** `drop_if_exists: true` makes the DDL step idempotent. Sling `truncate` and `full_refresh` are inherently idempotent. Views use `CREATE OR REPLACE`.
- **Two databases are different.** Postgres → DuckDB in this demo gives you the real "different DB type on each side" topology without needing cloud credentials. Oracle → Snowflake / Db2 → Snowflake / Postgres → BigQuery production swaps are the same component graph.

## Constraint coverage + enforcement on the target

The DDL components migrate **6 constraint types** automatically:

| Constraint | `database_tables_migration` (DDL-first) | `database_constraints_migration` (data-first) |
|---|---|---|
| Primary keys | inline `PRIMARY KEY` | `ALTER TABLE … ADD PRIMARY KEY` |
| Foreign keys | inline `FOREIGN KEY … REFERENCES` (with `table_replacements`) | `ALTER TABLE … ADD FOREIGN KEY` |
| NOT NULL | inline | `ALTER COLUMN … SET NOT NULL` |
| DEFAULT | inline | `ALTER COLUMN … SET DEFAULT` |
| CHECK | inline `CHECK (…)` with Postgres rewrites (`= ANY (ARRAY[])` → `IN ()`, `::cast` stripped) + `function_replacements` for dialect-specific functions | `ALTER TABLE … ADD CHECK` |
| UNIQUE | inline `UNIQUE (…)` | `ALTER TABLE … ADD UNIQUE` |

**Target enforcement varies.** The components emit pre-flight warnings:

- DuckDB / Postgres / MySQL / Oracle / Db2 / MSSQL: enforce everything ✓
- Snowflake / Redshift: accept PK / FK / CHECK / UNIQUE but **don't enforce** (informational metadata only — bad rows can still land)
- BigQuery: same, plus **CHECK is unsupported** (DDL would fail — skip via `include_check_constraints: false`)

Constraints the auto-generator can't translate (Oracle `REGEXP_LIKE` in a CHECK, complex CASE expressions, etc.) land in the status DataFrame as failures. Fix via:
- `function_replacements` for simple function-name swaps
- `table_ddl_overrides` for full hand-written CREATE TABLE
- Manual rewrite of the source check + re-run

## Status DataFrame shape (now with CHECK + UNIQUE counts)

```
| schema_name | table_name | target_table | status  | n_columns | has_primary_key | n_foreign_keys | n_check_constraints | n_unique_constraints | error_message | ddl_preview |
|-------------|------------|--------------|---------|-----------|-----------------|----------------|--------------------:|---------------------:|---------------|-------------|
| app         | customers  | raw.customers| success | 4         | true            | 0              | 1                   | 1                    |               | CREATE TABLE…|
| app         | orders     | raw.orders   | success | 6         | true            | 1              | 2                   | 0                    |               | CREATE TABLE…|
```

Aggregating across the migration: `dataframe_union` the status frames from `database_tables_migration` + `database_constraints_migration` + `database_views_migration` → single completion artifact CSV.

## See also

- [`replication.md`](replication.md) — the recurring-sync use of `database_replication`
- [`oracle.md`](oracle.md), [`db2.md`](db2.md) — proprietary-source DB demos
- [`dataframe_to_snowflake_bulk`](https://dagster-component-ui.vercel.app/c/dataframe_to_snowflake_bulk) — companion sink for in-memory bulk loads after migration
