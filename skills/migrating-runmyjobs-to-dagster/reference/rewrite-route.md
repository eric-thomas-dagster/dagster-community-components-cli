# The rewrite route: gates, measured evidence, limits

The second route of the skill (`SKILL.md`, "Two routes"): `analyze` writes a facts-only spec per unit, `prompt` writes the instructions, Claude writes the Dagster project from scratch with `dagster-expert` and `dagster-community-components`, `accept` runs the gates, `equivalence` compares the result with the converter's. The converter stays the default and the fallback. This file says what was measured. **Nothing here shows the rewrite route is better**; where it differs from the converter the differences are listed and the one that matches the source is named.

## File and manifest formats

Aligned with the Airflow skill's rewrite route (`skills/migrating-airflow-to-dagster/`: `dag_spec.py`, `rewrite_prompt.py`, `accept_rewrite.py`; read only): spec as markdown plus json per unit (`rewrite/<unit>.spec.md`, `.spec.json`), a prompt as markdown (`<unit>.prompt.md`), gate results as json (`<unit>.gates.json`), and in the manifest `route: converter | rewrite`, `gates` (per gate: pass, FAIL, not run), `rewrite_attempt` (a rewrite that failed on a unit the converter had already completed). The header-docstring grammar is the same: `<id>: replaced by `<asset>`` / `folded into` / `merged into` / `dropped: <reason>` (`reimplemented` for a call whose text is not kept). Differences: the units are chains and process definitions (not DAGs), the commands are RunMyJobs's, the gates here include `matches_spec` against `dg`-loaded definitions and a stub gate, and the commands are subcommands of one CLI (`analyze`, `prompt`, `accept`, `equivalence`) instead of scripts.

## The gates (`accept`)

| Gate | What it checks | Notes |
|---|---|---|
| `definitions_load` | `dg check defs` in the project (the converter's gate 2) | falls back to loading with dagster, and says so, when `dg` is missing |
| `matches_spec` | the converter's gate 3, made behavioral because a rewrite picks its own keys: every call maps (header) to an existing asset or sensor; every earlier-to-later pair of the chain graph is kept (downstream, or folded into one asset); the unit's schedules equal the spec's cron and time zone (an element with no cron form may become one extra schedule); pools equal the queues; retries equal the restart handler's maximum or Maximum Starts minus 1 | pairs involving a call replaced by a sensor are counted and not compared |
| `commands_preserved` | each command or SQL text is in the unit's code or YAML (whitespace and `$x`, `${x}`, `{x}` normalised), or the header says `reimplemented` | a command holding a redacted credential is not compared |
| `completeness` | every RunMyJobs object in the spec's accounting list (calls, schedules, queues, file events, preconditions, status handlers, waits, run windows, extractor notes, notifications) has a header line | the per-unit counterpart of `status summary` |
| `generated_tests` | writes and runs `tests/test_<unit>_rewrite.py`: keys, order, schedule, pools, retries, and each asset with no external system is materialized (echo/print commands run, SQL runs against SQLite through `RMJ_CONN_<NAME>`); anything else is reported as not materialized, with the reason | other commands run only with `--run-commands`; an asset with a `run_window` skips outside the window, so its materialization proves load and clean exit, not that the command ran (the detail line says so) |
| `structural_checks` | asset checks exist where a check is derivable (a declared output that is a file path) | the model carries no output locations in any fixture, so this gate is `not run` everywhere, with that reason; command exit status is enforced by the asset failing; no business check is invented |
| `no_stub_left` | no `RMJ-CAVEAT` stub in the unit's code | a stub means the unit is not ported |
| `no_secret_values` | credential-looking literals and URLs with credentials | pattern scan, not a proof |
| `no_invented_apis` | imports resolve; every component `type:` is a real class (registry classes are loaded, not only looked up in the lazy export) | |

A gate that cannot run fails. Passing the gates means the structure, schedule, pools, retries, commands and accounting match the source model and the unit loads and (where safe) runs. It does not mean behavior matches RunMyJobs run results; run both in parallel before cutover.

Manifest rule: all gates passed advances the unit's manifest rows with the evidence; any failure leaves them deferred with the failure (the converter's output remains the fallback); a unit the converter already completed keeps `route: converter`. A queue shared by several units completes only when every unit using it has passed; a queue no call uses (it happens: `car_dm`) belongs to no unit and needs `status advance` or `status defer` by hand.

