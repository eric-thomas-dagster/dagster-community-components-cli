---
name: migrating-runmyjobs-to-dagster
description: Guide for migrating Redwood RunMyJobs (formerly SAP CPS / Redwood Server) process definitions, chains, schedules and queues to Dagster. Use when the user mentions migrating, porting or converting RunMyJobs or Redwood jobs to Dagster, wants to assess such a migration, or asks what a RunMyJobs construct maps to in Dagster. Load dagster-expert alongside this skill for Dagster specifics.
---

# RunMyJobs to Dagster migration

Migrate honestly and incrementally: RunMyJobs stays authoritative until parity is shown, every source object gets an explicit disposition, and semantic differences are documented, never papered over.

A deterministic CLI enumerates and converts (so completeness does not depend on you); you handle the judgment calls the converter flags.

## Hard rules

1. **Stubs must be loud, never quiet.** Every unit is translated, translated with a stated caveat, or a stub that fails with `RMJ-CAVEAT: <reason>`. A stub keeps its place in the chain (dependencies, schedule, pool), so the project loads and the gap shows up in the report and in the run. A body that just returns success is never acceptable. Stub units stay pending until ported or deferred with a reason.
2. **No silent omissions.** Every manifest unit ends `complete` or `deferred (reason)`. `status summary` exits nonzero otherwise. Run it before claiming done.
3. **Do not invent Redwood semantics.** `reference/mapping.md` lists what is verified and what is not. Anything outside it gets checked against the Redwood documentation (or a real export) before you rely on it, and the finding is recorded.
4. **Fix classes, not instances.** If the same conversion pattern fails repeatedly, fix the converter (or the mapping rule), regenerate, and re-validate. Do not hand-patch each unit.
5. **State losses plainly.** Preconditions, calendars and status handlers that are not enforced are listed in the report as behavior changes (a "degraded" caveat).
6. **Prefer registry components.** `--output components` writes `dagster-community-components` YAML (`ShellCommandAssetComponent`, `SqlAssetComponent`, `TaskChainAssetComponent`, `EmailSendAssetComponent`, `SapJobTriggerAssetComponent`, `CronScheduleComponent`; needs registry 0.10.177 or later). Generated Python is for what no component covers (stubs and the in-chain dbt Cloud trigger). When the same gap recurs, ask for a component (`reference/component-wishlist.md`) instead of growing generated code.

## Workflow

### Phase 0: Input

The tool consumes an intermediate YAML model. For a real export, extract it from the CAR (a directory or a `.car` zip):

```
runmyjobs-to-dagster extract path/to/car --out model.yaml
```

