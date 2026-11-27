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
| 10 | **Helpers defined inside the DAG function** (a skip condition, `_reference_date`) were "unresolved names". | run_if_skip_if_example | Plain undecorated helpers nested in the DAG function or `with DAG` block are copied when they use only module names; others stay a loud stub. | A helper that closes over DAG-function locals: copied with them when they are plain data (pass 3); a local holding a task output or any call stays unresolved (loud). |
| 11 | **Mapped task groups** (`@task_group.expand(...)`): the IR did not record them, so a task in the group was emitted unmapped with an argument that referenced nothing (`XCom reference to None`). | UI_features_toy, task_group_mapping_toy | The IR records `mapped_group`; the converter reports each task in such a group as a stub that names the group, instead of a vague dangling-argument failure. Converted in stage 2a pass 3 (below) when the group is mappable. | Pass 3 replaces the stub with the native form. |
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

(Pass 3, below, converts mapped task groups, closure helpers, `ObjectStoragePath` arguments, user-defined mapped operators and a `Connection` env shim; this list is as of pass 2.) Still not converted deterministically after pass 2: mapped task groups, helpers that close over DAG-function locals, `ObjectStoragePath` arguments, user-defined mapped operators, operators with non-plain constructor arguments, and direct Airflow API calls (`Connection`, hooks) via an `AIRFLOW_CONN_*` env shim. Registry mapping already exists for `BashOperator` (`shell_command_asset`), S3 file and object operators, SQL, `BashSensor`/`TimeSensor`, Lambda, SSH, Docker and BigQuery under `--output components`; no new operator mapping was added in pass 2, and the registry's http and email components were not evaluated.


## Stage 2a, pass 3: mapped task groups, closures, ObjectStoragePath, user-defined mapped operators, Connection shim

Stage 2a is training data for the converter (it was read to fix v4 and v5). Each item below is a general rule with an executed test in `scripts/tests/test_converter_stage2a_v6.py` that converts a small IR and RUNS the emitted Dagster code. No DAG-specific case exists.

| Item | Before | Now | Loud when |
|---|---|---|---|
| Mapped task groups (`@task_group.expand`, `.expand_kwargs`, `.partial(...).expand`) | every member a stub naming the group | the verified native form: `mapped_group` graph over a `DynamicOut`, collected, one `pick` op per member (`reference/rewrite-patterns/mapped_tasks.py`). The IR now records `mapped_groups` (what each group iterates over) and a member's argument as `{"group_arg": name}`. Tests: values per member over the items, cross product, `expand_kwargs`, empty expansion (skipped, run succeeds), one failing item (others ran, run fails, nothing after), outside inputs and a shell member, the IR extractor on real Airflow | a group over `.map` / `.zip` / `.concat`; a mapped task, branch, short-circuit, sensor, setup/teardown or gated trigger rule inside; an IR without `mapped_groups` (re-extract) |
| Helpers closing over DAG-function locals | an unresolved name | the plain locals the helper reads are copied with it (literals, containers, f-strings, arithmetic, builtin calls) | a local that holds a task output, an Airflow object or any other call: the stub names the name |
| `ObjectStoragePath` arguments | `argument ... cannot be serialised` | the IR records the URI; `ObjectStoragePath(uri)` in the runtime is `pathlib.Path` (file) or `UPath` (other schemes, `universal-pathlib` in `converted_requirements.txt`); also provided to callables that import it | a `conn_id@` netloc whose `AIRFLOW_CONN_<ID>` is not set raises `AirflowNotFoundException` naming it |
| User-defined mapped operators; non-plain constructor arguments | `CustomMappedOperator` had no class (IR) and was refused; a datetime, timedelta, DAG param or path argument was refused | the IR records the mapped operator's class and its partial arguments; one instance per item with the partial arguments merged under the item's; datetime, timedelta, path, DAG param (`{{ params.x }}`), upstream value and group item are written as code | an argument the IR could only describe by its repr: the stub names the argument |
| `Connection` / `BaseHook.get_connection` | a stub: callable depends on an Airflow import | an environment shim over `AIRFLOW_CONN_<ID>` (URI or JSON) with `extra_dejson`, `get_uri()`; the password is never printed | missing variable: `AirflowNotFoundException` naming it; provider hooks other than `S3Hook` still stub |
| Airflow metadata / session calls | `callable depends on an Airflow import (airflow.models)` | still a stub, now naming the call and the Dagster equivalent (`context.instance.get_runs`, `get_run_records`, asset value / `get_latest_materialization_event`, `context.instance` for a session) | always |

