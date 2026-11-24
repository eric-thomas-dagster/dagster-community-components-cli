# adf-to-dagster

Convert Azure Data Factory pipelines, triggers and linked services into a Dagster project with a migration report. Sibling of `runmyjobs-to-dagster`: the same approach (deterministic loader and converter, loud stubs, a four-gate check with a dry run, a per-pipeline "Needs your input" list), and a skill, `skills/migrating-adf-to-dagster`, that drives it.

**Status: first converter.** It converts control flow, variables, expressions, SQL (Lookup, Script with bound parameters, stored procedure), web calls (including managed-identity auth), Azure Function calls, Fabric notebooks (through the registry's `fabric_workspace` client and component), Office 365 mail over SMTP, Delete, GetMetadata, Validation and file Lookups, Databricks notebooks (through the Databricks SDK), Copy for the common pairs (file copies and conversions between delimited text, JSON, Parquet and Excel over blob, ADLS, S3 and file servers; REST to file; files into SQL; SQL to files; SQL to SQL across the SQL Server, Postgres, MySQL, Oracle and Snowflake families), schedule triggers and tumbling windows, and Mapping Data Flows (over SQL tables in one database they compile to the registry's `warehouse_pipeline` component, one asset per sink; otherwise they run in the project's own process) made of source, sink, select, filter, derive, sort, aggregate, join, union, exists, lookup, conditional split, surrogate key, rank, alter row, window, pivot, unpivot and flatten, run in the generated project's own process (not on Spark). SSIS packages, Synapse notebooks and Spark jobs, Fabric notebooks and SQL-to-SQL Copy become registry-component assets with lineage when the pipeline can be split (loaded offline through generated subclasses; never run against a live service). Application connectors (Dynamics, Salesforce and the like), Lakehouse and warehouse sinks, Mapping Data Flows that use parse, call, assert, `byName` or column patterns, and HDInsight Spark are loud stubs (`ADF-CAVEAT`), and event triggers are not generated. It has not run against a live Azure factory. The first phase for a customer is not this tool: the registry's `AzureDataFactoryComponent` already observes and orchestrates ADF pipelines (see the skill).

```
uv sync --extra dev
uv run adf-to-dagster inventory path/to/factory             # read-only manifest
uv run adf-to-dagster convert path/to/factory --out-dir out --pkg my_migrated
cd out && uv sync
uv run adf-to-dagster validate out --execute --advance      # gates 1-4; 4 is a dry run (ADF_DRY_RUN=1)
uv run adf-to-dagster status summary out/migration_manifest.json
```

`path/to/factory` is a git-integrated factory folder or an ARM export (mixed is fine). Secrets are dropped on load; generated code reads `ADF_CONN_<LINKED_SERVICE>` (a SQLAlchemy URL), `ADF_FS_<LINKED_SERVICE>` (JSON fsspec storage options) and `ADF_GLOBAL_<NAME>` from the environment.

## What is here

- `src/adf_to_dagster/`: `loader.py` (both layouts, secrets removed), `expr.py` (ADF expressions to Python), `convert.py`, `runtime.py` (copied into each generated project), `validate.py` (gates), `manifest.py`, `questions.py`, `cli.py`.
- ``: the skill, `reference/mapping.md` (what is translated and verified), `reference/component-wishlist.md` (what the registry could add: a configurable Copy, offline loading for SSIS and Synapse, storage steps).
- `docs/DESIGN.md`, `docs/migration-intake.md` (what to ask a customer for).
- `tools/survey.py`: sizes up a folder of ADF JSON (activity mix, functions, Copy pairs).
- `tools/corpus.py`: converts every template in a folder as its own factory, imports the result and dry-runs each pipeline; `reference/corpus-results.md` has the numbers for 584 pipelines from 10 public repos (433 dry-run clean, 0 unexpected failures).
- `tests/`: 512 tests; `tests/fixtures/` has Microsoft's public samples (MIT, see its README) and a synthetic factory (`synthetic/`) that exercises every translated activity.
