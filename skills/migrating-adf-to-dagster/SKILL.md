---
name: migrating-adf-to-dagster
description: Guide for migrating Azure Data Factory (ADF) pipelines, triggers, linked services and datasets to Dagster. Use when the user mentions migrating, porting, converting or moving off Azure Data Factory (or ADF pipelines, Synapse pipelines, Fabric Data Factory) to Dagster, wants to assess such a migration, or asks what an ADF construct (Copy, ForEach, Lookup, tumbling window trigger, data flow, SSIS package) maps to in Dagster. Load dagster-expert alongside this skill for Dagster specifics.
---

# Azure Data Factory to Dagster migration

Migrate honestly and incrementally: ADF stays authoritative until parity is shown, every source object gets an explicit disposition, and semantic differences are documented, never papered over.

A deterministic CLI enumerates and converts (so completeness does not depend on you); you handle the judgment calls the converter flags.

## Pick a tier first

| Tier | What it is | Use when |
|---|---|---|
| 1. Observe and orchestrate | The registry `AzureDataFactoryComponent` (`dagster-community-components`) imports ADF pipelines as assets that start a run and poll it, triggers as observable assets, plus a polling sensor. ADF still does the work. | You want ADF in Dagster's graph, alerting and run history now, with no change to ADF. This is the first phase; nothing to convert. |
| 2. Native | This skill's converter: pipelines become Dagster assets, triggers become schedules and partitions | You want Dagster to own execution and to retire the ADF pipeline |

A project often uses both: tier 1 for the whole factory, tier 2 pipeline by pipeline. Tier 1 is a `defs.yaml` (see `reference/mapping.md`); everything below is tier 2. The component's README marks pipelines and triggers "validated" and linked services, datasets, data flows and integration runtimes "untested"; none of it has been run by this skill.

## Hard rules

1. **Stubs must be loud, never quiet.** Every activity is translated, translated with a stated caveat, or a stub that fails with `ADF-CAVEAT: <reason>`. A stub keeps its place in the pipeline (dependencies, order), so the project loads and the gap shows in the report and in the run. A body that just returns success is never acceptable.
2. **No silent omissions.** Every manifest unit (pipeline, trigger, linked service, data flow) ends `complete` or `deferred (reason)`. `status summary` exits nonzero otherwise. Run it before claiming done.
3. **Never copy a secret.** The loader drops SecureString values, Key Vault values and credential-looking keys; generated code reads environment variables (`ADF_CONN_<LINKED_SERVICE>` holds a SQLAlchemy URL, `ADF_GLOBAL_<NAME>` a global parameter). If a secret appears in output, that is a bug in the loader: fix the class.
4. **Do not invent ADF semantics.** `reference/mapping.md` lists what is verified and what is not. Anything outside it gets checked against Microsoft's documentation or a real export first, and the finding is recorded.
5. **Fix classes, not instances.** If the same conversion pattern fails repeatedly, fix the converter, regenerate, re-validate. Do not hand-patch each unit.
6. **State losses plainly.** Timeouts, window dependencies, parallel branches and every other difference are listed in the report as behavior changes.
7. **Prefer registry components.** When the same gap recurs (Copy, SSIS, Synapse, storage steps), ask for a component (`reference/component-wishlist.md`) instead of growing generated code.

## Workflow

### Phase 0: Input

`adf-to-dagster convert|inventory <dir>` reads a folder holding either layout (mixed is fine):

- **Git-integrated factory**: `pipeline/`, `trigger/`, `dataset/`, `linkedService/`, `dataflow/`, one JSON per object. Preferred; needs no Azure access.
- **ARM export** (`ARMTemplateForFactory.json`): `[parameters('x')]` values are resolved from the template's own defaults; the rest are left as written, warned about, and any activity depending on one becomes a stub.

`docs/migration-intake.md` says how to ask the customer for it.

### Phase 1: Inventory (read-only)

```
adf-to-dagster inventory path/to/factory --out migration_manifest.json
```

One unit per pipeline, trigger, linked service and data flow, classified MECH (expected correct), JUDG (needs a person) or NONE (no automatic translation).

### Phase 1.5: Go/no-go