Not done in pass 3: `isinstance` checks on `ObjectStoragePath`; mapped task groups inside a DAG that needs the failure-aware form (`fail_aware`), or whose members branch; a `LoopDecision` / `loop` group; provider hooks beyond `S3Hook`; a mapped group nested in a mapped group (Airflow itself rejects it).


## Stage 2a, pass 4: operators map to resources, not to asset components (`--output components`)

Direction: an operator or hook that calls a service is a Dagster resource used inside an asset; asset components are for operators whose job is a data product or a long-running job. The mapping is data in `scripts/registry_map.py`. Registry release verified: `dagster-community-components` 0.10.193 (installed in a scratch venv; the interpreter that runs the other tests still has 0.10.188, so `scripts/tests/test_registry_resources.py` loads 0.10.193 from `EVAL_REGISTRY_193` and skips without it). RUN = loaded by dagster's component loader and executed (HTTP against a local server, other clients against a recording fake or stand-in binary); READ = `schema.json`, `README.md`, `component.py` read only.

Audit of what `--output components` did before this pass, and the verdict:

| Operator | Component before | Shape | Fields used | Verdict | Checked |
|---|---|---|---|---|---|
| `BashOperator` | `shell_command_asset` | asset | command, env_vars, cwd, skip_on_exit_code, timeout_seconds, retry_policy_* | keep asset component (runs a command that produces files) | RUN on 0.10.193 |
| `PythonOperator`, `@task` | `python_callable_asset` | asset | callable_path, args, kwargs, retry_policy_* | keep (the user's callable is the asset) | RUN on 0.10.193 |
| `BashSensor`, `TimeSensor` | `wait_until_asset` | asset | condition, command, retry_exit_code, time_of_day, timezone, poll_seconds, timeout_seconds | keep (holds a step open, no service call) | RUN on 0.10.193 |
| schedule | `cron_schedule` | schedule | cron_expression, job_name, asset_keys | keep | RUN on 0.10.193 |
| `DockerOperator` | `docker_container_asset` | asset (long-running job) | image, command, env_vars, network | keep | READ and the document validated (a daemon is needed to run it) |
| S3 list, copy, delete, create object, upload | `s3_list_keys_asset`, `s3_copy_object_asset`, `s3_delete_objects_asset`, `s3_create_object_asset`, `s3_upload_file_asset` | resource-shaped (S3 API calls) | bucket, key, `resource_key` | move to resource + plain Python (`S3Resource`, the existing helpers, inside a step) | READ; the S3 calls are RUN by the Python-route tests |
| `SQLExecuteQueryOperator` (not Snowflake) and the database-specific operators | `sql_asset` | resource-shaped | connection_env, sql_statements, parameters | move to plain Python in a step: the registry database resources (`postgres_resource` ...) need host, database and user, which the IR does not carry. This also lifts the old limits (autocommit with several statements, positional parameters, `split_statements` now convert) | RUN (SQLite) |
| `LambdaInvokeFunctionOperator` | `lambda_invoke_asset` | resource-shaped | function_name, region_name, payload | plain Python step with the runtime's `lambda_invoke` (no registry Lambda resource) | READ |
| `SSHOperator` | `ssh_asset` | resource-shaped | host/user/password env vars, commands | plain Python step on `SSHResource` from `resources.py` (no registry ssh resource; `sftp_resource` is file transfer) | READ |
| `BigQueryInsertJobOperator` | `bigquery_query_asset` | resource-shaped | query, project_id, location | plain Python step on `BigQueryResource` (`bigquery_ddl_resource` has no query-job call, only datasets, tables and value checks) | READ |
| `HttpOperator`, `SimpleHttpOperator` | none (Python only) | resource-shaped | | new: `http_resource` (0.10.193 `request()`) + plain call | RUN |
| `DatabricksRunNowOperator` | none | resource-shaped | | new: `databricks_jobs_resource.run_now_and_wait` | RUN (fake SDK client) |
| `SparkSubmitOperator` | none | resource-shaped | | new: `spark_submit_resource.submit` | RUN (stand-in binary) |
| `S3CreateBucketOperator`, `S3DeleteBucketOperator`, `GCSCreateBucketOperator`, `GCSDeleteBucketOperator` | none | resource-shaped | | new: `s3_bucket_resource`, `gcs_bucket_resource` (`ensure_bucket`, `delete_bucket`) | RUN (fake clients) |

What changed: the nine asset components for service calls are gone from `convert_ir.py`; a service-calling task is an asset in `defs/<dag>_steps.py` (`required_resource_keys`, `deps` on other keys, retry policy, plain call); registry resources are written once to `defs/registry_resources/defs.yaml`; `report.json` adds `resource_types` and `registry_resources` next to `component_types`; `converted_requirements.txt` carries `dagster-community-components>=0.10.193` and what each resource imports. `http_resource`'s base URL is `{{ env.<CONN>_BASE_URL }}` (checked: the loader resolves it); its credentials are not written because an unset variable named in a resource field fails the run, so the caveat tells the user to add them. Not changed: the default Python output, which already called services with plain Python on `resources.py` resources and the env shims.

Resources of 0.10.193 that were read and loaded in a scratch project but have no operator mapping yet: `emr_resource`, `dataproc_resource`, `pubsub_resource`, `bigquery_ddl_resource`, `athena_resource`, `redshift_data_api_resource`, `dbt_command_resource` (the Python route stubs those operators and names the resource in the hint). Announced and not verified: none.


## Stage 2a, pass 5: fresh1 gaps (DAG-factory closures, Dataset objects, asset_state_store, callbacks, stub-matches-failure)

**fresh1 and stage 2a are TRAINING data for the converter now.** The gaps below were found by reading the fresh1 held-out run (60 DAGs); fixing them makes fresh1 a training set, so the fresh1 "after" numbers are NOT a generalisation estimate. A new held-out sample is needed for that. Each item is a general rule with an executed test in `scripts/tests/test_converter_stage2a_v8.py` (it converts an IR, often one extracted by real Airflow, and RUNS the emitted Dagster code); no DAG-specific case exists.

| Item | Before | Now | Not preserved / loud when |
|---|---|---|---|
| DAG-factory closure variables (`def create_dag(dag_id, dag_number): @dag ... def f(): @task def t(): ... dag_number`) | `unresolved name 'dag_number'` stub | The extractor records, per generated DAG, each free variable of a callable whose value is a plain literal (`callable.closure`, Python literal text, so tuples and sets keep their type); the converter writes them as constants in that DAG's module. Closure names win over module-level names, as in Python | A value that is not a literal (an object, a connection) is not recorded: the stub still names the name |
| `Dataset` / `Asset` objects in callables | `argument "Dataset(uri=...)" cannot be serialised`; `from airflow import Dataset` was not a shim import | The IR records an object passed as an argument (`asset_obj`: class, name, uri, extra); the runtime's `Asset` / `Dataset` are plain classes (`uri`, `name`, `group`, `extra`, Airflow's repr and equality; one of name / uri defaults the other); `from airflow import Dataset` is a shim import. Outlet / inlet key mapping is unchanged | Asset watchers and `AssetAlias` resolution beyond what pass 3 did |
| Airflow 3.4 `asset_state_store[asset]` | stub: context name not rebuilt | Plain-Python shim: JSON file per asset URI (else name) under `AIRFLOW_ASSET_STATE_DIR`, `get(key, default=)`, `set`, `delete`, `clear`; persists across runs | Not a Dagster store: point the directory at durable storage; no locking between concurrent runs |
| Task callbacks | `on_failure/success/retry` through `@on_hooks`; the whole set was dropped when `on_execute_callback` was present; a notifier instance had no source and was dropped; `ti.state` was refused | `on_execute_callback` is called at the start of each attempt; notifier instances (`BaseNotifier` subclasses) are copied with a plain `BaseNotifier` shim and an instance rebuilt from its attributes; `ti.state` (and `dag_run.state`) exist (`running`, `success`, `failed`, `up_for_retry`); an unsupported kind no longer drops the supported ones; a retry delay of 0 is 0 (it was 300) | `on_skipped_callback`, `pre_execute`, `post_execute` (no Dagster equivalent); Airflow's transient states (`scheduled` in `dag.test`); notifier `template_fields` are not rendered; callbacks in the graph form |
| DAG callbacks | degraded caveat, nothing emitted | `on_success_callback` / `on_failure_callback` become one `dg.run_status_sensor` per kind on the DAG's job, calling each function with a minimal context (`dag`, `dag_run`, `run_id`, `logical_date`, `ds`, `ts`, `reason`, `exception` = the run failure message) | Sensor daemon required; runs after the run, not inside it; no task instance in the context; `sla_miss_callback` is reported as degraded (use freshness checks or an alert policy) |
| `FanOutMapper` / `RollupMapper` over runtime partitions (stretch) | deferred: "only the identity mapping is converted" | **Still deferred**, with a message that says what the mapper does and what is missing. Dagster 1.13 has no built-in mapping between dynamic partition sets of different keys; a custom `dg.PartitionMapping` plus a sensor that registers the downstream keys would work but depends on how the mapper derives keys, and was not built or run | `scatter_live_region_to_segments`, `segment_region_stats_early_rollup` still `no-output` |
| Harness: a loud stub that fails scored `matches-baseline-failure` | counted as a success in the either-form share | `behavior_compare.py` marks a route whose failing step is a `AIRFLOW-CAVEAT` stub with `rollup.detail = "stub-matches-failure"`; `eval_report.py` reports it as its own verdict, outside the either-form share. The harness also recomputes each DAG's stratum from the current converter report (`behavior.json` has `stratum` and `manifest_stratum`; the report uses the first) and converts DAGs the manifest called deferred | |

