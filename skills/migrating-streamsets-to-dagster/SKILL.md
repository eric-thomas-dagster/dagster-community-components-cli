---
name: migrating-streamsets-to-dagster
description: Guide for migrating StreamSets Data Collector (and Control Hub) pipelines to Dagster. Use when the user mentions migrating, porting, converting or moving off StreamSets (Data Collector, Transformer, IBM StreamSets, Control Hub jobs) to Dagster, wants to assess such a migration, or asks what a StreamSets construct (origin, processor, Stream Selector, Expression Evaluator, event lane, offset, CDC origin, job) maps to in Dagster. Load dagster-expert alongside this skill for Dagster specifics.
---

# StreamSets to Dagster migration

Migrate honestly and incrementally: StreamSets stays authoritative until parity is shown, every source object gets an explicit disposition, and semantic differences are documented, never papered over.

A deterministic CLI enumerates and converts (so completeness does not depend on you); you handle the judgment calls the converter flags.

## The fact that shapes everything

StreamSets pipelines are *record streams*: a pipeline is usually a continuously running job, and its origins resume from stored offsets. A Dagster run is a *batch*. So a pipeline is one of:

| Pipeline shape | Dagster |
|---|---|
| Batch ingest/ETL (S3 or files, JDBC tables, Salesforce query) that ends with `no-more-data` | Tier 2: an asset that runs the stage graph once per materialization, on a schedule or sensor |
| Continuous (CDC, Kafka, HTTP server, directory spooling, file tail) | Tier 1: keep it running in StreamSets and observe it. Or redesign as micro-batch (a decision for the customer, not the converter) |

## Pick a tier first

| Tier | What it is | Use when |
|---|---|---|
| 1. Observe and orchestrate | The registry `StreamSetsWorkspaceComponent` (`dagster-community-components`) wraps Control Hub jobs as assets: `action: start` ensures the job is running, a sensor observes status changes, an asset check asserts it is `ACTIVE`. StreamSets still does the work. | You want StreamSets jobs in Dagster's graph and alerting now, with nothing converted. First phase for every estate, and the end state for continuous pipelines. Jobs are declared by id (`jobs:`), because no list-jobs call is verified. |
| 1b. Engine direct (no Control Hub) | `convert --engine-tier1 noop|start` also writes `defs/streamsets_engine/defs.yaml`: the registry `StreamSetsEngineComponent` (0.10.187) with one asset per Data Collector pipeline (`pipeline_id` from the export), a status sensor and a running check. `noop` observes, `start` makes materializing start the pipeline. Fill `STREAMSETS_ENGINE_URL` / `_USERNAME` / `_PASSWORD`. Data Collector only (not Transformer). | The engine runs with no Control Hub in front of it. Loading and asset listing are tested in the component's demo mode; start/stop against a live engine is not run by this skill's tests. |
| 2. Native | This skill's converter: batch pipelines become Dagster assets | You want Dagster to own execution and to retire the StreamSets pipeline |

When a Data Collector pipeline's only destination is an HTTP Client or Kafka Producer with static settings, tier 2 emits **two assets**: the converted record graph (returns a DataFrame) and the registry `DataframeToHttpComponent` / `DataframeToKafkaComponent` that sends it (own retries, batching, auth). Gate 4 does not run that sink asset in dry mode (it would really send); `sink_components.json` lists it.

`convert --dataframe-route` (opt-in) emits one registry `polars_pipeline` for linear pipelines whose every stage it can express (see the wishlist, section 1b); anything else stays a record graph. It is not compared with a real Data Collector.

## Hard rules

