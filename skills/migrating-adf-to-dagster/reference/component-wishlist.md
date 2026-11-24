# What the ADF converter needs from the components session

Written against registry 0.10.178 and measured on 584 public pipelines from 10 repos (`corpus-results.md`): 429 dry-run clean, 125 stubs, 29 that fail by design (an unconditional Fail activity). The sources are samples and one framework (procfwk is 275 of the 584), not an estate, so use the ranking, not the percentages. Everything below was read from the components' code and READMEs; apart from `fabric_workspace` (proven with a customer), none of them has been run against a live service by us.

## Status: registry 0.10.179

The components session built the asks. Read from the released package (code and field lists, not run against live services by us):

| # | Delivered as | Converter now |
|---|---|---|
| 1 | `packages:` (SSIS), `notebooks:` and `spark_jobs:` (Synapse), `notebooks:`, `pipelines:`, `dataflows:` (Fabric) | **adopted**: the three generated subclasses are deleted, the YAML uses the registry components |
| 2 | `dry_run` on SSIS, Synapse, Fabric and `sql_to_database_asset` | **adopted**: `dry_run: "{{ env.ADF_DRY_RUN == '1' }}"` in the YAML. `warehouse_pipeline` has none, so it keeps a small generated subclass |
| 3 | `item_deps` on `fabric_workspace` | **adopted** |
| 4 | Synapse notebook parameters (`notebooks[].parameters`), final-state polling, failures propagate, no client at load | **adopted**: literal notebook parameters are passed, the "swallowed failure" caveat is gone. Spark job definitions still take only a name |
| 5 | `semantic_model_refresh_asset` | **adopted**: `PBISemanticModelRefresh` is a component asset (literal `waitOnCompletion`, `commitMode`, `refreshType` and the enhanced-refresh options carried over). The export holds no workspace or model id, so they are env vars to set and the item is review |
| 6 | `wildcard`, `recursive` and a static directory on `adls_to_database_asset` | **deliberately not adopted**: it reads with a delimiter only (no header, quote or escape options, no pre-copy script, no typed columns), authenticates with a connection string, and has no `dry_run`. The inline Copy honors all of those, so the component would be a less faithful route. Would need: header and quote options, `dry_run` |
| 7, 9 | not checked | open |
| 8 | `FabricRunConfig` (parameters from run config at materialization) | **deliberately not adopted**: expression parameters come from earlier activities in the same run, which a run config set before launch cannot carry. They stay inline |
| 10 | transform semantics (pivot, Rank, Sort, lookup, window_calculation) | **not adopted**: the conditions are pandas `eval` strings, so every ADF expression would need a second translator, and a flow as one `warehouse_pipeline` already gives per-sink assets |
| 11 | `if_exists: upsert` with `upsert_key_columns` on `sql_to_database_asset` (and the DataFrame sinks) | **adopted** for SQL copy: `writeBehavior: upsert` with literal keys. A `preCopyScript` or column mappings still keep the copy inline |
| 12 | `warehouse_pipeline` sink modes `truncate_insert` and `merge` (plus `incremental`) | **adopted**: a `truncate` sink is `truncate_insert` (the table, grants and indexes stay), `recreate` is `replace`. `merge` needs alter row in SQL, not built. No plain append mode |
| 13 | `join`, `union` and `sql` ops in `polars_pipeline` and `pyspark_pipeline` | **not adopted, with reasons**: `polars_pipeline` reads CSV with an inferred schema (a `001` column becomes an integer, which ADF's declared-type read would not do) and offers no header or quote options; `pyspark_pipeline` needs a Spark runtime to verify against, which this environment lacks. Only parquet-to-parquet flows would be exact, and none in the corpus is. Would need: `schema` and CSV read options on both file sources |

The table below is the original request; the rows above supersede it.

## Asks, in priority order

| # | Ask | Component | Why it matters |
|---|---|---|---|
| 1 | **A declared-items option** that skips discovery: `packages:` / `notebooks:` / `spark_jobs:` / `pipelines:` / `dataflows:` (name, id, optional parameters) | `ssis_workspace`, `azure_synapse`, `fabric_workspace` | An ADF pipeline already names what it runs. Today each of these can only list items from the live service, so a generated project cannot load, be checked or be dry-run without one. We work around it with three generated subclasses (`AdfSsisWorkspaceComponent`, `AdfAzureSynapseComponent`, `AdfFabricWorkspaceComponent`) that write a state file or override the lister; they should not need to exist. |
| 2 | **A project-wide `dry_run`** (flag or `*_DRY_RUN=1`) that renders and logs, sends nothing | the same three, `sql_to_database_asset`, `fabric_pipeline_trigger_job` | The SQL, email and SAP components have it. The migration gate runs every asset once with nothing leaving the process; the Fabric `fabric/skip` run tag only works per run. We wrap the assets today. |
| 3 | **Per-item upstream assets** on `fabric_workspace` (`item_deps: {item name: [asset keys]}`, like Synapse's `asset_overrides.depends_on` and SSIS's `assets_by_package.deps`) | `fabric_workspace` | `upstream_asset_keys` applies to every item. We add per-item deps through the `_runnable_asset_kwargs` seam (thank you for it). |
| 4 | **Fix three behaviors in `azure_synapse`** (found by reading the code): the notebook asset catches any exception from `begin_execute_notebook` and succeeds with `status: Skipped`; neither the notebook nor the Spark job asset checks the final state of the run it starts; `build_defs` builds the credential and both clients at load time, so a placeholder tenant id fails at load | `azure_synapse` | A notebook that never started looks successful. Also: neither takes parameters, so an ADF activity that sets them stays a stub. |
| 5 | **A semantic model refresh component** (Power BI / Fabric REST: `POST .../datasets/{id}/refreshes`, wait for completion, optional enhanced-refresh body) | new | ADF `PBISemanticModelRefresh` has none; `fabric_workspace` lists SemanticModel as external-only. The one genuinely missing piece on the Fabric side. |
| 6 | **A static source path (and wildcard, recursive) on the file-to-database components**, plus **a configurable same-format file copy** that takes store, path and wildcard from configuration | `adls_to_database_asset`, `s3_to_database_asset`; new file copy | See "Copy" below. Their source file arrives through `run_config` from a sensor, so a Copy with a known path cannot be one asset. |
| 7 | **`file_ingestion` reading from a URL** (blob, ADLS, S3 through fsspec, credentials from an environment variable), not just a local `file_path` | `file_ingestion` | It is the natural source half of file-to-Dynamics, file-to-Snowflake and similar chains, which today stay stubs. |
| 8 | **Run parameters from run time** on `fabric_workspace` (`item_parameters` is static YAML) | `fabric_workspace` | A Fabric notebook whose parameters come from an activity output or a pipeline parameter cannot be a component asset; the converter runs it inline through `FabricWorkspaceResource` instead. |
| 9 | Event triggers: sensors over blob-created, Event Grid and Service Bus events that start a run with the file path as run config | `adls_observation_sensor`, `servicebus_observation_sensor` | ADF storage and custom event triggers generate nothing today. Whether these emit runs carrying the event's path is the question. |
| 10 | **Close the gaps between the DataFrame transform components and ADF data flows** (table in "Mapping Data Flow" below), so a flow can become a chain of component assets with per-step lineage instead of one inline run | `pivot`, `Rank`, `Sort`, `lookup`, `window_calculation`, `Array Exploder` and friends | The registry already has a component for almost every transformation (see the table). What blocks using them is not a missing component but semantics: pivot has one value column and no naming pattern, Rank one key, Sort one null position, lookup no sort conditions or match flag, flatten no unroll root; and the conditions and formulas are pandas `eval` strings, so every ADF expression would need a translator to pandas or SQL |
| 12 | **An append and a merge mode on `warehouse_pipeline` sinks** (today `replace` and `create_if_not_exists`), and a table-sink option that keeps the existing table (truncate and insert) | `warehouse_pipeline` | A Mapping Data Flow SQL sink usually inserts, truncates and inserts, or upserts by key. The converter compiles a flow to `warehouse_pipeline` steps (see `docs/DATAFLOW.md`) but can only use it when every sink is a full replacement, so a flow that appends or upserts stays inline |
| 13 | **`join`, `union` and a `sql` step in `polars_pipeline` and `pyspark_pipeline`**, and file sinks with a name | `polars_pipeline`, `pyspark_pipeline` | Most public data flows read and write files. Their operation lists (filter, select, with_columns, group_by, sort, limit ...) cannot express a join or a union, so a file flow cannot be one asset with Spark or polars behind it. `warehouse_pipeline` already has both and a `sql` escape hatch |
| 11 | **Upsert, key columns and auto-created tables** on the SQL and DataFrame sinks | `sql_to_database_asset`, `dataframe_to_snowflake`, `dataframe_to_redshift`, `dataframe_to_databricks` | ADF's SQL sink has `writeBehavior: upsert` with `upsertSettings.keys` and `tableOption: autoCreate`, and data flows add update and delete by key. The converter does these inline (`_insert_records`); a SQL-to-SQL Copy that needs them cannot become the component asset |

Reference code you can lift: `src/adf_to_dagster/dflow.py` (data flow script parser, expression language and executor) and `src/adf_to_dagster/runtime.py` in the converter repo have tested implementations (real file, sqlite and local-HTTP tests, not run against cloud services) of a same-format file copy with wildcard, recursive and flatten options (`adf_copy_files`), CSV, JSON, Parquet and Excel conversion (`adf_copy_convert`), streaming SQL-to-SQL with a pre-copy script (`adf_copy_sql_to_sql`), SQL to and from files, REST with `AbsoluteUrl` pagination (`adf_copy_rest`), file lookups, `GetMetadata`, `Validation` and `Delete` over fsspec.

## Copy

Copy is the most common ADF activity. The converter translates these pairs itself, inline, with generated Python over fsspec, pandas and SQLAlchemy:

| Pair | Where |
|---|---|
| same-format file copy (blob, ADLS Gen2, S3, file server; wildcards, folders, flatten) | `adf_copy_files` |
| delimited text, JSON, Parquet and (as a source) Excel into each other | `adf_copy_convert` |
| REST to JSON or delimited text, with `AbsoluteUrl` pagination and a request interval | `adf_copy_rest` |
| delimited text, Parquet, JSON or Excel into a SQL table (with a pre-copy script and column mappings) | `adf_copy_csv_to_sql`, `adf_copy_frames_to_sql` |
| SQL to delimited text, JSON or Parquet | `adf_copy_sql_to_csv`, `_json`, `_parquet` |
| SQL to SQL (the SQL Server family plus Postgres, MySQL, Oracle and Snowflake through a SQLAlchemy URL; Snowflake, MySQL and Oracle type names are from ADF's connector list, not from an export) | `adf_copy_sql_to_sql` |

SQL to SQL becomes a `sql_to_database_asset` asset instead (with lineage) when its pipeline can be split and its tables and query are literal; otherwise, and for every other pair, it runs inline. These registry components already cover parts of the rest, read from their field lists:

| ADF pair | Registry component | Gap |
|---|---|---|
| delimited text (ADLS or S3) to SQL | `adls_to_database_asset`, `s3_to_database_asset` | ask 6: no static path |
| REST to file | `rest_api_ingestion` then `dataframe_to_adls` / `dataframe_to_s3` | two chained assets; does it page results? |
| SQL to file | `dataframe_to_adls`, `dataframe_to_s3` after a query asset | no single SQL-to-file component |
| same-format file copy | `s3_copy_object_asset` (one object, S3 to S3) | ask 6: no wildcard or recursive copy, no blob, ADLS or file server |
| file to Dynamics (6 in the corpus) | `file_ingestion` then `dynamics_crm_record_upsert` (`alternate_key_field`, `fields_map`, `upstream_asset_key`) | ask 7; the converter would chain the two and map ADF's `DynamicsSink` `alternateKeyName` and column mapping onto them |
| Dynamics to file (5) | `dynamics_crm_ingestion` (dlt over the Dataverse Web API, `resources`, `bucket_url`) | its output is a dlt load into a bucket, not a file the pipeline names; ADF's `DynamicsSource` takes FetchXML or OData `query` |
| Salesforce, Jira, OData, SAP HANA sources | `salesforce_ingestion`, `jira_ingestion`, `odata_ingestion`, `sap_hana_ingestion` | same shape as Dynamics: asset-per-source producing a DataFrame |
| Snowflake, Redshift, Databricks and Lakehouse sinks | `snowflake_copy_into_asset`, `dataframe_to_snowflake`, `dataframe_to_redshift`, `dataframe_to_databricks`, `dataframe_to_fabric_lakehouse` | consume an upstream DataFrame asset, so they pair with ask 7 |

Translated inline since the last revision: incremental filters (modified-time windows, file lists, prefixes), `MergeFiles`, partition discovery, the documented REST pagination rules, SQL upsert and auto-created tables, stored-procedure sources. Still stubs in the converter: Dynamics, Salesforce, Jira, SAP and other application connectors; Lakehouse, Warehouse and Snowflake sinks; Office 365 (Graph Data Connect); Azure File Storage locations; stored-procedure sinks.

## SSIS (`ExecuteSSISPackage`) and Synapse

Built through generated subclasses (ask 1): the packages, notebooks and Spark jobs are declared from the ADF activities, each is its own asset with run-order dependencies (`assets_by_package.deps`, `asset_overrides.depends_on`) and parameters (`package_overrides`), and the project loads and dry-runs with no live service. Not run against a catalog or workspace.

- `ssis_workspace` state needs a `project_id`; we write 0, which is fine unless `environment_reference` is used (it looks the id up).
- The Synapse findings are ask 4.
- **HDInsight Spark** (`HDInsightSpark`): `spark_livy_batch_asset` is the near fit (Livy batch, polls to completion, sends an `Authorization` header). ADF names the entry file, arguments and the cluster's Livy endpoint.
- **Databricks notebooks**: no ask. The converter runs the notebook as a one-off run through the Databricks SDK (`jobs.submit`, existing cluster or new cluster with a worker count or autoscale range). `dagster-databricks` has `DatabricksWorkspaceComponent` and `PipesDatabricksClient`, the alternative if the customer wants notebook runs modelled as Databricks workflows.

## Fabric (`fabric_workspace`, proven with a customer)

A literal Fabric notebook activity becomes a `fabric_workspace` asset (the notebook's id; the name is the activity's, because a pipeline holds only the id), and any Fabric notebook activity, including ones with expression parameters, can call `FabricWorkspaceResource.trigger_item_run` and `wait_for_run` inline. Both use your client, so the per-item-type parameter shapes are the ones already proved. Asks 1, 2, 3, 5 and 8 are the Fabric ones.

`InvokePipeline`, `RefreshDataflow` and Fabric's inline-dataset Copy (`datasetSettings` inside source and sink, Lakehouse and Warehouse sinks) are stubs because the only samples seen are Microsoft's templates, which do not show the ids. A real export from your customer's Fabric workspace would let the converter translate them against `fabric_workspace` (DataPipeline and Dataflow items) and `dataframe_to_fabric_lakehouse` without guessing. That is a request for a file, not a component change.

## Mapping Data Flow: registry components that overlap

A flow over SQL tables in one database now compiles to `warehouse_pipeline` steps (`docs/DATAFLOW.md`); every other flow runs inline (`dflow.py`), where the documented semantics are tested. The registry has a DataFrame component for nearly every transformation, which would give per-step assets and lineage. Read from their schemas, not run:

| ADF transformation | Registry component | Fit | Gap against ADF |
|---|---|---|---|
| filter | `Filter` (`condition` pandas query, `negate`) | good | expression language: ADF to pandas query |
| select | `Select Columns` (`columns`, `rename`, `drop_columns`) | good | |
| derive | `Formula` (pandas eval), `warehouse_formula` (SQL) | needs a translator | every ADF function would map to pandas or SQL |
| sort | `Sort` | partial | one `na_position` for all keys (ADF: a null flag per key) |
| aggregate | `Summarize` (`group_by`, `aggregations`) | good | aggregate expressions beyond a column |
| join | `DataFrame Join`, `warehouse_join` (inner, left, right, outer, cross; `on`, `left_on`, `right_on`) | equality only | ADF conditions are any expression |
| union | `DataFrame Union` (`by_name`, `by_position`) | good | |
| exists | none | gap | a semi or anti join; no component |
| lookup | `lookup` (`first`, `last`, `all`) | partial | no sort conditions for first and last, no `isMatch()` |
| conditional split | `router` (`exclusive`, default route) | good | |
| surrogate key | `surrogate_key` (`sequential`, `sequential_start`) | good | |
| rank | `Rank` (`method` dense and min, `group_by`) | partial | one sort column; ADF takes several |
| alter row | `alter_row` (ordered rules, default operation) | good | an `operation` column rather than ADF's per-sink policy handling |
| window | `window_calculation` (lag, lead, rank, dense_rank, row_number, cumulative, moving average) | partial | no `nTile` or `cumeDist`; ADF aggregates run over the whole partition |
| pivot | `pivot` (one `value_column`, one `agg_func`) | partial | ADF: several pivoted columns, listed values and `columnNaming` (`$V$N`, confirmed from a public flow) |
| unpivot | `unpivot` (`id_columns`, `value_columns`, `var_name`, `value_name`, `drop_null_values`) | good | matches ADF's `ungroupBy`, `pivotValues`, output names and `ignoreNullPivots` |
| flatten | `Array Exploder`, `Json Flatten`, `Nested Field Extractor` | partial | no unroll root; a null or empty array gives one row in ADF |
| a whole flow on Spark | `pyspark_pipeline` (multi-step `operations`, `steps`, `sinks`) | candidate backend | the operations list is a different vocabulary from the script |

## Nothing needed

- Mapping Data Flow: translated inline for the transformations above (see ask 10 and the component table below); a flow with parse, call, assert, `byName` or column patterns is a stub that names them, to be rewritten in SQL or Spark or kept in ADF.
- Storage activities (`Delete`, `GetMetadata`, `Validation`, file `Lookup`): translated inline over fsspec. A component would be neater (`adls_cleanup_job` and `adls_monitor` are job and sensor shaped, not step shaped) but nothing is blocked.
- Tumbling window dependency between windows: reported, not enforced. Dagster has no direct equivalent except a partition mapping or an automation condition; say if a component wraps it.

## Behavior to confirm in your own tests

- `AzureDataFactoryComponent` (the tier 1 observe-and-orchestrate path): run parameters, how failures and cancellations surface, whether its polling sensor handles tumbling windows.
