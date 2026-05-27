# Db2 for i (AS/400 / iSeries / IBM i)

Most of our walkthroughs scaffold a Docker container so customers can run them locally. **This walkthrough is different** — IBM i is proprietary hardware, no Docker image exists. The doc covers how to point existing Dagster components at a real AS/400 system, what changes vs Db2 LUW, and which catalog queries actually work on the i.

If you don't have access to an AS/400 to test against, the [`examples/db2.md`](db2.md) walkthrough still applies — it uses Db2 Community Edition (Docker) and exercises the same `db2_resource` + downstream-component shape on LUW.

## Two-line summary

```yaml
# Connection: db2_resource(system_type=iseries, library_list=[...])
# Catalog discovery / migration: database_schema_inventory(database_type=db2_iseries)
```

Same components as Db2 LUW. Different `system_type` + `database_type` strings, and the `library_list` field plays the role of LUW's `schema:` / `search_path:`.

## Db2 for i vs Db2 LUW — what actually differs

| Aspect | Db2 LUW (Linux / Cloud / Warehouse) | Db2 for i (AS/400 / IBM i) |
|---|---|---|
| Default port | 50000 | **446** (DRDA listener) |
| TLS | Optional on-prem, required on Cloud | **Almost always required** on i 7.x+ |
| Database name | Logical DB name (e.g. `BLUDB`, `testdb`) | **Relational DB Directory entry** — usually the `*LOCAL` system name. Find via `WRKRDBDIRE` on the i. |
| Auth profile | Db2 user | IBM i **user profile** (often `QSECOFR` or a service profile) |
| Schema search path | `SET SCHEMA` / `CURRENT SCHEMA` (single value) | **Library list** — ordered list mapped to `CURRENT SCHEMA` (first entry) + `CURRENT PATH` (rest) |
| Catalog SQL | `SYSCAT.TABLES`, `SYSCAT.PROCEDURES`, etc. | `QSYS2.SYSTABLES`, `QSYS2.SYSPROCS`, `QSYS2.SYSFUNCS`, `QSYS2.SYSSEQUENCES`, `QSYS2.SYSTRIGGERS` |
| Object naming | Schemas + objects | "Libraries" hold "files" (= tables), "members" (= partitions), programs, etc. SQL surface presents these as schemas + tables. |
| Encoding | Usually UTF-8 | Often **EBCDIC**. `ibm_db` converts on read, but mismatched CCSID can produce mojibake — set `CCSID=1208` (UTF-8) on the user profile if you control it. |

The SQLAlchemy dialect is the same — `ibm_db_sa`. Only the connection parameters and the catalog queries differ.

## Wiring it up

### Step 1 — resource

```yaml
# defs/db2_iseries_resource/defs.yaml
type: dagster_community_components.Db2ResourceComponent
attributes:
  resource_key: db2_iseries_resource
  host: as400.example.com           # IBM i system hostname / IP
  database: 'S101ABCD'              # *LOCAL RDB name — see WRKRDBDIRE on the i
  username: QSECOFR
  password_env_var: AS400_PASSWORD
  ssl: true                         # almost always required on i 7.x+
  system_type: iseries              # ← drives port=446 + library-list params
  library_list:                     # ordered; first is CURRENT SCHEMA
    - MYAPP                         # app tables
    - MYAPP_DATA                    # app data
    - QSYS2                         # system views (always last)
```

What the resource does behind the scenes when `system_type=iseries`:

- `port` defaults to `446`
- `library_list` is appended to the connection DSN as `CurrentSchema=MYAPP;LibraryList=MYAPP,MYAPP_DATA,QSYS2;`
- The underlying `ibm_db` driver translates these into the AS/400 DRDA equivalents at session start

### Step 2 — schema inventory (catalog discovery)

This is where Db2 LUW and Db2 for i diverge sharply. **Use `database_type: db2_iseries`** so the component queries `QSYS2.*` instead of `SYSCAT.*`:

```yaml
# defs/as400_inventory/defs.yaml
type: dagster_community_components.DatabaseSchemaInventoryComponent
attributes:
  asset_name: as400_inventory
  connection_env_var: AS400_DB_URL      # SQLAlchemy URL exported by your env / the resource
  database_type: db2_iseries            # ← NOT 'db2' — uses QSYS2 catalog
  schemas: [MYAPP, MYAPP_DATA]
  object_types: [table, view, procedure]
```

The inventory queries that fire on the i:

