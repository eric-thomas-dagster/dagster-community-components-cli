# airflow-to-dagster

Migrate Apache Airflow DAGs to Dagster. The skill (`SKILL.md`) is the method: pick a tier (observe, bridge or native), inventory the Airflow project, convert what has a mechanical mapping by code, hand-convert the rest, then check behavior against Airflow before cutover. The scripts in `scripts/` do the deterministic parts: a static scanner, a runtime extractor, a converter that writes Dagster code, a manifest with a completeness gate, and parity harnesses.

**Status: scripts, not a packaged CLI.** There is no `pyproject.toml` and no installed command; each step is a Python script run from `scripts/`. Conversion covers Python, Bash, S3 and Snowflake tasks, plus the scheduling, branching, mapping and partition constructs listed below. Provider operators with no conversion become loud stubs. Nothing here has run against a live Airflow deployment, real S3 or real Snowflake.

## Quick start

```bash
# 1. Inventory (read-only; never imports DAG code)
python3 scripts/inventory.py path/to/airflow_repo --out manifest.json

# 2. Extract what Airflow actually built. EXECUTES the DAG files: sandbox, trusted code,
#    same Airflow version and providers as the project.
python3 scripts/airflow_ir.py path/to/dags --out ir.json [--pythonpath path/to/project_root]
python3 scripts/airflow_truth.py path/to/dags --out truth.json
python3 scripts/reconcile.py manifest.json truth.json --out manifest.json

# 3. Convert, then record the outcome in the manifest
python3 scripts/convert_ir.py ir.json --out-dir my_project/src/my_pkg/defs --report report.json
python3 scripts/apply_conversion.py manifest.json ir.json report.json --out manifest.json

# 4. Gates and completeness
python3 scripts/validate_defs.py my_project --manifest manifest.json --advance
python3 scripts/status.py summary manifest.json            # exits nonzero while any unit is pending
python3 scripts/status.py defer manifest.json A031 --reason "custom timetable"
```

Create the target project with `uvx create-dagster project <name> --uv-sync` first (the skill relies on `dagster-expert` for the Dagster side). `convert_ir.py --help` lists the options: `--caveats {off,raise,skip}`, `--catchup {defer,ignore}`, `--mapping {graph,partitions}`, `--output {python,components}`, `--dataset-keys {path,script-orchestrator}`, `--partition-start`, `--return-all`.

## What gets converted

Details and the full list are in [reference/conversion.md](reference/conversion.md) and [reference/mapping.md](reference/mapping.md).

| Airflow | Dagster |
|---|---|
| `PythonOperator`, `@task` | Asset running the same function; its source is copied with only the imports, constants and helpers it uses |
| `BashOperator`, `@task.bash` | Asset running the command with `bash -c` |
| `EmptyOperator` | Elided; edges rewired |
| Value passed between tasks | Asset parameter (the IO manager carries it) |
| S3 list, copy, delete (explicit keys), upload operators; `S3Hook` in callables | Assets calling boto through the `aws_*` resource; a mirrored `S3Hook` subset in `runtime.py` |
| Snowflake COPY INTO, `SnowflakeOperator`, `SQLExecuteQueryOperator` on a Snowflake connection | Assets running the SQL through the Snowflake resource |
| Outlets and inlets | Asset keys and dependencies |
| Schedule: none, `@once`, cron | No schedule or `ScheduleDefinition` with the timezone |
| Schedule: one asset, any of several, all of several | `AutomationCondition.eager()`; all-of uses a per-asset `newly_updated().since_last_handled()` condition |
| Task groups, retries, retry delay, backoff, pool | Key prefix, `RetryPolicy`, `pool` |
| Connections | Resources under the same key (`resources.py`) |
| Branch, short-circuit, mapped tasks (`.expand`), setup/teardown, trigger rules | One graph asset with an op per task (default), or independently runnable partitions with `--mapping partitions`; see [reference/dynamic-mapping.md](reference/dynamic-mapping.md) |
| `PythonVirtualenvOperator`, `ExternalPythonOperator` | Callable run in its own interpreter (`af_isolated`) |
| `TriggerDagRunOperator` (no `wait_for_completion`), task `sla`, `ExternalTaskSensor` and time sensors (some forms) | Translated, with caveats where behavior differs; see [reference/conversion.md](reference/conversion.md) |
| `CronPartitionTimetable`, `PartitionedAssetTimetable` with identity and start-of-period mappers | Partitioned assets and per-partition automation; see [reference/partitions.md](reference/partitions.md) |
| dag-factory, Blueprint and loop-generated DAGs | Converted from the runtime IR like any DAG; factory handling in [reference/factories.md](reference/factories.md) |

Generated output per project: `defs/<dag>.py`, `runtime.py` (shims copied into the project), `resources.py`, `sources.py` (assets a DAG reads but no converted DAG produces), `converted_requirements.txt`, `concurrency_limits.yaml` (limits the deployment must apply), and the `report.json`. With `--output components`, DAGs whose tasks all have a registry component form are written as `defs/<dag>/defs.yaml` (Bash, Python callable, S3 operators, cron schedule) and the rest stay Python.

