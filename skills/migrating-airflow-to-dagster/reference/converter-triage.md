# Converter triage: why the deterministic converter was `different` on real behaviour

Source: the converter route of the stage-1 behaviour comparison (40 DAGs, real Airflow 2.11 / 3.4 against the converter's `--return-all` output run on Dagster 1.13). The first run (`conv_v2`) had the converter equivalent in either form on 16 of 36 evaluated DAGs (44%) and `different` on 19. Each `different`, `no-output` and `baseline-failed` row was read (the per-DAG verdict JSON, the converter's output, and the failing step's error) and traced to a cause in `convert_ir.py`, the generated runtime, or the IR extractor (`airflow_ir.py`).

**These 40 DAGs were used to find the bugs, so every number below is a TRAIN number.** They show that the causes were real and are fixed; they do not measure how the converter generalises. The fixes are covered by executed tests that do not depend on these DAGs (`scripts/tests/test_converter_triage.py`), plus an ordering invariant that is also run over the 40 stage-1 IRs when they are present.

## Root causes and what was done

| # | Cause | DAGs affected (of the 19 `different` + the 5 `equivalent-with-differences`) | Fix | Risk |
|---|---|---|---|---|
| 1 | **Lost ordering.** The graph form (used for any DAG with mapping, branching, short-circuit, trigger rules, setup/teardown, skip logic) was a `graph_multi_asset`. That decorator cannot declare the edges between its own outputs, so every task in the DAG was an asset with no dependency on the others; Airflow's order existed only as op wiring inside the graph and was invisible in the asset graph (and so to lineage, partial runs and the harness). It showed as `lost_ordering` on 17 DAGs (task groups, mapped tasks, branch outputs alike). | 2_3_example_toy_dag_mapping_xcoms, UI_features_toy, complex_dag_structure_rainbow, example_bash_decorator, example_complex, launch_window_history, mapped_task_loop, planet_conditions, openlineage_base_complex_dag, task_group_mapping_toy, and the 5 `equivalent-with-differences` (example_astronauts x2, example_branch_dop_operator_v3, example_dtm_non_taskflow_api, toy_setup_teardown_task_group_and_failures) | The graph is now `@dg.graph(out={..: GraphOut()})` wrapped by `AssetsDefinition.from_graph(internal_asset_deps=...)`: still one run and one set of ops, one asset per task, and every Airflow edge (through task groups, mapped tasks, branch outputs, joins) is an asset dependency. The `run_check` asset depends on every task. Tags carry the `kind`, metadata the Airflow task. | Low: same ops and wiring, only the asset declaration changed. A graph asset still cannot declare edges from *external* triggering assets (documented). The `dependencies` dimension now matches on every converted DAG. |
| 2 | **Exit code 99 failed instead of skipping.** `BashOperator` / `@task.bash` skip on exit code 99 (`skip_on_exit_code`); `run_shell` treated every non-zero exit as a failure, and a DAG whose command exits 99 was not routed to the form that can skip. | example_bash_decorator (`this_will_skip`) | `run_shell(..., skip_on_exit_code=(99,))` raises the skip exception; a shell task whose command visibly contains `exit <skip code>` puts the DAG in the graph form (where a skip is a state) and is wrapped by `af_skippable`; non-default codes are passed through; any other code still fails. | A command that reaches 99 without a literal `exit 99` is not detected and fails, as before (documented). |
| 3 | **`@task.run_if` / `@task.skip_if` conditions were dropped** (the decorators were stripped with the other decorators), so the task always ran. | run_if_skip_if_example | The condition (a lambda, or a named function copied with the callable) wraps the function (`af_run_if`) and skips it at run time; such DAGs use the graph form. | A condition that is neither a lambda nor a plain function defined in the DAG file defers the DAG. |
| 4 | **Missing Jinja names and macros**: `macros.json`, `macros.yaml`, `macros.ds_format_locale` (babel), `macros.time/uuid/random/dateutil`, `var.value` / `var.json`, DAG-level `user_defined_macros` (`{{ my_json }}`) were refused at conversion and became stubs that fail when run. | json_yaml_macros_toy, babel_format, simple_redshift_1 (`var`), example_bash_decorator (`@task.bash` returns a template that Airflow renders; the converter returned it unrendered) | Added to the runtime and to the accepted-name tables; `var` reads `AIRFLOW_VAR_<KEY>` as `Variable.get` does; user macros ride in `af_context`; a `@task.bash` command is rendered; `babel` is added to `converted_requirements.txt` when `ds_format_locale` is used. | `conn.*` and any other `macros.*` still stub loudly. `ds_format_locale` needs `babel` installed where Dagster runs (it was not in the evaluation environment, so babel_format still fails there). |
| 5 | **Task-level `params` were not in the IR.** `{{ params.my_param }}` on a task (`params={"my_param": ...}`) raised an undefined-variable error; the step then retried for 300 s, which is why `tutorial` hit the 300 s sandbox timeout. | tutorial | `airflow_ir.py` records each task's `params`; the converter merges them over the DAG's when it builds the context. | Low. |
| 6 | **A stub (AIRFLOW-CAVEAT) was retried.** A task the converter could not translate fails the same way every time, but kept the task's retry policy, so a 3-retry task with a 60 s delay took minutes to fail (`launch_window_history`, `planet_conditions` also hit the 300 s timeout). | launch_window_history, planet_conditions, tutorial | `af_caveat` raises `dg.Failure(allow_retries=False)`. | None: the failure and message are unchanged, only the waiting is gone. |
| 7 | **Mapped classic `PythonOperator` lost its callable** (the IR read `python_callable` from the task, not from `partial_kwargs`): the whole task was a stub. | 2_3_example_toy_dag_mapping_xcoms, 2_4_example_toy_dag_mapping_multiple_parameters (still deferred: see below) | `airflow_ir.py` falls back to `partial_kwargs["python_callable"]`. | IRs extracted before the fix lack it; re-extract. |
| 8 | **`ti.xcom_push(key="return_value", ...)` raised** (`NotImplementedError`) in a sensor callable that Airflow handles. | pythonsensor_example | The pushed value is kept and used as the value (a sensor's done flag becomes a `PokeReturnValue` carrying it). | Low. |
| 9 | **`logging.getLogger("airflow.task")` counted as an Airflow dependency**, stubbing every task of a DAG that used the common `t_log` idiom. | launch_window_history, planet_conditions, launch_readiness_report | The logger-name assignment is copied as plain code. | None. |
| 10 | **Helpers defined inside the DAG function** (a skip condition, `_reference_date`) were "unresolved names". | run_if_skip_if_example | Plain undecorated helpers nested in the DAG function or `with DAG` block are copied when they use only module names; others stay a loud stub. | A helper that closes over the DAG function's locals is still unresolved (loud). |
| 11 | **Mapped task groups** (`@task_group.expand(...)`): the IR did not record them, so a task in the group was emitted unmapped with an argument that referenced nothing (`XCom reference to None`). | UI_features_toy, task_group_mapping_toy | The IR records `mapped_group`; the converter reports each task in such a group as a stub that names the group, instead of a vague dangling-argument failure. **Not converted**: see the remaining list. | Honest, not a fix. |
| 12 | **`load failure: resource with key 'aws_default' required ... was not provided`** (`load_file_to_S3`, `simple_redshift_1`). **Not a converter bug.** The converter provides the resource in the same project (`defs/resources.py`, a `@dg.definitions` module). The behaviour harness loads a single DAG module for the converter route and never loads `resources.py`, so every converted DAG with an S3, Snowflake or provider-connection step fails to load there. Also the evaluation Dagster environment has no `dagster-aws`. | load_file_to_S3 (excluded as `baseline-failed`), simple_redshift_1 | None in the converter. The test `test_converted_s3_project_provides_the_resource_key_its_op_requires` asserts the project provides the key (it needs `dagster-aws`, so it is skipped in the evaluation environment). The harness (`parity/run_dagster.py`, owned by another change) should load the project's resources. | The converter-route verdict for connection-using DAGs is a harness artefact until that is fixed. |

`baseline-failed` rows (example_branch_operator x2, new_branching_decorators_example, load_file_to_S3) are correctly excluded: Airflow itself fails there (virtualenv tasks that need a network, a missing file).

## What cannot be fixed deterministically (the remaining `different`)

| DAG | Why |
|---|---|
| UI_features_toy, task_group_mapping_toy | Mapped task groups: the group's `expand` values become the tasks' arguments. A deterministic form needs a mapped sub-graph per group with the group's inputs threaded through; not implemented. Use the rewrite route. |
| mapped_task_loop | `LoopDecision` / the `loop` context (a task-group loop, Airflow 3.4): no equivalent in the runtime. |
| my_dag_task_flow, example_passing_params_via_test_command | The callable calls Airflow APIs directly (`airflow.sdk.Connection`, an `AwsGenericHook`, `task.params`, `test_mode`). The converter refuses to guess and stubs loudly. |
| openlineage_base_complex_dag | User-defined operators without an `execute` method and `CustomMappedOperator`: nothing to convert. The `pools` difference is the converter's deliberate `max_active_tis_per_dag` -> pool mapping (Airflow reports no pool). |
| complex_dag_structure_rainbow | `ExtractFromObjectStorageOperator` has no conversion. |
| producer_dag | An `ObjectStoragePath` argument cannot be serialised into code. |
| example_bash_decorator | `__file__` (the DAG file's own path) is meaningless in the converted project; the task reads the DAG file's stat. |
| babel_format | Needs `babel` installed (now in `converted_requirements.txt`); missing in the evaluation environment. |
| example_complex | Nondeterministic bash (`exit $((RANDOM % 2))`) with retries, so Airflow's success was luck, and the graph form's per-op pools and retries are read by the harness as the union over the graph (every asset of one graph shows every op's pool). |
| planet_conditions | A helper that closes over the DAG function's locals (`_reference_date` uses the enclosing `params`), so it is not a module-level name. |
| 2_4_example_toy_dag_mapping_multiple_parameters (`no-output`) | Deferred: `op_args` and `op_kwargs` expanded together (`mix_cross_and_zip`); the graph form maps one of them. |
| simple_redshift_1 | Harness artefact (see 12). |

## Re-measurement (TRAIN numbers: the same 40 DAGs the bugs were found on)

Converter route only, real Airflow baselines re-run, `eval_behavior.py batch --routes converter`, report from `eval_report.py`. Evaluated DAGs: 36 of 40 (4 `baseline-failed` excluded).

| verdict | before (`conv_v2`) | after (`conv_v3`) |
|---|---|---|
| equivalent | 7 | 16 |
| equivalent-with-differences | 7 | 4 |
| matches-baseline-failure | 2 | 1 |
| different | 19 | 14 |
| no-output | 1 | 1 |
| **equivalent in either form, or matching a by-design baseline failure** | **16/36 (44%)** | **21/36 (58%)** |

Movers (all improvements, except one baseline flake): 2_3_example_toy_dag_mapping_xcoms, json_yaml_macros_toy, pythonsensor_example, run_if_skip_if_example (different -> equivalent); launch_window_history, tutorial (different -> equivalent-with-differences); example_astronauts (two repositories), example_branch_dop_operator_v3, example_dtm_non_taskflow_api, toy_setup_teardown_task_group_and_failures (equivalent-with-differences -> equivalent). `fetch_extra_info` moved from matches-baseline-failure to different with **identical converter output** (the same five tasks fail): Airflow's own baseline failed `get_extra_triggering_run` in one run and not in the other, so it is baseline nondeterminism, not a converter change.

The `dependencies` dimension, which differed on 17 DAGs, now differs on none. The 14 that remain `different` are listed above with the reason each cannot be fixed deterministically. This is a train number: it shows the causes are fixed, not how many unseen DAGs convert equivalently.

## How this was checked

- `scripts/tests/test_converter_triage.py`: executed tests (installed Dagster, the real emitted code) for the edge declaration through task groups, branches and mapped tasks; an ordering invariant over every converted stage-1 DAG; exit 99 (default and custom code, downstream skipped, run succeeds, other codes fail); macros and `var`; task params; stub retries; `xcom_push` of a return value; `run_if` / `skip_if` (lambda, named, nested helper); `user_defined_macros`; the `airflow.task` logger; `@task.bash` template rendering; `babel` in the requirements; the IR extractor on real Airflow 2.10 (mapped classic callable, task params, mapped groups).
- Old test expectations that encoded the bug or the gap were updated, and say so in the test: the pool test looked for `@dg.graph_multi_asset`; one test expected `var.value.x` to defer; one expected `xcom_push("return_value")` to raise; one expected `macros.ds_format_locale` to defer.

## Stage 2a: classes fixed, and the v3 to v4 re-measurement

Source: `eval_behavior.py batch --routes converter` on the 60-DAG stage-2a sample (57 evaluated, 3 `baseline-failed`), real Airflow 2.11 / 3.4 against `convert_ir.py --return-all` on Dagster 1.13. **Stage 2a was the evaluation sample for the converter's v3; reading its `different` rows to fix v4 makes it TRAINING data for the converter now.** The v4 numbers below are therefore not a generalisation estimate; a fresh sample is needed for that.

| Change (general rule, tested on synthetic code in `scripts/tests/test_converter_stage2a.py`, `test_granularity.py`, `test_eval_harness.py`) | Class |
|---|---|
| Product granularity is the default (`--granularity task` is the fallback); the harness maps folded steps (`mode: folded`), skips their values and accepts their state when the leaf asset fails | design principle: an asset is a data product |
| An `EmptyOperator` that declares outlets or inlets is an asset (returning None), not glue; glue tasks are listed in `report.json` under `glue` and the harness marks them dropped | elided EmptyOperator outlets |
| `xcom_pull` of another DAG or of a prior run: a pushed key reads the latest materialization's metadata (None if never materialized, as Airflow); another DAG's return value stays a LOUD error naming the Dagster equivalent | cross-DAG xcom_pull |
| `ti.map_index` (`-1` unmapped; the index from the dynamic mapping key `i<n>`) | ti.map_index |
| A helper the project keeps under the `airflow.` namespace (found by walking up from the DAG file) is inlined as plain Python, with what it uses; a real Airflow import still defers | repo-local helper under `airflow.*` |
| `__file__` becomes a constant `_AF_DAG_FILE`, default the original DAG path, overridable with `AIRFLOW_DAG_FILE` | `__file__` shim |
| Harness: per-STEP states from step events (producing op of each graph-asset output, failed / succeeded-without-yielding = skipped / never ran behind a failed step = upstream_failed). Before, one failing op marked every output of its graph asset `failed` | failed vs upstream_failed attribution, unyielded outputs |
| Harness: an Airflow serde-encoded value (`pandas.core.frame.DataFrame@version=1(hex)`) is not compared as text and the report says why | DataFrame XCom stringification |

| verdict (converter, 57 evaluated) | v3 | v4 |
|---|---|---|
| equivalent (strict) | 29 (51%) | 34 (60%) |
| equivalent-with-differences | 8 | 8 |
| matches-baseline-failure | 3 | 6 |
| different | 15 | 7 |
| no-output | 2 | 2 |
| equivalent in either form (incl. matching a by-design failure) | 40 (70%) | 48 (84%) |

Movers (all `different` in v3): example_local_kubernetes_executor, example_time_delta_sensor_async, pandas_xcom, my_astronauts_dag, example_dataset_producer_with_and_condition became `equivalent`; toy_custom_names_dynamic_tasks_taskflow, toy_custom_names_dynamic_tasks_traditional_operators and toy_dynamic_task_default_index became `matches-baseline-failure` (they were a harness attribution error: the downstream mapped task is `upstream_failed` in both). Caution on the matches-baseline-failure gain: three of the 8 movers come from the harness change, not the converter, so the converter-only gain is smaller than the headline.

Not done after pass 1: mapped task groups, helpers closing over DAG-function locals, `ObjectStoragePath` arguments, user-defined mapped operators and operators with non-plain constructor arguments, registry-component mapping for provider operators, and the direct Airflow API calls (`Connection`, hooks). The two remaining `different` DAGs are explained below.

## Stage 2a, pass 2: the two remaining `different` DAGs (both converter bugs, now fixed)

Stage 2a is training data for the converter now; both rows below were read, traced to a cause in `convert_ir.py`, and fixed by a general rule (tests in `scripts/tests/test_converter_stage2a_v5.py`).

| DAG | Root cause | Fix |
|---|---|---|
| example_dataset_producer | Not a harness artefact. In the asset form an `EmptyOperator` that declares an outlet is an asset, but the emitter's last `else` branch is the S3 upload branch, so the asset ran `s3_upload_file(client, None, None, None, False)` (and required an `aws_default` resource): the error `'NoneType' object has no attribute 'startswith'` was the bucket/key parse of `None`. | An `ELIDED`-type task that is kept as an asset returns `None` (nothing runs; the asset is the dataset it publishes). |
| example_task_state_store_mapped | Two causes. (1) `needs_ti` was true for any use of `ti`, so a task that only reads `ti.map_index` in a graph-form DAG was treated as pulling XComs by task id and became a stub with a misleading message. (2) Airflow 3.4's `task_state_store` context argument was not a known name, so the callable received `None` and would fail on `.set`. | (1) `needs_xcom` separates `xcom_pull` (or passing `ti` on to other code) from the `ti` attributes that need no upstream values; only the former wires upstream values in. (2) `task_state_store` is a supported context name: a plain-Python shim (`get`, `set`, `delete`, `clear`; JSON file per dag, task, run and map index under `AIRFLOW_TASK_STATE_DIR`, default the temp dir, so it survives a retry). Dagster has no per-step key/value store; asset metadata is the nearest durable record. |

Also in pass 2: `report.json` carries `converted_with_component: true` and `component_types` for a DAG written as registry components (`--output components`); the behaviour harness need not compare those.

| verdict (converter, 57 evaluated) | v3 | v4 | v5 |
|---|---|---|---|
| equivalent (strict) | 29 (51%) | 34 (60%) | 36 (63%) |
| equivalent-with-differences | 8 | 8 | 8 |
| matches-baseline-failure | 3 | 6 | 6 |
| different | 15 | 7 | 5 |
| no-output | 2 | 2 | 2 |
| equivalent in either form (incl. matching a by-design failure) | 40 (70%) | 48 (84%) | 50 (88%) |

v5 moves exactly the two DAGs above. Still `different`: deltalake_example, example_dag_decorator, example_bash_decorator, task_group_mapping_and_pulling_usecase, example_dynamic_task_mapping_with_no_taskflow_operators. This is training data, not a generalisation estimate.

### Not done (honest list)

Still not converted deterministically: mapped task groups (the verified native form is in `reference/rewrite-patterns/mapped_tasks.py`, `mapped_group`; the converter still stubs the tasks in such a group and names the group), helpers that close over DAG-function locals, `ObjectStoragePath` arguments, user-defined mapped operators, operators with non-plain constructor arguments, and direct Airflow API calls (`Connection`, hooks) via an `AIRFLOW_CONN_*` env shim. Registry mapping already exists for `BashOperator` (`shell_command_asset`), S3 file and object operators, SQL, `BashSensor`/`TimeSensor`, Lambda, SSH, Docker and BigQuery under `--output components`; no new operator mapping was added in pass 2, and the registry's http and email components were not evaluated.