Measured with `eval_behavior.py batch --routes converter` (work dirs `work_fresh1b`, `work_v8`; IRs re-extracted; converter output `conv_fresh1b`, `conv_stage2a_v8`):

| verdict (converter) | fresh1, before (60 evaluated) | fresh1, after (TRAIN) | stage 2a v7 (57) | stage 2a v8 (57) |
|---|---|---|---|---|
| equivalent (strict) | 45 | 50 | 37 | 37 |
| equivalent-with-differences | 4 | 4 | 8 | 8 |
| matches-baseline-failure | 2 | 1 (+1 `stub-matches-failure`) | 6 | 6 |
| different | 7 | 2 | 4 | 4 |
| no-output | 2 | 2 | 2 | 2 |
| equivalent in either form (excluding stub-matches-failure) | 51 (the before figure counted the stub row) | 55 | 51 | 51 |

fresh1 changes: `upstream_datasets_taskflow_toy`, `downstream_datasets_taskflow_toy`, `example_asset_state_store_producer`, `loop_hello_world_3`, `variable_hello_world_1`: different to equivalent; `selected_quotes`: `matches-baseline-failure` to `stub-matches-failure` (its callable uses a context name the runtime does not rebuild: a stub, not a success). `callbacks_overview` stays equivalent-with-differences: its callbacks now run, and the remaining stdout difference is the state Airflow's `dag.test` shows to `on_execute_callback` (`scheduled`; we set `running`) plus the DAG-level callbacks running in a sensor, which the harness does not start. Stage 2a: no DAG changed (no regression). Still `different` in fresh1: `example_python_operator`, `example_python_decorator` (the Dagster environment lacks `httpx2` and `colorama`; not worked around). Still `no-output`: the two FanOutMapper / RollupMapper DAGs.


