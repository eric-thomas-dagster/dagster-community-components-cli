# Corpus results: public ADF repos through the converter

`tools/corpus.py <dir>` loads each ARM template (or git-integration folder) as its own factory, converts it, writes the project, imports the generated module and dry-runs every pipeline in-process with `ADF_DRY_RUN=1`. It exists to find converter bugs and to rank what to translate next. It does **not** measure whether converted pipelines behave like ADF: a dry run proves the code compiles, loads and wires up, with nothing sent anywhere.

Run on (cloned for local analysis, not redistributed; only Microsoft's samples are copied into `tests/fixtures`, with their MIT licence):

| Source | Pipelines | Converted | Review | Unmapped | Dry-run clean | Stub | `Fail` activity | Unexpected |
|---|---|---|---|---|---|---|---|---|
| Azure/Azure-DataFactory (Microsoft samples, 105 templates) | 164 | 26 | 74 | 64 | 52 | 103 | 5 | 0 |
| mrpaulandrew/procfwk (a metadata-driven framework, git and ARM, two versions) | 275 | 256 | 19 | 0 | 275 | 0 | 0 | 0 |
| CloudFormations/CF.Cumulus | 37 | 17 | 20 | 0 | 16 | 1 | 19 | 0 |
| ChristianCC1993/Azure-Data-Factory | 44 | 14 | 26 | 4 | 39 | 4 | 1 | 0 |
| thamizzz11/ADF (35 pipelines, mostly one data flow each) | 35 | 4 | 24 | 7 | 28 | 7 | 0 | 0 |
| vmusikara/adf-demo, subark/ADF_reporting, rebremer/blog-datapipeline-cicd, Smars-Bin-Hu/...-EDA, Bertverbeek4PS/bc2adls | 29 | 10 | 18 | 1 | 22 | 2 | 4 | 0 |
| **Total** | **584** | **327** | **181** | **76** | **433** | **117** | **29** | **0** |

"Converted" means no stub and no stated difference (a few pipelines moved from converted to review when the documentation checks added honest notes about, for example, ignored `typeConversion` and `escapeChar` settings); "review" means translated with a caveat or one stub; "unmapped" means every activity is a stub. Read it with care: procfwk is 275 of the 584 and is one framework with many near-identical pipelines, and these are public examples and frameworks, not a customer estate.

## What it found (all fixed, each with a test)

Bugs a real repo exposed that Microsoft's samples did not: a pipeline named `01-Grandparent` generated an invalid class name; dataset `schema` and `table` can be expressions; ADF parameter names are not case-sensitive (`executionId` declared, `ExecutionId` used); raw strings starting with `@` are expressions even without the `{"type": "Expression"}` wrapper (this was a correctness bug, not just a crash); and a dry run needs to survive values that only exist on a real run (empty dates, `int("<dry>")`, division, `json("")`, an empty list).

## What the stubs are, in order

1. Mapping Data Flows (`ExecuteDataFlow`): translated when the script uses only source, sink, select, filter, derive, sort, aggregate, join, union, exists, lookup, conditional split, surrogate key, rank, alter row, window, pivot, unpivot and flatten, with datasets or inline datasets over SQL and files. 28 of the 76 flows that carry a script have only those transformations; the rest stay stubs for parse, call, assert, `byName`, column patterns, Synapse lake-database and other inline stores, and `wildcardPaths`. Against the previous measurement, converted fell from 332 to 327 and review rose from 155 to 177: mostly data flow pipelines moving from unmapped to review, and five pipelines that gained a documented-difference note from the Copy, SQL and storage changes (not itemised).
2. Fabric: notebooks and Office 365 mail are translated (the notebook through the registry's `fabric_workspace` client or component). `PBISemanticModelRefresh` becomes a `SemanticModelRefreshAssetComponent` asset with the workspace and model ids left as environment variables to set (4 pipelines moved from unmapped to review). Still stubs: `RefreshDataflow` and `InvokePipeline` (no real export shows their ids), the Fabric function activity, and Fabric's inline-dataset Copy with Lakehouse and Warehouse sinks (8 in the corpus).
3. Copy: Dynamics (11), Salesforce, Jira, SAP and Office 365 connectors; Lakehouse and Warehouse sinks; `AzureFileStorage` locations; REST with pagination outside the documented vocabulary, or other authentication. Parquet, cross-format, SQL-family and nested-in-a-loop copies are translated now.
4. Pipelines whose linked services are not in the export (bc2adls ships datasets without them).
5. A Databricks linked service with a parameterised cluster size (`@linkedService().NumWorkers`).

Re-run it after any converter change: `uv run python tools/corpus.py <dir>`; the aim is zero crashes and zero unexpected dry-run failures, and a shrinking stub list.