## Sample: what was run

Sample size, stated plainly: **20 units** in 8 model fixtures (3 synthetic: the bundled sample, the linear-chain fixture; 5 from the skill's real-export fixtures, whose chains are mostly `System_DoNothing` placeholders because the export holds no process definitions behind them). The converter route was gated on all 20. The rewrite route was written and gated on **8** (written by Claude in this session following each generated prompt; one session, no repetition, so no variance estimate). Rewrites not run: the other 12 units, including the 16-call and 26-call chains, and the estate prompt (`ESTATE.prompt.md` is generated and tested for content, never followed).

Converter route, all 20 units (`examples/rewrite/run_converter_gates.py`; load = `dg check defs`; structure = expected assets, deps and schedule crons from the manifest):

| Model | Unit | Calls | Converter disposition | Assets | Stubbed | Degraded | Load | Structure |
|---|---|---|---|---|---|---|---|---|
| sample (synthetic) | LEGACY_SAP_EXTRACT | 1 | stubbed | 1 | 1 | 0 | pass | pass |
| sample | NIGHTLY_SALES_LOAD | 6 | stubbed | 6 | 1 | 2 | pass | pass |
| sample | PURGE_TEMP | 1 | degraded | 1 | 0 | 1 | pass | pass |
| linear_chain (synthetic) | FIN_MIXED_RETRY | 2 | degraded | 2 | 0 | 1 | pass | pass |
| linear_chain | FIN_NIGHTLY | 3 | degraded | 1 | 0 | 1 | pass | pass |
| car | My_Job_Definition | 1 | stubbed | 1 | 1 | 0 | pass | pass |
| car_dm | NIGHTLY | 2 | degraded | 2 | 0 | 2 | pass | pass |
| rest_json | NIGHTLY | 2 | degraded | 2 | 0 | 2 | pass | pass |
| car_chains | Forecast_Replenishment_Template | 16 | degraded | 16 | 0 | 1 | pass | pass |
| car_chains | Full_FR_Run | 10 | degraded | 10 | 0 | 1 | pass | pass |
| car_chains | Process_Negative_Stock_Template | 2 | translated | 1 | 0 | 0 | pass | pass |
| car_chains | ReplenishShop | 4 | translated | 1 | 0 | 0 | pass | pass |
| car_isu | ISU_Core_Processing_Template | 16 | degraded | 16 | 0 | 4 | pass | pass |
| car_isu | IS_1A_Read_Bill_Template | 6 | degraded | 6 | 0 | 2 | pass | pass |
| car_isu | IS_2_Meter_Template | 3 | degraded | 1 | 0 | 2 | pass | pass |
| car_isu | IS_3_DailyBilling_Template | 2 | translated | 1 | 0 | 0 | pass | pass |
| car_isu | IS_4_BankInclearing_Template | 7 | degraded | 7 | 0 | 2 | pass | pass |
| car_isu | IS_5_DMS_Template | 3 | degraded | 1 | 0 | 2 | pass | pass |
| car_isu | IS_6_CreditCardProcessing_Template | 1 | translated | 1 | 0 | 0 | pass | pass |
| car_plan_to_produce | Plan_to_Produce_Template | 26 | degraded | 26 | 0 | 21 | pass | pass |

Rewrite route, the 8 units, with the equivalence verdict against the converter (each side measured against the source facts):

| Unit | Rewrite gates | Rewrite structure | Converter structure | Equivalence (machine-checked dimensions) |
|---|---|---|---|---|
| NIGHTLY_SALES_LOAD (synthetic, converter stubs it) | accepted, 8 of 8 applicable gates pass, 5 of 5 assets materialized | 5 assets, 1 sensor, 2 pools, schedule `30 2 * * *` America/Chicago | 6 assets, 1 stub, same pools and schedule | differs: the rewrite ported the stubbed RedwoodScript step (rewrite matches the source: no stub); the converter keeps the command texts of `margin` and `notify` verbatim, the rewrite reimplemented both (converter matches the source on text). Notification became a run-status sensor that fires after the whole run, no longer in parallel with `margin` |
| PURGE_TEMP (synthetic, converter degrades it) | accepted | 1 asset, 2 schedules (interval window plus a month-end schedule function) | 1 asset, 1 schedule | differs: the converter drops the "last day of month" element, the rewrite keeps it with a schedule function (rewrite matches the source; the day logic is not tested by the gates) |
| LEGACY_SAP_EXTRACT (synthetic, SAP call with no parameters) | **not accepted** (`no_stub_left`) | 1 loud stub | 1 loud stub | equivalent: both stub; the spec has no SAP job name, so neither can port it. Left deferred |
| FIN_NIGHTLY (synthetic, clean) | accepted | 3 assets, pool, run window per asset, schedule, file-event sensor | 1 task-chain asset (timeout summed to 900 s) | equivalent on every machine-checked dimension |
| FIN_MIXED_RETRY (synthetic) | accepted (after the parser fix below) | 2 assets, pool, sensor | 2 assets | equivalent |
| NIGHTLY (car_dm fixture) | accepted | 2 assets, schedule UTC | 2 assets | equivalent |
| ReplenishShop (real export, placeholders) | accepted | 4 placeholder assets | 1 task-chain asset | equivalent except command text: the no-op built-in became logged placeholders (reimplemented) |
| Process_Negative_Stock_Template (real export) | accepted | 2 placeholder assets | 1 task-chain asset | same as above |

Reading it: on 6 of 8 units the two routes agree on every machine-checked dimension, or differ only because the rewrite chose not to keep a no-op command. On the 2 where the converter stubs or drops something (the RedwoodScript step, the month-end element) the rewrite did more and the converter did less. On the one unit neither can port, both fail the same way. The rewrite never lost graph order, schedule, pools or retries when its gates passed. The cost shows in what was not preserved: the rewrite changed the notification's timing and reimplemented two commands, and the BUSINESS_HOURS and BIZ time windows stay unenforced in both routes because the export does not contain them. The converter, by contrast, is complete without any model effort and is the only route measured on all 20 units.

What the machine did not compare: timeouts, run windows, success exit codes, sensor poll and size settings, and what replaced a precondition or status handler (the report prints the two statements side by side).

## Failure patterns and the fixes

Fixes went into the generator or the gate, not into the unit:

- **A header line that names more than one backticked asset** (`... replaced by `mixed_alone` (depends on `mixed_pull`)`) made the gate read the dependency as a second replacement and fail the retry check on `FIN_MIXED_RETRY`. Fixed in the header parser (only what follows the verb, up to a parenthesis or semicolon, counts) and in the prompt (name the replacing assets right after the verb, details after a parenthesis). Class fix; the unit passed unchanged.
- **Relative paths** (`--python ../env/bin/python`, a project given as `conv`) broke the subprocess gates; fixed by resolving paths in the CLI and gate.
- **A sensor in place of an asset** (the notification): the first gate version required every call to map to an asset, which contradicted the prompt's idiom; the gate now accepts a sensor named in the header and says how many order pairs it did not compare.
- **A defs folder holding `defs.yaml` does not load Python modules next to it**: the unit's YAML and Python go in sibling subfolders (`components/`, `python/`) under `defs/<unit>/`. Found by the gate (the asset was missing); now in the prompt text.
- **Window-skipped materialization** and **unused queues**: described above; both are stated by the tool, not fixed.

## Cost, measured and not measured

Measured: the generated prompt is 15 to 22 KB for the six small units here (roughly 4,000 to 6,000 tokens at 4 characters per token, an estimate) and includes the spec (4 to 11 KB). A 16-call or 26-call chain would be several times that; its spec and prompt were generated (`analyze` and `prompt` run on all 20 units) but not followed. Each `accept` run takes about 30 seconds, almost all of it `dg check defs`. Not measured: the output tokens and the number of iterations Claude spends per unit, because this session did not record them; the one failure that needed a second round (`FIN_MIXED_RETRY`) was a generator bug, not an iteration on the unit. Treat "tens of thousands of tokens for a large chain" as an expectation, not a measurement.

## Not run

The estate prompt followed end to end; the rewrite on the 12 other units; any rewrite of real RedwoodScript; the BW process chain and any SAP job against a live system; a parallel run against RunMyJobs results; a second author (a person) following the prompts; repeat runs for variance; Python output (`--output python`) as the reference draft.