The extractor reads process definitions today (verified against a real sample). Chains are parsed and checked against three real exports (tests/fixtures/car_chains, car_plan_to_produce, car_isu): steps and calls in order, call parameters (literals and references to the caller's parameters), wait events, call and step time-window preconditions, status handlers (restart, mark completed, raise error), chain parameters, event definitions, and chains embedded inside a call (`System_Internal_Chain<n>` wrappers, read from direct children so their steps are not mixed into the parent). A call to another chain is inlined as assets named after the call. Queue limits and submit frames (bound to targets through job groups) are still parsed from the data model reference only (unverified XML shape; the extractor warns). Calls to RunMyJobs built-ins are mapped without a definition in the export: `System_DoNothing` is a no-op step, `System_Sleep` a real wait of its `Milliseconds`, `System_Mail_Send` a real SMTP step (`run_mail`, `SMTP_*` env vars; attachments are not sent) and `SAP_*` jobs loud stubs that keep their parameters; any other missing definition is a stub saying to export it. Status handlers: restart becomes a retry, mark completed logs the failure and continues (shell and Python steps), raise error is the normal failure with its message in the note, go-to-step is reported. Time windows and events are not parsed yet (file events can be written by hand in the model under `file_events:` with `target`, `directory`, `pattern`, `poll_seconds`, `min_size_bytes`, `min_age_seconds`, `stable`; the converter turns each into a `FilesystemMonitorSensorComponent` that starts stopped, plus its own job in `defs/jobs.py`); anything skipped is named in a warning. Schedules may be absent from a CAR export, so the extractor warns when none are found. Given only a job listing (HTML report), run `runmyjobs-to-dagster inventory` to size and triage it; `docs/first-week-asks.md` lists what to request next. The converter recognises steps that call `JD_dbt_Cloud_Job_Trigger` (dbt Cloud) or an `HVR`/`LDP` refresh definition and notes them as heuristic. For definitions without a CAR export use `fetch-ext` then `extract-json` (Inbound REST Extension, API key in `RMJ_API_KEY`; exact JSON shapes from its swagger, but no schedule or time-window endpoints; user setup steps are in `docs/rmj-api-setup.md`). `runmyjobs-to-dagster fetch <base-url> --user U` (password in `RMJ_PASSWORD`) pulls objects from an on-premises built-in REST interface into a directory that `extract` reads; it is untested against a live server and unavailable on SaaS. If a skipped object matters, get a sample of it and extend `extract_car.py` against that sample (never guess the XML). `runmyjobs-to-dagster sample` writes a hand-written example model.

If the customer's RunMyJobs MCP server is connected (and no CAR export is available), follow `reference/mcp-extraction.md`: read-only tools only, raw responses saved for audit, hypotheses about queues, chains and schedules checked and recorded, and the result checked with `runmyjobs-to-dagster validate-model`.

### Phase 1: Inventory (read-only)

```
runmyjobs-to-dagster inventory model.yaml --out migration_manifest.json
```

Each unit lists its source object, the Dagster artifact it will become, and a classification: MECH (converter output expected correct), JUDG (needs a person or you to confirm or fix), NONE (no automatic translation).

### Phase 1.5: Go/no-go

MIGRATION.md ends with a per-chain **Needs your input** checklist (also kept in the manifest, so `report` reproduces it). Put those questions to the customer; the answers (partition keys, owners, event producers, window hours, connection details) are what the converter cannot read from an export.

Before generating anything, review the JUDG and NONE units against what this team depends on (precondition-gated steps, named calendars, event-driven chains, RedwoodScript volume, connector job types such as SAP). Record the outcome in the report: migrate, migrate with conditions (the common case), or do not migrate yet with the specific unmitigated gaps named.

### Phase 2: Convert

```
runmyjobs-to-dagster convert model.yaml --out-dir out --pkg my_migrated --output components   # or --output python
cd out && uv sync
```

`--output components` (recommended) writes one `defs.yaml` per chain or application with a registry component for each call and each schedule; a linear chain of shell, SQL and Python steps whose queue, retries and time window agree becomes ONE `TaskChainAssetComponent` (one asset, stops at the first failed step; per-step timeouts are summed into one chain timeout; `--chains assets` turns this off). Any chain with parallel calls, mixed retries or queues, or a dbt Cloud, HVR or stub step keeps one asset per call, and MIGRATION.md says why. Shell steps take `success_exit_codes` (from the return-code mapping), and a `run_window` (hand-written in the model, `HH:MM-HH:MM tz days`) skips instead of failing outside the window. SQL steps read a SQLAlchemy URL from `RMJ_CONN_<NAME>` through the component's own `connection_env` (no generated resource). SAP_AbapRun and SAP_BW_ProcessChain become `SapJobTriggerAssetComponent` (with a generated `SapRFCResourceComponent`; never run against a live SAP system), and System_Mail_Send with a fixed recipient becomes `EmailSendAssetComponent` over `SMTPResourceComponent` (a recipient supplied at run time stays a generated step that takes it as run config). dbt Cloud jobs are one asset each by default (the project's per-model assets are switched off); `--dbt-model-jobs 7,12` (or `"models": true` on a job in the job list) keeps per-model lineage, checks and events for those jobs, as a second component instance. A dbt Cloud step becomes the component's mirrored executable job asset when `convert --dbt-jobs dbt_jobs.json` (from `fetch-dbt-jobs`) has its job id; otherwise it is a generated Python trigger. Python output is one generated module; both outputs are dry-run by gate 4.

This writes a Dagster project, `migration_manifest.json` and a first `MIGRATION.md`. The report's pool section lists the `dagster instance concurrency set` commands for the queue limits; apply them (and enable run monitoring so cancelled runs free their slots) before running anything, because a pool has no limit until one is set.

### Phase 3: Validate (gates)

```
runmyjobs-to-dagster validate out --advance
```

- Gate 1: generated Python compiles.
- Gate 2: `dg list defs --json` loads the definitions.
- Gate 3: loaded assets, dependencies, jobs and schedule crons match the manifest.

- Gate 4 (opt-in, `--execute`): runs every asset once with `RMJ_DRY_RUN=1`. Commands and SQL are rendered and logged, not executed; scripts are compiled; Dagster exercises dependencies, pools and retries. A stub must fail with `RMJ-CAVEAT` and nothing else may fail. Both outputs support it: the python runtime honors the flag, and the components output points at `src/<pkg>/components.py`, whose `Rmj*` classes subclass the registry shell and SQL components and add `dry_run` (the flag or the environment variable). A project whose YAML uses the plain registry shell or SQL component would run the real commands, so gate 4 refuses it.

`--advance` marks passing MECH units complete (with `--execute`, only those whose asset also ran). A MECH unit passing means the structure matches the source model and the step rendered and ran without error, not that its behavior matches RunMyJobs.

### Phase 4: Resolve flagged units

For each pending JUDG or NONE unit: read its notes, fix or port it (RedwoodScript to Python, preconditions to schedule windows, sensors or skip logic, status handlers to retries or run-failure sensors), re-run `validate`, then:

