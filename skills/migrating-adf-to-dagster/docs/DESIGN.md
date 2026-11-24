# ADF to Dagster: design

Status: first converter built (`src/adf_to_dagster`, 507 tests, four gates passing on a synthetic factory). Everything about ADF's JSON comes from Microsoft's public sample repo (see `tests/fixtures/README.md`) plus general knowledge of ADF; nothing has run against a live factory, and items not yet checked are listed at the end. Written for the people who will build and use this skill.

## 1. Goal

Same promise as the RunMyJobs skill (`../runmyjobs-to-dagster`): a deterministic CLI enumerates and converts every pipeline, trigger and linked service, so completeness does not depend on a person or a model; a skill handles the judgment calls the converter flags. Nothing is dropped silently, anything not translated is a loud stub (`ADF-CAVEAT: <reason>`), and behavior differences are written down.

## 2. Two tiers (the same shape as the Airflow skill)

1. **Observe and orchestrate.** The registry's `AzureDataFactoryComponent` already imports ADF pipelines as assets that start a run and poll it, imports triggers as observable assets, and has a polling sensor. ADF keeps doing the work. This is the first phase for any customer and needs no converter: a `defs.yaml` (example in `reference/mapping.md`). We did not build a converter mode for it. The component's README marks pipelines and triggers "validated" and the rest "untested"; we have not run it.
2. **Native.** This repo's converter rewrites the pipeline's logic as Dagster assets and retires the ADF pipeline.

Offer tier 1 across the factory and tier 2 pipeline by pipeline where it pays: control-flow-heavy pipelines, REST and SQL work, anything ADF bills heavily for. Pipelines dominated by Mapping Data Flows, SSIS or Copy stay in tier 1 until those have a translation.

## 3. Inputs

1. **Git-integrated factory (preferred).** One JSON file per object in `pipeline/`, `trigger/`, `dataset/`, `linkedService/`, `dataflow/` (shape seen in `tests/fixtures/native`). Easy to hand over, no Azure access needed.
2. **ARM export** (`ARMTemplateForFactory.json` plus its parameters file): `resources[]` of type `Microsoft.DataFactory/factories/<kind>` (shape seen in `tests/fixtures/arm`). Resource names are `[concat(parameters('factoryName'), '/Name')]` and some values are deploy-time `[parameters('x')]` strings; the loader must resolve those from the parameters file defaults or leave them as named unresolved inputs.
3. **Live read** through the Azure management API or `az datafactory` with a read-only role. Unverified; only needed if the customer cannot export.

Secrets: linked services can hold plaintext values (one fixture does, as a sample). The extractor must drop every `SecureString` value and every key it cannot prove is not a secret, and say so in the report. Generated projects reference environment variables, never values (same `{{ env.X or 'SET_X' }}` placeholder approach as the RunMyJobs SAP resource, so a project loads without them).

## 4. What real pipelines look like

`python -I tools/survey.py <dir>` run over Microsoft's sample repo: 151 pipelines.

- Activities (nested ones included): Copy 115, WebActivity 75, SetVariable 70, ExecutePipeline 57, ExecuteDataFlow 55, ForEach 40, IfCondition 37, Lookup 29, Delete 24, GetMetadata 14, Wait 13, Until 12, Fail 8, AppendVariable 7, SqlServerStoredProcedure 6, Filter 6, Script 6, then a tail of notebook, Spark and SSIS activities.
- 62 of 151 pipelines contain at least one activity with no translation (data flow, SSIS, Spark, notebook). About 41%; a customer estate heavy in Mapping Data Flows will look worse.
- Expression functions in use: `pipeline()`, `activity()`, `variables()`, `concat`, `item`, `equals`, `string`, `contains`, `replace`, `split`, `formatDateTime`, `utcnow`, `if`, `or`, `not`. A small expression translator covers these.
- Copy is dominated by file-to-file and REST-to-file pairs (Office365 to binary, binary to binary, REST to JSON, delimited text to delimited text, delimited text to Dynamics or Azure SQL).
- Only two triggers in the corpus, both tumbling window. The sample repo is templates, not estates, so trigger mix tells us nothing; ask for a real export.

This is a teaching corpus. Use it to rank work, not to promise coverage.

## 5. Mapping

Rating: **N** native (generated Python or core Dagster, no component needed), **C** registry component exists, **S** loud stub (tier 1, the registry component, still covers it).