MIGRATION.md ends with a per-pipeline **Needs your input** checklist (kept in the manifest, so `report` reproduces it). Put those questions to the customer. Then review the JUDG and NONE units against what the team depends on (Mapping Data Flows, SSIS, Copy-heavy estates, event triggers, self-hosted integration runtimes). Record the outcome: migrate, migrate with conditions (the common case), or not yet with the gaps named. Copy decides this most often: check which of the estate's Copy pairs are in the translated list in `reference/mapping.md`.

### Phase 2: Convert

```
adf-to-dagster convert path/to/factory --out-dir out --pkg my_migrated
cd out && uv sync
```

One asset per pipeline, in `defs/adf.py`, over a copied `runtime.py`; a pipeline holding an SSIS, Synapse or SQL-copy activity is split into Python segments and registry-component assets (`defs/adf_*/defs.yaml`, `components.py`). Pipeline parameters are run config (defaults from ADF; ones with no default are blank and listed as questions). A tumbling window trigger makes the pipeline's asset time-partitioned, with the trigger's window passed in; schedule triggers become schedules that start **stopped**, so ADF and Dagster can run in parallel.

### Phase 3: Validate (gates)

```
adf-to-dagster validate out --execute --advance
```

- Gate 1: generated Python compiles. Gate 2: `dg list defs --json` loads. Gate 3: assets, jobs and schedule crons match the manifest.
- Gate 4 (`--execute`): runs every asset once with `ADF_DRY_RUN=1`: web calls and SQL are logged not sent, waits do not sleep, loops run once at most. A stub must fail with `ADF-CAVEAT`; a pipeline whose `Fail` activity always runs fails as in ADF and is counted separately; nothing else may fail.

A MECH unit passing means the structure matches and the dry run executed, not that behavior matches ADF.

### Phase 4: Resolve flagged units

For each pending JUDG or NONE unit: read its notes, port or fix it, re-run `validate`, then `status advance <manifest> U005 --evidence "..."` or `status defer <manifest> U009 --reason "..."`. Run `status summary` until it exits zero. Regenerate the report with `report`.

### Phase 5: Parallel run and cutover

Start the Dagster schedules while ADF's triggers are still on, compare outputs and run times for a full cycle (include a month end if any pipeline is monthly), then stop the ADF trigger one pipeline at a time. Nothing in this tool does the comparison.

## What is verified, and what is not

The converter has been run over 584 pipelines from 10 public repos (`reference/corpus-results.md`): no crashes, no unexpected dry-run failures, 412 clean dry runs. That finds converter bugs; it does not show behavior matches ADF. Behavior is checked a second way: every expression example, all 26 schedule recurrences, the pipeline-outcome rule, the Copy format and mapping defaults, the storage-filter and REST-pagination examples and tumbling-window anchoring in Microsoft's own documentation run as tests (`reference/mapping.md`), which is the only ground truth available without an Azure subscription.

See `reference/mapping.md`. In short: control flow, variables, expressions, SQL (Lookup, Script with bound parameters, stored procedure), web calls (managed identity included), Azure Function calls, Office 365 mail, Delete, GetMetadata, Validation and file Lookups are translated and covered by tests and dry runs; DatabricksNotebook runs through the Databricks SDK; SSIS packages, Synapse notebooks and Spark jobs, Fabric notebooks (also callable inline through the registry's Fabric client) and SQL-to-SQL Copy become registry-component assets when the pipeline can be split (`reference/mapping.md`, Splitting), which loads offline and is dry-run by the gates but has never run against a live service; Copy is translated for the common pairs (file copies and conversions between delimited text, JSON, Parquet and Excel; REST to file; files into SQL; SQL to files; SQL to SQL) and is a stub for application connectors and warehouse or Lakehouse sinks; Mapping Data Flows are translated in-process when their script uses only source, sink, select, filter, derive, sort, aggregate, join, union, exists, lookup, conditional split, surrogate key, rank, alter row, window, pivot, unpivot and flatten (parse, call, assert, column patterns and `byName` make the flow a stub), exact but not Spark-fast; HDInsight Spark activities are loud stubs (as are component-backed ones in a pipeline that cannot be split); event triggers are not generated. Nothing has run against a live Azure factory.
