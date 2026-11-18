# Dynamic task mapping

`expand` / `expand_kwargs` run one task instance per item discovered at run time (files, tables, tenants). Two Dagster shapes cover it; pick by whether the items are known when you write the code.

| Items | Dagster | Example |
|---|---|---|
| Known list that rarely changes (countries, regions) | `StaticPartitionsDefinition`: one asset partitioned by the item, one schedule that requests every partition | `examples/dagfactory_family/` |
| Discovered at run time (files that landed, new tables) | `DynamicPartitionsDefinition`: a schedule or sensor lists the items, registers each as a partition and requests a run per partition | `examples/s3_to_snowflake_factories/example_04_per_file/` |

Not N assets. One asset per data product, partitioned by the varying value.

## What `convert_ir.py` converts automatically

Two modes, chosen with `--mapping`.

**`graph` (default): dynamic outputs in one graph asset.** A DAG with a branch or short-circuit task is also converted this way, with or without mapped tasks (see `reference/mapping.md`). The whole DAG becomes one `@dg.graph_multi_asset` with one op per task. A mapped task is a mapped op fed by a fan-out op (`DynamicOut`); a task after it receives the collected list (a reduce step); a mapped task over another mapped task's output chains the maps. This keeps what the Airflow DAG has: one run, siblings that finish before the next step, all-or-nothing for the reduce step, and the same task order. Supported:
- Python tasks and shell tasks (`BashOperator`, `@task.bash`); no other operator type may be in a DAG that has mapped tasks yet.
- `expand` over literal lists or another task's plain return value, one argument or several (several is a cross product, built by the fan-out op); `expand` over `op_args`; `expand_kwargs` over a literal list or a task's output.
- For a mapped shell task, the expanded `bash_command`, `env` and `cwd` replace the fixed ones; templates inside the expanded values are not rendered (the task fails with a message).
- Tasks that do not pull XComs by task id and are not asset-triggered.

Differences to state: retries and pools apply to each op; one task cannot be re-run on its own (the asset is the unit); an empty list skips the mapped step and gives the next step an empty list.

**`partitions`: leaf mapped tasks as partitions** (use when each item should be independently runnable, re-runnable and visible, for example one file or one customer). Leaf mapped Python tasks over a literal list become static partitions with a schedule that requests each one. Leaf mapped tasks over one root producer's return value become dynamic partitions: the producer adds the item keys to its materialization metadata, and an asset sensor registers the new keys and requests one run per item. Keys: a short string is its own key, a small value is `json:<JSON>`, anything longer is `sha1:<digest>`; identical items collapse. No reduce step is supported in this mode.

Everything else defers with a named reason: an S3 or Snowflake operator inside the mapped DAG, mapping over `expand` arguments of an unsupported kind, and cross products over a mapped task's output.

Airflow's `.map(func)` (a function over each item of an upstream result before expanding), `.zip(a, b)` (pairs items; `fillvalue` uses `zip_longest`) and concat (joins several results) are converted in graph mode: the fan-out op builds the list as `[f(x) for x in src]`, `[*a, *b]` or `list(zip(a, b))` and the map functions are copied into the module. Partitions mode does not convert them.

Verified by test: graph mode (producer, mapped Python task with a fixed argument, a reduce step over the collected list, a chained second-order map, mapped shell commands, `op_args` and `expand_kwargs`) and partitions mode (producer, sensor, three key kinds, one run per partition finding its item, and an idempotent second tick). Not verified: the partitions sensor under a running daemon.

## The per-file design (verified in the worked example)

- The partition key is the item (the S3 key). Keys containing `/` and spaces work.
- The task chain per item (COPY, archive, delete) becomes assets partitioned by the same definition. Steps that are side effects, not data products (archive plus delete) fold into one asset.
- The "build the kwargs for each item" tasks disappear: the partition key is the item. Reuse the Airflow task's own filtering and naming logic as plain functions and compare them with the originals.
- A schedule evaluates at the Airflow cadence, lists the prefix, adds the new keys with `context.instance.add_dynamic_partitions(...)`, and returns one `RunRequest(partition_key=...)` per item (use the `@dg.schedule` decorator: the `ScheduleDefinition` constructor does not accept a list). Return a `SkipReason` when nothing landed.
- Idempotence: register only keys not already present; give each request a `run_key`.

## Differences to state in the report

1. **All-or-nothing becomes per-item.** In Airflow, a downstream mapped task with the default `all_success` rule waits for every upstream mapped instance. With one partition per item, a failed item stops only itself: in the example, a failed COPY left that file in the landing prefix and the other files were archived and deleted (tested). If all-or-nothing is required, it needs a different structure.
2. **Dynamic partitions accumulate.** Airflow's mapped instances are ephemeral; partitions persist. Plan to delete old ones (`instance.delete_dynamic_partition`) on a schedule.
3. **Re-running a day.** Airflow re-lists the prefix; the Dagster schedule registers only keys it has not seen. A file that was archived and deleted cannot be reloaded without putting it back.
4. **A sensor is the documented route for adding partitions** (`SensorResult(dynamic_partitions_requests=...)`; there is no `ScheduleResult`). The schedule above mutates the instance from inside the evaluation: verified in-process on a persistent local instance only, not in a deployed daemon. If that fails in your deployment, use a sensor that applies the same cron logic, or move to an event-driven sensor that registers files as they land (usually the better design anyway).

## Testing pitfalls

- `build_schedule_context(resources=...)` needs a persistent instance (`DagsterInstance.local_temp(...)`), not `DagsterInstance.ephemeral()`, or resource initialisation fails with "no instance reference".
- Pass `repository_def=defs.get_repository_def()` so job and partition validation can resolve, and bind the resources into those definitions.
- Evaluate a schedule with `schedule.evaluate_tick(ctx).run_requests`.
