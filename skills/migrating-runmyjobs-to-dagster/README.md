# runmyjobs-to-dagster

Convert Redwood RunMyJobs definitions into a Dagster project, with a migration report that says what converted, what needs review and what has no equivalent.

**Status: early spike.** It works on an intermediate YAML model (see `src/runmyjobs_to_dagster/samples/sample_model.yaml`). `extract` reads process definitions from a real CAR export, and chains, queues, submit frames/job groups from the data model reference (unverified XML shape, flagged by warnings); time windows and events are not parsed yet. `inventory` sizes and triages a job-listing report (name/description only). Steps that call the dbt Cloud trigger or an HVR refresh definition are recognised by name (heuristic) and flagged for review. `fetch-ext` + `extract-json` read definitions (jobs, chains, queues, events) from the Inbound REST Extension using its published swagger (untested live; setup in `docs/rmj-api-setup.md`). `fetch` pulls objects from an on-premises REST interface (untested against a live server); see `docs/DESIGN.md`.

```bash
uv run runmyjobs-to-dagster extract path/to/car --out model.yaml   # from a real export
uv run runmyjobs-to-dagster sample my_model.yaml
uv run runmyjobs-to-dagster convert my_model.yaml --out-dir out --pkg my_migrated --output components   # registry components (or --output python); --chains assets: one asset per call instead of one asset per linear chain
cd out && uv sync && uv run dg check defs
uv run runmyjobs-to-dagster fetch-dbt-jobs <account id> --out dbt_jobs.json   # needs DBT_CLOUD_TOKEN; then add --dbt-jobs dbt_jobs.json to convert
uv run runmyjobs-to-dagster validate out --execute   # gates 1-3, plus a dry run of every asset (commands and SQL rendered, not executed)
```

## Mapping

| RunMyJobs | Dagster |
|---|---|
| Process definition (OS) | `ShellCommandAssetComponent` (or an asset running the command) |
| Process definition (SQL) | `SqlAssetComponent` with `connection_env` (or an asset running the SQL via SQLAlchemy) |
| SAP_AbapRun, SAP_BW_ProcessChain | `SapJobTriggerAssetComponent` (components output; python output keeps a loud stub) |
| System_Mail_Send | `EmailSendAssetComponent` (fixed recipient) or a generated SMTP step |
| Process definition (RedwoodScript) | Loud stub asset (fails with `RMJ-CAVEAT`), source preserved; flagged for review |
| Chain, steps and calls | Assets with `deps` between steps (calls in a step are parallel) and one job per chain |
| Submit frame | `CronScheduleComponent` per element, with time zone and `end_date` (python output: `ScheduleDefinition`, end date listed as a caveat) |
| Queue | Pool on each asset |
| Chain parameter with no value in the export | python output: run config field on the asset (blank keeps the default); components output: noted only |
| Maximum Starts and restart handlers | `RetryPolicy` |
| Nested chain call | Loud stub in its place (order kept) |
| Preconditions, other status handlers | Flagged in `MIGRATION.md` as caveats |

`MIGRATION.md` ends with a **Needs your input** checklist per chain: the things an export cannot hold (business keys for partitions, owners, who raises an event, time-window hours, connection details, what the caller passes for a parameter), derived from what the chain contains.

Output is a normal Dagster project: `src/<pkg>/defs/runmyjobs.py`, `MIGRATION.md` and `migration_manifest.json`.

The workflow for an agent is in `SKILL.md`: `inventory` → `convert` → `validate --advance` → resolve flagged units → `status summary` (must pass) → `report`.

Every source object is either converted, converted with a stated caveat, or a stub that fails loudly with `RMJ-CAVEAT`; nothing is dropped silently. `reference/component-wishlist.md` lists what the community components could still do for the converter.
