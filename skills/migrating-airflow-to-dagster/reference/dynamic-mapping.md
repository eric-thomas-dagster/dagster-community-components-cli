# Dynamic task mapping

`expand` / `expand_kwargs` run one task instance per item discovered at run time (files, tables, tenants). Two Dagster shapes cover it; pick by whether the items are known when you write the code.

| Items | Dagster | Example |
|---|---|---|
| Known list that rarely changes (countries, regions) | `StaticPartitionsDefinition`: one asset partitioned by the item, one schedule that requests every partition | `examples/dagfactory_family/` |
| Discovered at run time (files that landed, new tables) | `DynamicPartitionsDefinition`: a schedule or sensor lists the items, registers each as a partition and requests a run per partition | `examples/s3_to_snowflake_factories/example_04_per_file/` |

Not N assets. One asset per data product, partitioned by the varying value.

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