## Stage 2a, pass 6: fresh2 gaps (harness fixes and converter fixes, counted separately)

**fresh2, fresh1 and stage 2a are ALL TRAINING data for the converter now; the held-out pool is exhausted.** Pass 6 was written by reading the fresh2 run (82 DAGs), so the fresh2 "after" numbers are NOT a generalisation estimate, and neither are fresh1 or stage 2a. A new held-out sample from a corpus that was not read is needed for that. Each item is a general rule with an executed test in `scripts/tests/test_converter_stage2a_v9.py` (a converter test converts an IR, written by real Airflow where the claim is about what the extractor records, and RUNS the emitted Dagster code; a harness test runs `run_dagster.py` or the comparison functions). No DAG-specific case exists.

Harness (`scripts/parity/run_dagster.py`, `behavior_compare.py`, `scripts/eval_behavior.py`); the rules are in `reference/parity.md`, section 4:

| Item | Before | Now |
|---|---|---|
| (a) dynamic partition set | `get_partition_keys()` without an instance: `CheckError`, the route "could not be executed" | the harness runs with an instance and registers the key `harness_partition` when a dynamic set is empty (a multi-partition set too); `partition_key` is recorded |
| (b) loop groups | Airflow's `group.task` / `group.<gate>` had no asset: `different` | the converter report lists them (`loop_members`, also in `task_keys`) mapped to the loop's step (to the consumer's asset when the loop was folded into it); the harness treats them as folded |
| (c) folded chain, leaf failure | the final task (`upstream_failed` in Airflow) against the failed asset: `different` | translated like the folded-skip case, only when a folded step of that asset failed in Airflow |
| (d) wall-clock strings | two Airflow runs in the same second made a `date` value look deterministic: a `values` difference | a value that differs from an EARLIER baseline run is nondeterministic; a pair equal after masking wall-clock timestamps near the Airflow run time is not compared |
| (e) `dag_run.partition_key` | `dag.test()` prints `None`, the route the key: a `stdout` difference | decision: the key is read as `None` in the route's printed lines (the rest of the line is still compared; evidence `partition_key_read_as_None`); running the route unpartitioned is not possible |
| baseline classification | a `dag.test()` limit counted as the DAG failing by design | `baseline-failed` (excluded from the shares, listed): deferrable task, unregistered inlet asset (`ASSET_NOT_FOUND`), async state-store call (`asend`) |

Converter (`scripts/convert_ir.py`, `airflow_ir.py`; `registry_map.py` and the registry resource code paths were not touched):

| Item | Before | Now | Not preserved / loud when |
|---|---|---|---|
| (f) `NEVER_EXPIRE`; the task state store | stub: callable depends on an Airflow import | `NEVER_EXPIRE` shim; `set(..., retention=)` accepted; async `aget` / `aset` / `adelete` / `aclear`; a retry reattaches from the store (sync and async tasks, both tested) | state is a file per dag, task, run and map index, never expired; no locking |
| (g) `task.render_template` | stub: the runtime's task does not provide it | `_TaskShim.render_template(content, context)` renders strings and strings in lists, tuples, sets and dicts; also `task.dag_id`. `templates_exts`: a TaskFlow argument naming a `.sql` file receives the file's rendered text (the extractor reads `op_kwargs` / `op_args` strings, key `op_kwargs.<name>`) | a template file outside the DAG folder and `template_searchpath` |
| (h) module-level `ObjectStoragePath` | `'base' (used by the callable) depends on Airflow`: the check read the word `airflow` in the bucket name `aws_default@airflow-tutorial-data` | the check reads code (names, imports, dotted `airflow.x` strings), not words in strings (`mentions_airflow`); the assignment is copied as a lazy path (`af_lazy_path`), built on first use, so an unset connection fails the task that uses it, not the import | `isinstance` on the path; a path whose value is not a literal call |
| (i) callables that take `context` | stub: context names the runtime does not rebuild `['context']` | `context` is the dict `get_current_context()` returns, now with `var` and `conn` added; an `@asset` function's inlet parameters are `Asset` objects. A `@task` function cannot take a parameter named `context` in Airflow (only `**context`), so that case does not arise | `DagRun`, `TaskInstance`, `provide_session`, a session: loud stubs naming the call and the Dagster equivalent; an inlet's URI is not known (the `Asset` carries the name) |
| (j) dict key order through `xcom_push` | Dagster's JSON metadata sorts keys: `key0, key1, key10, key11, key2 ...` | a text copy of the pushed order sits beside the metadata and a pull reads it back (also for another run's value) | JSON values only |
| (k) deferrable operators | `operator class uses the Airflow API (a direct airflow reference)` (the check matched `xcom_push` on `context["ti"]`) and `without a execute method` | **converted to plain Python, run in place:** `self.defer(trigger, method_name, kwargs)` drives the trigger's `run()` to its first event and calls `method_name(context, event=payload, **kwargs)`; `start_from_trigger` with `start_trigger_args` and no `execute` starts the trigger from them (the `trigger_cls` string is resolved to the class in the operator's module); `BaseTrigger`, `TriggerEvent`, `StartTriggerArgs`, `sync_to_async` (no package needed) and `conf` (`AIRFLOW__...` environment) are shims; the extractor keeps `deferrable=True` (stored as `_defer`). Loud with a clearer message: no `execute` and no `start_trigger_args` (Airflow raises `NotImplementedError`), `self.xcom_push` / `self.xcom_pull` in an operator class | No triggerer and no freed worker: the step waits, and a trigger that never yields blocks it. Copying a class needed its dependencies by method (a method's parameters are not dependencies) and by annotation (a copy evaluates them) |

Measured with `eval_behavior.py batch --routes converter --jobs 2` (work dirs `work_fresh2b`, `work_fresh1c`, `work_v9`; IRs re-extracted; converter output `conv_fresh2b`, `conv_fresh1c`, `conv_stage2a_v9`; the specs' `arguments` refreshed from the new IRs):

| verdict (converter) | fresh2 before (82) | fresh2 old converter, NEW harness | fresh2 after (82) | fresh1 before (60) | fresh1 after | stage 2a before (57 + 3 baseline-failed) | stage 2a after |
|---|---|---|---|---|---|---|---|
| equivalent (strict) | 59 | 65 | 69 | 50 | 50 | 37 | 39 |
| equivalent-with-differences | 7 | 2 | 3 | 4 | 4 | 8 | 7 |
| matches-baseline-failure | 2 (+4 `stub-matches-failure`) | 2 (+2 stub) | 3 | 1 (+1 stub) | 2 | 6 | 7 |
| different | 8 | 6 | 0 | 2 | 2 | 4 | 2 |
| no-output | 2 | 2 | 2 | 2 | 2 | 2 | 2 |
| baseline-failed (excluded from the shares) | 0 | 3 | 5 | 0 | 0 | 3 | 3 |
| strict / evaluated | 59/82 | 65/79 | 69/77 | 50/60 | 50/60 | 37/57 | 39/57 |
| equivalent in either form (excluding stub) / evaluated | 68/82 | 69/79 | 75/77 | 55/60 | 56/60 | 51/57 | 53/57 |

Where the fresh2 verdict changes come from (the middle column is the pass-5 converter's own output run through the pass-6 harness, so the first list is the harness alone and the second is the converter on top of it):

- **Harness fixes alone (9 DAGs):** `daily_inference`, `daily_team_a_rollup`, `monthly_team_a_rollup`, `trailing_week_inference` (equivalent-with-differences to equivalent: the partition key read as `None`); `tutorial` (equivalent-with-differences to equivalent: wall-clock value); `summarize_live_region_stats` (different to equivalent: dynamic partition key); `example_kubernetes_executor` (different to `baseline-failed`: its baseline `FileNotFoundError` matches the environmental list, and whether it does depends on the traceback being inside the logged tail, so the DAG moves between `matches-baseline-failure` and `baseline-failed` between runs; in the run where it was scored, the folded-chain translation made `tasks` and `failure` match); `asset2_producer` (stub-matches-failure to `baseline-failed`: `ASSET_NOT_FOUND` in `dag.test()`); `example_task_state_store_async` (stub-matches-failure to `baseline-failed`: `asend`). Of these 9, 6 are real equivalence the old harness could not see, 3 are DAGs moved out of the shares.
- **Converter fixes (8 DAGs, on top of the new harness):** `example_task_state_store` (different to equivalent: `NEVER_EXPIRE` and the store), `fixed_task_loop`, `partial_override_loop`, `refine_estimate` (different to equivalent: the report names the loop's inner tasks; the harness mapping needs the report), `tutorial_taskflow_templates` (different to equivalent-with-differences: `render_template` and the template file; the remaining differences are Airflow's `manual__...` run id against a UUID and the printed context dict), `tutorial_objectstorage` (different to `matches-baseline-failure`: the module-level path converts; both routes then fail on the harness's canned HTTP response, which has no `hourly` data, so this is agreement on a failure, not a pass), `datasetalias_deferrable` and `direct_deferrable` (stub-matches-failure to `baseline-failed`: both are now converted to plain Python and run; the Airflow baseline fails in `dag.test()` because it cannot run a deferred task, so the pair leaves the shares and the conversion is not scored; the converted code runs in the unit test).
- **fresh1:** no verdict changed except `selected_quotes`, `stub-matches-failure` to `matches-baseline-failure` (the callable is now converted instead of a stub: converter). No regression. **Stage 2a:** `deltalake_example` different to `matches-baseline-failure` (harness: the folded-chain translation), `example_dag` equivalent-with-differences to equivalent (harness: earlier-baseline-run rule), `example_bash_decorator` different to equivalent (converter: a URL containing `airflow.apache.org` in a string was read as an Airflow reference). No regression.

The share changes are inflated by exclusion: 5 of fresh2's 82 DAGs are now `baseline-failed` (3 harness, 2 where the converter now converts what Airflow's own harness cannot run), which shrinks the denominator (69/77 against 59/82). Counted on the same 82 DAGs, the fresh2 strict count goes from 59 to 69 (65 harness alone). Two DAGs (`aggregate_regional_sales`, `segment_region_stats_rollup`) are still `no-output` (FanOutMapper / RollupMapper).

Not done: the fresh1 pair `example_python_operator`, `example_python_decorator` (the Dagster environment lacks two packages, not worked around); FanOutMapper / RollupMapper over runtime partitions; the deferrable operators were run in place, never against a triggerer; a held-out measurement.

## Burn-down, harness

Classes H1..H6 and E1 of the 242-DAG burn-down are harness or environment artefacts, not converter gaps. What was done and what is left (re-measured on the 25 DAGs concerned, converter route, with `--conv-dir` set to a fresh folder; the details are in [parity.md](parity.md)):

| class | finding | action | result on re-measure |
|---|---|---|---|
| H1 duplicate dag id (4) | the spec, the kit and the converter picked a DAG by id only: Airflow ran `exercises/`, the converter converted `solutions/` | choose by manifest `file` everywhere; the converter gets an IR with the row's file for such ids | `top_level_code`, `launch_readiness_report`, `launch_window_history` are `equivalent`; `planet_conditions` stays `different` (real gap: `dag_run.run_after` missing in the runtime context, timeout) |
| H2 virtualenv / external python (5) | the baseline cannot build the environment offline; no wheelhouse exists, and the DAGs pin `numpy~=1.24.4` / `~=1.26.0` | not fixable by a stand-in without installs; stays `baseline-failed` with the reason `environmental (virtualenv-build)` and the requirements | unchanged (`baseline-failed`). Needs from the user: a wheelhouse or index for those pins on an interpreter that supports them (1.24.4 has no wheel for the 3.12 Airflow venvs) |
| H3 deferrable (2), H5 `dag.test()` limits (3) | Airflow's own harness cannot run them (no triggerer; asset lookup; in-process supervisor without `asend`; a Kubernetes pod mount) | documented limits, classified `baseline-failed` | unchanged |
| H4 `include/` missing (2) | harness bug: a symlinked `repos/` made `local_deps` find nothing, so the sandbox working directory was empty | resolve the root first | both baselines run now; both sides then fail on the S3 bucket the DAG assumes, so the verdict is `matches-baseline-failure` (they left the excluded set) |
| H6 stdout (2) | harness bug: a logger-prefixed line (`CRITI [airflow.task]`) and a long line cut at different points | normalise both | `UI_dag`, `toy_xcom_big_vs_small`: `equivalent` |
| E1 route environment (2) | the route's Dagster venv lacks `httpx2` (the converted requirements list it) and `dagster_snowflake` | verdict not moved; the report lists `route_missing_modules` | install needed in the `dg` venv: `httpx2`, `dagster-snowflake`, and `pyarrow` (`deltalake_example`) |

MB-genuine (12 by design) keep `matches-baseline-failure` and are reported apart from the successes with whether the two failures share an exception class; a route whose failing step is a stub or a converter `NotImplementedError` is `stub-matches-failure` (on re-measure `selected_quotes` moved there: its failing step is the converter's own refusal, not the DAG's failure). The 80 thin strict equivalents are listed per DAG with what was not observable. `reference/verdict-snapshot.json` plus `scripts/verdict_snapshot.py check` guards all of this against regressions.

## Burn-down, converter

Converter-side classes of the 242-DAG burn-down (F-BR, F-CTX, F-XPULL, F-POOL, F-TPL, F-MTG, F-MAPARG, F-OPX, F-DEP, F-OBJ, F-CB, F-PM), fixed generally and each run by `scripts/tests/test_converter_burndown.py` (real Airflow parses a small DAG, the converter converts what the extractor wrote, the emitted Dagster code runs). Each DAG below was re-run alone through the converter and `eval_behavior.py one` (converter route, new work dirs `work_bd*`); the 242 were not re-measured. The verdicts on `train` DAGs are training numbers.

| class | cause | change | DAGs re-run alone (before -> after) |
|---|---|---|---|
| F-BR (6) | the runtime wrapped the choice in an object, so the asset value was a repr | a branch asset returns a `str` / `list` subclass (the callable's value), a short-circuit the plain `bool`; `af_gate(..., sc=)` reads short-circuit-ness from the graph. **Converter representation, not harness** | `example_short_circuit_decorator`, `task_group_branching`, `example_params_trigger_ui`: equivalent-with-differences -> equivalent |
| F-CTX (5) | names the runtime did not rebuild | `test_mode`=False, `task.params`, Airflow-style `run_id`, one logical date per manual run, mapped task inside a task-group loop (`loop`, `ti.map_index`) | `mapped_task_loop`: different -> equivalent; `tutorial_taskflow_templates`: -> equivalent; `example_passing_params_via_test_command`: -> matches-baseline-failure (the DAG's own failure). `my_dag_task_flow` stays a stub (an `AwsGenericHook` provider hook with no shim). `clean_and_combine_player_stats`: converter side done, the remaining difference is harness (below) |
| F-XPULL (3) | `ti.xcom_pull` by task id stubbed in graph form; a failed member of a mapped group made the whole group `upstream_failed` | the pulled tasks' values are wired into the op (`map_indexes`, Airflow 2 `LazySelectSequence` repr); a failed group member is a value with its own state; `AfTaskRef` for task objects a callable names; every op declares its `retry_policy` | `task_group_mapping_toy`: different -> equivalent; `task_group_mapping_and_pulling_usecase`: different -> matches-baseline-failure; `example_complex` (gsd): different -> equivalent-with-differences (retries, pools: harness) |
| F-POOL (2) | **harness**: `pool_of` / `retry_of` in `parity/run_dagster.py` aggregate every op of a graph asset, so one pooled op marks all assets; `max_active_tis_per_dag` is a Dagster pool named `<dag>__<task>`, not an Airflow pool | converter output already applies the pool per op (`pool_example`: one op) | for the coordinator: attribute pools and retries per op (op name `<dag>__<task>`, the output's `airflow_task` metadata), and do not count the `<dag>__<task>` pool of `max_active_tis_per_dag` as a pool difference |
| F-TPL (2) | a `templates_dict` file was never read, a missing file never failed | the file is read at extraction and rendered; a missing file fails the task with `TemplateNotFound` before the callable runs | `example_python_operator`: different -> equivalent-with-differences (the remaining difference is a printed context dump; `virtualenv_python` needs `colorama`, E1) |
| F-MTG (1) | a trigger rule on a member of a mapped group made the group a stub | allowed now (fail-aware graph, gate on the member's inputs) | `complex_dag_structure_rainbow`: different -> equivalent |
| F-MAPARG (1) | `expand(op_args=..., op_kwargs=...)` deferred | per-item args and kwargs, cross product in Airflow's order (the DAG's own comment lists another order; real Airflow 2.10 records this one) | `2_4_example_toy_dag_mapping_multiple_parameters`: no-output -> equivalent |
| F-OPX (1) | a class that only overrides `__init__` was treated as a custom operator without `execute` | it is the stock operator it extends (the extractor records the base classes) | `openlineage_base_complex_dag`: different -> equivalent-with-differences (pools: harness; one stdout line) |
| F-DEP (1) | product folding merged steps with different upstreams into one asset | a step folds only when every upstream of the host asset is an Airflow ancestor of the step | `apache_airflow::example_complex`: equivalent-with-differences -> equivalent |
| F-OBJ (1) | `file://include/bears` was read as `/bears`, and the extractor stored an absolute path | relative to the task's working directory; the extractor records the path as written; Airflow 2 `replace` of a relative target is a sibling | `producer_dag`: different -> equivalent |
| F-CB (1) | `callbacks_overview` prints `ti.state` as `scheduled` in Airflow's `dag.test()` and `running` here | **left degraded**: a `dag.test()` quirk of Airflow (a real run shows `success` / `failed`), not worth mimicking; `sla_miss_callback` has no Dagster form | unchanged: equivalent-with-differences |
| F-PM (6) | deferred partition mappers on runtime-partitioned assets | **left deferred**: Dagster has no mapping between dynamic partition sets, so each of Product, AllowedKey, FanOut and Rollup needs a custom `PartitionMapping` plus a sensor that derives the downstream keys from Airflow's mapper rule; not built or run on 1.13 in this pass. The deferral message names the mapper and that equivalent | unchanged |

Strict-equivalent DAGs that still carry a stub or degraded caveat: `example_bash_operator` (a graph-form `xcom_pull` stub) and `run_if_skip_if_example` (a spurious `pre_execute` caveat) are clean now. Six asset-sharing DAGs (one producer per asset in Dagster), `downstream_on_alias*` (an alias the extractor cannot resolve) and `continuous_toy` (a schedule every minute) are by design; `example_branch_dop_operator_v3` keeps `depends_on_past` degraded in the graph form (the step-event guard is built for the asset form; not built for graph ops).

Behaviour change worth knowing: the graph form is fail-aware when a mapped task group exists (a failed member is a value, the run fails at the end, as Airflow's leaves decide it), so the failing item no longer shows as a `STEP_FAILURE` on its op; the state is on the member's asset (`airflow/state: failed`). Retry policies are now declared on every graph op, also in fail-aware graphs.