| ADF | Dagster | Rating | Notes |
|---|---|---|---|
| Pipeline | One chain asset (or an asset per activity when branches run in parallel) | N | Same split rule as the RunMyJobs `TaskChainAssetComponent` decision |
| Pipeline parameters | Run config with defaults; unresolved ones listed in "Needs your input" | N | Re-use the RunMyJobs run-config design |
| Variables, SetVariable, AppendVariable | Locals in the generated function | N | Variables are pipeline-scoped in ADF |
| Activity dependency (Succeeded, Failed, Skipped, Completed) | Success path is plain order; Failed and Completed branches need try/except in a chain asset | N, partly | Verify the semantics of Skipped on a real factory before relying on it |
| IfCondition, Switch, Filter, Until, Wait, Fail | `if`, `match`, list comprehension, `while` with timeout, `time.sleep`, `raise` | N | |
| ForEach | `for`; `isSequential: false` with `batchCount` becomes a thread pool, large fan-outs a dynamic fan-out | N / C (`dynamic_fanout_asset`) | ForEach cap of 50 parallel in ADF is a platform limit, not copied |
| ExecutePipeline | Inline call, or a trigger asset when the child must run as its own run (`waitOnCompletion`) | N / C (`trigger_job_asset`) | |
| Lookup, Script, SqlServerStoredProcedure | SQL through a connection resource | C (`sql_asset`, `sql_server_stored_procedure_workspace` for on-prem SQL Server) / N | Lookup output size limits in ADF do not apply |
| WebActivity | HTTP request in generated code, auth from env | N / C (`http_webhook_job`, `rest_api_ingestion`) | Managed identity auth has no direct equivalent: ask |
| Copy | Common pairs only: blob or ADLS files and Azure SQL, REST to files | C (`adls_to_database_asset`, `rest_api_ingestion`, `dataframe_to_adls`, `file_ingestion`) / S | The first thing to check against a real estate. Every other pair is a stub that keeps its source and sink in the message |
| GetMetadata, Delete, Validation | Storage SDK calls | C (`adls_monitor`, `adls_cleanup_job`) / N | |
| DatabricksNotebook | One-off notebook run through the Databricks SDK (built); `dagster-databricks`' `DatabricksWorkspaceComponent` if workflows should carry the lineage | N (built) | Other Databricks activities are stubs |
| ExecuteDataFlow | Translated in-process when the script uses only source, sink, select, filter, derive, sort, aggregate, join, union, exists, lookup, conditional split, surrogate key, rank, alter row, window, pivot, unpivot and flatten; otherwise a loud stub naming what blocks it | M | `dflow.py`: a script parser, the data flow expression language and a row-by-row executor. Exact, not fast; no Spark. Keep big or unsupported flows in ADF under tier 1, or rewrite them in Spark or SQL |
| ExecuteWranglingDataflow, HDInsightSpark | Loud stub | S | Power Query and HDInsight have no mechanical translation |
| Schedule trigger | Cron schedule from the recurrence (frequency, interval, hours, minutes, weekDays, time zone) | C (`cron_schedule`) | Some recurrences need more than one cron: split and say so |
| Tumbling window trigger | Time-partitioned asset plus a schedule that requests the closed window | N, new | Needs the window variables (`@trigger().outputs.windowStartTime`, `windowEndTime`) as partition bounds. Window dependencies, delay, maxConcurrency and retry are reported first, enforced later |
| Storage and custom event triggers | Sensor on the same source | C (`adls_observation_sensor`, `servicebus_observation_sensor`) / S | |
| Linked service | Resource component, env-var placeholders | C (`azure_synapse`, `mssql_resource`, `databricks_resource`, `http_resource`, ...) | Key Vault references become env vars with a note |
| Dataset | Folded into the activity that uses it (path, format, schema) | N | Parameterised datasets resolve per call site |
| Global parameters | Environment variables or run config | N | |
| Integration runtime | Not translated; reported | | A self-hosted runtime means the pipeline reaches private networks: the Dagster code location needs the same network path. Always a question for the customer |

Expressions: translate `@pipeline().parameters.x`, `@variables('x')`, `@activity('a').output.y`, `@item()`, string interpolation `@{...}` and the functions in section 4 to Python. Any function or property path not in the table makes the activity a stub; never guess.

## 6. Pipeline of the tool (built)