```sql
SELECT 'table', table_schema, table_name, NULL, NULL
  FROM QSYS2.SYSTABLES
 WHERE table_type IN ('T','P')
   AND table_schema NOT IN ('QSYS','QSYS2','SYSIBM','SYSPROC','SYSCAT','SYSIBMADM','QSYS2ROW');
SELECT 'view',      table_schema, table_name, NULL, NULL  FROM QSYS2.SYSTABLES WHERE table_type='V' ...;
SELECT 'procedure', routine_schema, routine_name, NULL, NULL  FROM QSYS2.SYSPROCS  ...;
SELECT 'function',  routine_schema, routine_name, NULL, NULL  FROM QSYS2.SYSFUNCS  ...;
SELECT 'sequence',  sequence_schema, sequence_name, NULL, NULL  FROM QSYS2.SYSSEQUENCES  ...;
SELECT 'trigger',   trigger_schema, trigger_name, NULL, NULL  FROM QSYS2.SYSTRIGGERS  ...;
```

Output is a normalized DataFrame — same columns as every other dialect (`object_type, schema_name, object_name, definition, row_count`). Pipe it to `dataframe_to_csv` for the team's migration checklist.

### Step 3 — replication / migration

Existing `database_replication`, `database_tables_migration`, etc. accept the same `ibm_db_sa` connection URL. Use the `db2_iseries_resource`'s connection string as the source, and your modern warehouse (Snowflake / BigQuery / Databricks) as the target. The migration components currently send `db2_iseries` source dialects through the same `db2` translation path — works for table DDL + data; the CHECK / trigger / procedure rewrite still needs manual review (PL/SQL → target dialect) per the [`warehouse_migration.md`](warehouse_migration.md) playbook.

## Drivers + connection gotchas

### `ibm_db` works for AS/400, but watch the platform

The IBM `ibm_db` Python driver is the supported path. It bundles `libdb2` for Linux + macOS + Windows wheels. **It connects to the DRDA listener on the i** — make sure:

- The DRDA listener is started on the i: `STRTCPSVR SERVER(*DDM)` (covers DDM/DRDA)
- Port 446 is reachable from your Dagster host
- The user profile has DRDA authority + isn't `*DISABLED`

### Alternative: `pyodbc` + iSeries Access ODBC driver

If your environment already has the **IBM i Access Client Solutions** ODBC driver installed (common on legacy enterprise infra), you can connect via `pyodbc` instead of `ibm_db`. This component doesn't ship a pyodbc path natively — it's a one-line escape hatch:

```python
import pyodbc
conn = pyodbc.connect(
  "DRIVER={IBM i Access ODBC Driver};"
  "SYSTEM=as400.example.com;UID=QSECOFR;PWD=...;"
  "DBQ=MYAPP MYAPP_DATA QSYS2;"      # DBQ = library list
)
```

If you need the resource component to use pyodbc, file an issue — we'll add a `driver: ibm_db|odbc` field.

### Encoding (EBCDIC)

If query results come back as garbled text on string columns, the issue is usually CCSID mismatch. Two fixes:

- Set `CCSID(1208)` (UTF-8) on the user profile: `CHGUSRPRF USRPRF(YOURUSER) CCSID(1208)` from the i.
- Or coerce per-query: `SELECT CAST(my_col AS VARGRAPHIC(100) CCSID 1200) FROM mytbl` (UCS-2). Then decode on the Python side.

## What's NOT covered

- **CL / RPG programs as Dagster assets** — IBM i ships CL (Control Language) + RPG II/III/IV programs. There's no community component that wraps `CALL QSYS/QCMDEXC(...)` today; you'd need to write a custom op against the `ibm_db.callproc` interface, or call them through SQL stored-procedure aliases (`CREATE PROCEDURE ... LANGUAGE CL EXTERNAL NAME ...`).
- **Journaling / commitment control hooks** — IBM i has a journaling system that's distinct from Db2 LUW's transaction log. Out of scope for current components.
- **IBM i task scheduler integration** — i has `WRKJOBSCDE` for scheduled jobs; Dagster doesn't ship a sensor that reads i job-scheduler state. If a customer asks, we'd model it the same way as `precisely_job_sensor`: poll a status table, emit on terminal SUCCESS.

If any of these matter for your environment, file an issue with the specific i version + use case — these are addressable, just not yet shipped.

## See also

- [`db2_resource` README](https://dagster-component-ui.vercel.app/c/db2_resource) — full Db2 family resource
- [`database_schema_inventory` README](https://dagster-component-ui.vercel.app/c/database_schema_inventory) — catalog discovery
- [`examples/db2.md`](db2.md) — Db2 LUW (Docker Community Edition) end-to-end walkthrough
- [`examples/warehouse_migration.md`](warehouse_migration.md) — full migration playbook (Postgres → DuckDB end-to-end; same DDL-first / data-first patterns apply to Db2-i → Snowflake / BigQuery / Databricks)
- [IBM i SQL reference (QSYS2 catalog views)](https://www.ibm.com/docs/en/i/7.5?topic=views-syscolumns)