Not converted by code: Variables, `default_args` beyond retries, alerts and callbacks, and deployment. Those are the manual "platform layer" step in `SKILL.md`.

## When something cannot be converted

- **Loud stubs.** With `--caveats raise` (default) a task that cannot be translated (Airflow API in the callable, unresolved import, unsupported operator or template) keeps its place as an asset whose body raises `AIRFLOW-CAVEAT: <reason>`. `skip` returns nothing instead; `off` defers the whole DAG.
- **Two kinds of caveat in `report.json`:** `stub` (not translated) and `degraded` (translated, with a stated difference, e.g. `depends_on_past`, `pool_slots`, `fail_stop` not enforced).
- **Deferrals.** Whole-DAG problems (producer conflicts, some schedule shapes, SubDAGs, a custom operator class that cannot be copied) defer the DAG with every reason. A converter bug surfaces as a deferral named `CONVERTER ERROR`, not a crash.
- **Operator report.** `report.json` has an `operators` section: each operator type, its task count, whether it is mapped, and the integration or registry component that fits.
- **Manifest.** `apply_conversion.py` writes a `target` per converted unit and defers the rest with the reason. A unit with a caveat stays `deferred`.

## Validation and parity

1. `validate_defs.py`: `dg list defs --json` loads the project, and every unit's target (assets, deps, jobs, schedule crons) exists and matches. A unit with no target is reported UNPLANNED. `--advance` marks passing MECH units complete; it does not prove behavior.
2. `status.py summary`: exits nonzero while any unit is neither `complete` nor `deferred` with a reason.
3. Parity ([reference/parity.md](reference/parity.md)): `parity_incremental.py CONFIG.json` runs each converted DAG with Airflow's `dag.test()` and with `dg.materialize` in separate interpreters and compares overall outcome, each task's outcome and returned values (`scripts/parity/`). It only re-runs DAGs whose generated module hash changed. [examples/three_dags/](examples/three_dags/) has function-level and automation-condition parity checks.

Measured results are recorded in [reference/conversion.md](reference/conversion.md) and [reference/compatibility.md](reference/compatibility.md): on a corpus of 292 DAGs from 14 public repos, 284 convert (59 with at least one stubbed task); parity on 117 eligible DAGs found identical outcomes on 106 of 115 comparable ones (the rest differ in random or time-based values). The corpus leans toward teaching repos, so do not read the rate as typical. The converter was also checked from Airflow 2.2 to 3.4 ([reference/compatibility.md](reference/compatibility.md)); behavior comparison runs from 2.5 on.

## Layout

- `SKILL.md`: the workflow (phases 0 to 7), hard rules, the tier choice.
- `scripts/`: `inventory.py`, `airflow_ir.py`, `airflow_truth.py`, `reconcile.py`, `convert_ir.py`, `apply_conversion.py`, `validate_defs.py`, `status.py`, `parity_incremental.py`, `parity/` (both sides and the comparer), `tests/`.
- `reference/`: `tiers.md`, `mapping.md`, `conversion.md`, `parity.md`, `factories.md`, `dynamic-mapping.md`, `partitions.md`, `compatibility.md`, `component-wishlist.md`.
- `examples/`: `three_dags/` (a completed trial with equivalence rows), `s3_to_snowflake_factories/`, `dagfactory_family/`, `runtime_conversion/` (converted repo and parity harness), `airflow_release_matrix/` (18 one-construct DAG files run across Airflow releases).

## Tests

```bash
unset VIRTUAL_ENV
uv run --no-project --with pytest --with pyyaml --with dagster --with dagster-aws \
  --with dagster-snowflake --with jinja2 --with croniter --with boto3 \
  python -m pytest -q scripts/tests
```

152 tests (`test_convert_ir.py`, `test_inventory.py`): 150 pass and 2 are skipped when `dagster_community_components` is not installed. Without `dagster` and `jinja2` installed, many tests skip or fail on import, so install them. The tests do not need Airflow; the extractor (`airflow_ir.py`) and the parity harness do.

## Limitations

- `airflow_ir.py` and `airflow_truth.py` execute DAG code. Run them only on trusted code in a sandbox.
- Only Python, Bash, S3 and Snowflake operators convert by default. Other provider operators (for example `KubernetesPodOperator` and HTTP operators) have no conversion yet and become stubs (or defer the DAG with `--caveats off`); the report names the integration or component that fits.
- A DAG is converted whole or stubbed per task; the converter does not partially convert a graph silently.
- Not enforced in Dagster (reported as degraded or deferred): `depends_on_past`, `wait_for_downstream`, `max_active_tis_per_dagrun`, `pool_slots` above 1, `execution_timeout` on a Python task, `fail_stop`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`.
- `catchup=True` defers the DAG unless `--catchup ignore`, which runs from now on and does not replay history.
- Eight corpus DAGs remain deferred (consumers of runtime-partitioned assets with rollup, fan-out, product or allowed-key mappings; a timetable with a mapper per asset; some mapped-task argument shapes).
- Parity covers DAGs with no external systems. Operator-based tasks, sensors on external systems, durations, failure behavior and data volume still need a side-by-side run on the same logical window.
- Airflow's run history does not migrate.