1. **Stubs must be loud, never quiet.** Every stage is translated, translated with a stated caveat, or a stub. A stub keeps its place in the graph, and a pipeline that holds one **fails before it reads any data** with `SDC-CAVEAT: <stage>: <reason>`. A body that succeeds while skipping a stage is never acceptable.
2. **No silent omissions.** Every manifest unit (each pipeline, and each untranslated stage) ends `complete` or `deferred (reason)`. `status summary` exits nonzero otherwise. Run it before claiming done.
3. **Never copy a secret.** The loader drops credential-named runtime parameters and literal credential values in stage properties (an export's `constants` often hold real passwords). Generated code reads `SDC_SECRET_<NAME>`. If a secret appears in output, that is a loader bug: fix the class.
4. **Do not invent StreamSets semantics.** `reference/mapping.md` lists what is verified and what is assumed. Anything outside it is checked against IBM's documentation or a real export first, and the finding is recorded.
5. **Fix classes, not instances.** If the same pattern fails repeatedly, fix the converter, regenerate, re-validate.
6. **Hand-written code is labelled.** A stage replaced with `--override` is a REVIEW unit, not a translation. Write it with a test; the original script or stage config stays in the manifest.
6a. **State losses plainly.** Offsets, error destinations, event handlers, batch sizes and execution mode are listed as behavior changes in the report.
7. **Prefer registry components.** When a stage recurs and the registry has a component for it (Salesforce, Kafka, Delta, Sling replication, S3 to database), use it instead of growing generated code (`reference/mapping.md`, `reference/component-wishlist.md`).

## Workflow

### Phase 0: Input

`streamsets-to-dagster convert|inventory <path>` reads Data Collector or Control Hub pipeline JSON: a file, a folder, or a `.zip` (Control Hub's export archive). `docs/migration-intake.md` says how to ask the customer for it. The export contains the full stage configuration, including any credentials in runtime parameters: ask the customer to send it through their approved channel, and know that the loader drops them.

### Phase 1: Inventory (read-only)

```
streamsets-to-dagster inventory path/to/export --out migration_manifest.json
```

One unit per pipeline and one per untranslated stage, classified MECH (expected correct), JUDG (translated with caveats, a person confirms) or NONE (no automatic translation).

### Phase 1.5: Go/no-go

MIGRATION.md ends with a per-pipeline **Needs your input** checklist (kept in the manifest, so `report` reproduces it). Put those questions to the customer, above all: which pipelines are continuous, what triggers each batch pipeline, which stages are retired. Then review the NONE units against what the team depends on (CDC, scripting stages, Hadoop/Hive). Record the outcome: migrate, migrate with conditions (the common case), or not yet with the gaps named.

### Phase 2: Convert

```
streamsets-to-dagster convert path/to/export --out-dir out --pkg my_migrated
cd out && uv sync
```

Add `--override "PIPELINE/STAGE=file.py"` (repeatable) for stages you rewrote by hand (`reference/mapping.md`). Generated: `src/<pkg>/defs/streamsets.py` (one asset per pipeline), `runtime.py`, `MIGRATION.md`, `migration_manifest.json`. Nothing is scheduled: StreamSets pipelines carry no schedule.

### Phase 3: Validate

```
streamsets-to-dagster validate out --advance --execute
```

Gate 1: generated Python compiles. Gate 2: definitions load. Gate 3: assets match the manifest. Gate 4 (`--execute`): every asset runs once with `SDC_DRY_RUN=1` (reads and writes are logged, not performed); stubbed pipelines must fail with `SDC-CAVEAT` and nothing else may fail. Dry run proves wiring, not data.

### Phase 4: Resolve units, then prove parity

For each JUDG and NONE unit: fix it, or `status defer <unit> --reason "..."`. Then run both systems on the same input and compare destination rows before cutover; the converter has never seen the customer's data. Offsets differ between the systems (see the report), so compare on a bounded input.

```
streamsets-to-dagster status summary out/migration_manifest.json
```

## Do not

- Do not say a pipeline is migrated because the project loads. Gates 1-4 prove structure; only a parallel run proves behavior.
- JavaScript evaluators are transpiled to Python automatically (`scripts/<stage>.py`); read the generated module before trusting it. Do not translate a Groovy evaluator by guessing: it runs on a JVM, or you read the script (in the manifest notes), rewrite it in Python with a test, and pass it with `--override`. A Jython evaluator runs as written under CPython: still read it.
- - Do not assume encrypted data cannot be read after the move: Encrypt and Decrypt Fields ciphertext is compatible in both directions (user-supplied keys checked against Data Collector 6.4.1; AWS KMS not exercised). Get the key (the export drops it) and say which date or time fields are encrypted: those are not supported yet.
- Do not run a continuous pipeline as a scheduled batch without the customer's decision on lag and ordering.

## Reference

- `reference/mapping.md`: stage and EL catalog, verified versus assumed.
- `reference/corpus-results.md`: what the converter does to the public sample pipelines.
- `reference/component-wishlist.md`: registry components that would remove generated code.
- `docs/migration-intake.md`: what to ask the customer for.