```
runmyjobs-to-dagster status advance migration_manifest.json U005 --evidence "ported; validate passes; <what you checked>"
runmyjobs-to-dagster status defer   migration_manifest.json U009 --reason "SAP connector job; needs a separate integration"
```

If a gate fails, retry with the validator output in context (cap around ten attempts, then defer with the failure class).

### Phase 5: Side by side, then cutover

Run Dagster alongside RunMyJobs on the same logical window. Compare run results and timing (row counts, file outputs, durations). The `runmyjobs_integration` component's reconciliation job is a starting point for drift checks. Cut over per application or chain: pause the RunMyJobs submit frame, enable the Dagster schedule; rollback is the reverse.

### Phase 6: Report

`status summary` must pass. Then:

```
runmyjobs-to-dagster report migration_manifest.json --out MIGRATION.md
```

The report states: the go/no-go outcome, every disposition, schedule equivalence (source frame vs. cron and time zone), what was deferred and why, and the behaviors that changed.

## Two routes: convert (default) and rewrite

| | `convert` (default) | `rewrite` (opt-in, per unit) |
|---|---|---|
| What it does | transforms the extracted model one object at a time, deterministically | analyzes the unit into a facts-only spec, generates a prompt, you write the Dagster project from scratch (with `dagster-expert` and `dagster-community-components`), gates accept or reject it |
| Use it for | everything it translates (MECH units), and as the first pass over the whole estate | units the converter **stubs or degrades** (RedwoodScript, a "last day of month" frame, a precondition you can now enforce), redesign-heavy chains, pilots where idiomatic output matters |
| Cost | none per unit (no model) | model effort per unit: read a spec and prompt (about 4,000 to 6,000 tokens of input for the small units measured, an estimate; output and iterations were not measured), search the registry, write, iterate on the gates; about 30 s per `accept` run. Details and what was not measured: `reference/rewrite-route.md` |
| Fallback | | the converter's output stays the reference draft and the fallback; a rewrite that fails a gate stays deferred |

Both routes share the manifest and the completeness rule: `route: converter | rewrite` is recorded per unit, with the rewrite's gate result. Nothing is complete without its gates. If the skill's tiers (inventory, convert, validate) are in your head: they are the same steps for both routes; only Phase 2 differs.

```
runmyjobs-to-dagster convert model.yaml --out-dir out --pkg my_migrated --output components   # the default route, and the reference draft
runmyjobs-to-dagster analyze model.yaml --out-dir rewrite --manifest out/migration_manifest.json   # <unit>.spec.md/.json + ESTATE.md, deterministic
runmyjobs-to-dagster prompt rewrite --project my_rewrite --package my_rewrite --converter-dir out   # <unit>.prompt.md + ESTATE.prompt.md
# ... load dagster-expert and dagster-community-components, follow rewrite/<unit>.prompt.md, write src/my_rewrite/defs/<unit>/ ...
runmyjobs-to-dagster accept rewrite/<unit>.spec.json --project my_rewrite --manifest out/migration_manifest.json   # the gates; exit 0 only when all pass
runmyjobs-to-dagster equivalence rewrite/<unit>.spec.json --converter-project out --rewrite-project my_rewrite   # behavior comparison of the two routes
```

The spec holds only facts (inferred statements are marked *inferred*): every call with its type, resolved parameters and the command or SQL verbatim, parameters and defaults, the order graph, schedules (with the cron the converter derives and the elements it cannot), queues and what is shared, preconditions, status handlers, wait events, notification targets, connection names (never values), time zones, what must be preserved, what differences are acceptable, and what the converter does with the unit today. `ESTATE.md` relates the units (chains calling chains, shared queues and definitions, events) so asset boundaries can be chosen across units. The gates are in `reference/rewrite-route.md`; the loud-stub rule applies: a rewrite that still contains an `RMJ-CAVEAT` stub fails the gate and the unit stays deferred.

## Scripts and commands

| Command | Purpose |
|---|---|
| `inventory` | Enumerate source objects into a manifest (read-only) |
| `convert` | Generate the Dagster project and manifest |
| `validate [--advance] [--execute]` | Gates 1-3 (and the dry-run gate 4) and manifest comparison |
| `status summary / advance / defer / reopen` | Per-unit state; summary is the completeness gate |
| `report` | Render MIGRATION.md from the manifest |
| `analyze` | Rewrite route: spec per unit and the estate view (read-only) |
| `prompt` | Rewrite route: the prompt per unit and for the estate |
| `accept` | Rewrite route: the acceptance gates; records `route: rewrite` and the gate result in the manifest |
| `equivalence` | Compare the converter's and the rewrite's behavior per unit |

See `reference/component-wishlist.md` for the components the converter would use if they existed, and `reference/mapping.md` for the object mapping and its verification status, and `reference/dagster-targets.md` for what each Dagster target (pools, schedules, sensors, run-status sensors, declarative automation, freshness policies) does and how to configure it.
