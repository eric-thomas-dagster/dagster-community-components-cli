# runmyjobs-to-dagster

Convert Redwood RunMyJobs definitions into a Dagster project, with a migration report that says what converted, what needs review and what has no equivalent.

**Status: early spike.** It works on an intermediate YAML model (see `src/runmyjobs_to_dagster/samples/sample_model.yaml`). `extract` reads process definitions from a real CAR export, and chains, queues, submit frames/job groups from the data model reference (unverified XML shape, flagged by warnings); time windows and events are not parsed yet. `inventory-html` sizes and triages a job-listing report (`inventory` turns a model YAML into a manifest) (name/description only). Steps that call the dbt Cloud trigger or an HVR refresh definition are recognised by name (heuristic) and flagged for review. `fetch-ext` + `extract-json` read definitions (jobs, chains, queues, events) from the Inbound REST Extension using its published swagger (untested live; setup in `docs/rmj-api-setup.md`). `fetch` pulls objects from an on-premises REST interface (untested against a live server); see `docs/DESIGN.md`.

```bash
uv run runmyjobs-to-dagster extract path/to/car --out model.yaml   # from a real export
uv run runmyjobs-to-dagster sample my_model.yaml
uv run runmyjobs-to-dagster convert my_model.yaml --out-dir out --pkg my_migrated --output components   # registry components (or --output python); --chains assets: one asset per call instead of one asset per linear chain
cd out && uv sync && uv run dg check defs
uv run runmyjobs-to-dagster fetch-dbt-jobs <account id> --out dbt_jobs.json   # needs DBT_CLOUD_TOKEN; then add --dbt-jobs dbt_jobs.json to convert
uv run runmyjobs-to-dagster validate out --execute   # gates 1-3, plus a dry run of every asset (commands and SQL rendered, not executed)
```

## Two routes

`convert` (above) is the default and stays as it is. For units it stubs or degrades, redesign-heavy chains and pilots there is a second, opt-in route: analyze each unit into a facts-only spec, generate a prompt, have Claude (with the `dagster-expert` and `dagster-community-components` skills) write the Dagster project from scratch, and accept it only if the gates pass. The converter's output stays the reference draft and the fallback.

```bash
uv run runmyjobs-to-dagster analyze my_model.yaml --out-dir rewrite --manifest out/migration_manifest.json   # <unit>.spec.md/.json, ESTATE.md, estate.json
uv run runmyjobs-to-dagster prompt rewrite --project my_rewrite --package my_rewrite --converter-dir out     # <unit>.prompt.md, ESTATE.prompt.md
# Claude follows rewrite/<unit>.prompt.md and writes src/my_rewrite/defs/<unit>/
uv run runmyjobs-to-dagster accept rewrite/<unit>.spec.json --project my_rewrite --manifest out/migration_manifest.json
uv run runmyjobs-to-dagster equivalence rewrite/<unit>.spec.json --converter-project out --rewrite-project my_rewrite
```

`accept` needs an environment with dagster, `dg` and `dagster-community-components` (`--python` points at it; the dev extra installs them). Gates: definitions load (`dg check defs`), matches the spec (order, schedules, pools, retries), commands preserved, every RunMyJobs object accounted for in the header docstring, a generated pytest (materializes what has no external system), structural checks where derivable, no loud stub left, no secrets, no invented APIs. Results and measured evidence: `reference/rewrite-route.md`; worked before/after: `examples/rewrite/`.

## Layout

- `src/runmyjobs_to_dagster/`: the converter (`convert.py`, `extract_*.py`, `validate.py`, `manifest.py`, `cli.py`) and `rewrite/` (`spec.py` analyze, `prompt.py`, `accept.py` gates, `equivalence.py`, `introspect.py`)
- `reference/`: `mapping.md`, `dagster-targets.md`, `component-wishlist.md`, `rewrite-route.md`
- `examples/rewrite/`: specs, prompts, gate results, converter output and rewritten project per fixture set
- `tests/`: `uv run --extra dev pytest` (needs `dg` in the environment for the `accept` tests, which take about 90 s). 69 tests (59 existing, 10 for the rewrite route).

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
