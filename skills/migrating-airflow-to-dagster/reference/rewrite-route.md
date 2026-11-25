# The rewrite route: spec, prompt, gates

Inside tier 3 (Dagster owns the code) there are two routes for how the code gets written. **Convert**: the deterministic converter, a near like-for-like conversion of what Airflow built. **Rewrite**: build a thorough, deterministic understanding of one DAG, hand it to Claude as a generated prompt, and have Claude write the Dagster code from scratch with `dagster-expert` and `dagster-community-components`, accepted only through gates. The rewrite is expected to be more idiomatic (one asset per data product, glue folded away, registry components) and to cover operators the converter stubs. This page gives the evidence rather than the claim.

## The three scripts

| Script | What it does |
|---|---|
| `scripts/dag_spec.py ir.json [--report] [--manifest] [--parity]` | `rewrite/<dag>.spec.{md,json}`, per DAG, deterministic. Facts only: tasks, operator types and resolved arguments, the source of every callable and of function-valued arguments (a sensor's `response_check`), dependencies, value (XCom) flow, assets in and out, schedule, timezone, catchup, retries, pools, timeouts, callbacks, SLAs, connection and Variable **names**, trigger rules, branching and mapping, the dbt project and selection for Cosmos. Purpose is stated only as what the facts support and marked *inferred*. Also: what the converter did and where it stubbed or degraded (as a reference), the Airflow parity baseline when given, and an explicit list of behaviours that must be preserved against differences that are acceptable. No secret value: credential-like arguments and string literals assigned to credential-like names are redacted. |
| `scripts/rewrite_prompt.py <spec.json> [--converter-module]` | `rewrite/<dag>.prompt.md`: a self-contained prompt. It tells Claude to load `dagster-expert` and `dagster-community-components` (by name, and the repository path `skills/dagster-community-components/SKILL.md` for agents that load skills from files), follow the per-stage search, verify, schema, add and wire, validate recipe of that skill's `reference/demo-workflow.md` with its real commands (`dagster-component search "<term>" --json`, `info`, `schema <id> --format json`, `add`, `list --available`; `dg list components --json`; `dg check defs`), record the id, schema fields and validation tier of every component chosen (an unvalidated tier carries a caveat), embeds the spec, states the target idioms (one asset per data product, ops only where restart points matter, one pipeline component for dataframe chains, glue as sensors, hooks and run-status sensors), lists the acceptance gates, and says the converter's output is a draft for reference and the fallback. |
| `scripts/accept_rewrite.py --spec --module --project [--parity] [--manifest]` | The gates, and the manifest record of which route produced each unit. |

## The gates

| Gate | Passes when |
|---|---|
| `definitions_load` | `dg check defs` passes in the project (when `dg` is not installed, the definitions are loaded with dagster instead and the gate says so) |
| `generated_tests` | a behavioural contract written to `tests/test_<dag>_rewrite.py` passes: the module loads, a job named after the DAG exists, the schedule's cron and timezone equal Airflow's, and for a DAG with no external system the whole DAG materializes and every asset check passes. The converter's key-for-key tests do not apply: a rewrite picks its own keys |
| `asset_checks_present` | at least one structural check when the converter derived any from the source (a rewrite folds steps, so it has fewer places to check) |
| `parity` | with an Airflow baseline: the same overall outcome, and the same value as Airflow for every task the header says is `replaced by` an asset. Without one: `not run (no baseline)`, recorded as such |
| `no_secret_values` | no credential-looking literal in the module or the component YAML (the gate never echoes the literal) |
| `no_invented_apis` | every import and every component `type:` resolves to something that exists |
| `tasks_accounted_for` | the module docstring names every Airflow task and says what happened to it (replaced, folded, dropped, merged) |

With `--manifest`, each unit of the DAG gets `route: rewrite` and the gate results: all passed, the unit is advanced with the evidence; otherwise it stays or becomes `deferred` with the failures, and the converter's output remains the fallback. A unit the converter already completed keeps `route: converter` and gets a `rewrite_attempt` note. The converter's units carry `route: converter`. `plan.py` has a Route column, and the cutover checklist shows which route produced the code and the gate results.

## Worked examples

`examples/rewrite/` has two real DAGs: `simple_sequential_pipeline` (the converter handles it) and `partner_ingest` (the converter stubs two tasks). I wrote both rewrites by following the generated prompts. Both pass all seven gates; the README there compares them with the converter's output (one asset against three, 52 lines against 100 plus a shared 1,561-line runtime; two assets that run against five assets of which two are loud stubs) and lists what the spec missed and what was fixed in the generators.

## When to use it

For a DAG the converter deferred or stubbed, a redesign-heavy DAG, the plan's last wave, and the pilot DAGs where idiomatic output matters while the mapping is being learned. Keep the converter for the mechanical bulk. **Cost:** the rewrite spends model effort on every DAG (the prompt is 14 to 16 KB for these two, the model reads it, searches the registry and writes the file, and a failed gate means another pass); the converter takes under a second. The plan's effort figures do not include it.