Mirror RunMyJobs:

1. `extract`: git folder or ARM export to an intermediate YAML model (pipelines, activities with dependencies, triggers, resolved parameters, linked-service references with secrets removed).
2. `inventory`: manifest with one unit per pipeline, trigger and linked service, each rated MECH (expected correct), JUDG (needs a person) or NONE (stub). It does not yet recommend a tier per pipeline.
3. `convert`: Python output only so far (one module, `defs/adf.py`, over a copied `runtime.py`). Registry-component output (YAML) is not built.
4. `validate`, same gates: generated code compiles, `dg list defs --json` loads, assets and schedules match the manifest, and an `--execute` dry run with `ADF_DRY_RUN=1` in which no HTTP call, SQL statement, copy or notebook run is made and each stub fails with `ADF-CAVEAT`.
5. `report`: MIGRATION.md with caveats and a per-pipeline "Needs your input" list, kept in the manifest.

Code to take from `runmyjobs-to-dagster` unchanged in spirit: the manifest and status commands, the question and report machinery, the gate runner, the dry-run runtime, the run-config pattern. Do not abstract into a shared package until the second converter works; extract after, so the interface comes from two real users.

Skill name: `migrating-adf-to-dagster`, its own repo and SKILL.md, loading `dagster-expert` alongside. A skill per technology matches how people ask for it.

## 7. Needs your input (what the converter cannot read)

- Which pipelines matter and which are dead; owners and SLAs.
- Tumbling-window pipelines: what a window means for the business, whether windows depend on each other, whether past windows are re-run.
- Data flows: which ones, how big, and whether they can be rewritten in SQL or Spark.
- Integration runtimes: self-hosted ones and the networks they reach.
- Authentication: managed identity or service principal, and where Key Vault secrets will live.
- Triggers that are stopped in the export and whether they are meant to be.
- ADF cost per pipeline, if the driver for migrating is the bill.

## 8. Azure access and how we test

The code needs no Azure account: the converter reads files, and the dry-run gate calls nothing.

A live factory is useful for three things later: producing real exports for shapes the samples lack, confirming ADF's actual behavior (branch-on-failure, Skipped, retry timing, tumbling-window reruns) against what we generate, and trying the tier 1 component against a real run. For that, create a free subscription on a personal email (the corporate tenant shows "Welcome to Azure" because no subscription is attached to the account). A factory costs nothing idle; a handful of test runs cost cents. Do not load customer or Dagster data into it.

## 9. Build order

Done: loader for both layouts (ARM parameters resolved from defaults, secrets dropped), expression translator, control-flow and SQL and web activities, schedule and tumbling window triggers, manifest and gates 1-4, per-pipeline questions, report, skill and mapping reference.

Measured on 584 public pipelines, 412 dry-run clean (`reference/corpus-results.md`). Next, in order of how much it changes real output:
1. More Copy pairs (cross-format needs pyarrow or pandas; warehouse sinks), or a configurable Copy component (`component-wishlist.md` item 1). Four pairs are built.
2. (Built) SSIS, Synapse notebook and Spark job and SQL-to-SQL Copy as registry-component assets: a pipeline is split into Python segments and component assets, with the conditions in `mapping.md`. Remaining: run it against a live SSISDB, Synapse workspace and database; (the subclasses for SSIS, Synapse, Fabric and SQL copy are gone: the registry's declared items and `dry_run` replaced them).
3. Registry-component output (`SqlAssetComponent`, `CronScheduleComponent`) in place of generated Python for the pieces that have one, as the RunMyJobs converter does.
4. Event triggers as sensors.
5. Run a real export through it and fix classes of failure.

## 10. Not verified yet

- Anything against a live ADF run. Dependency semantics and the pipeline-level success rule are now checked against Microsoft's documentation (`mapping.md`, "Verified against Microsoft's documentation"); ForEach stop-on-failure, tumbling-window dependency and retry behavior are not.
- Real-export shapes beyond the samples: Switch with non-string values, nested ForEach, managed-VNet and private-endpoint settings, global parameters, the metadata-driven ingestion solution in the samples repo.
- The registry components named here (`AzureDataFactoryComponent`, `AzureSynapseComponent`, `SsisWorkspaceComponent`) against live services; we have read their READMEs and schemas only.
- Whether `az datafactory` or the REST API gives a read-only export good enough to skip the git or ARM route.
