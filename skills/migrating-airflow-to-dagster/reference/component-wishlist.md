# What the components need to do (handoff to the components session)

The converter turns existing orchestrator DAGs into Dagster definitions. It owns the translation: it reads the DAG, works out what each part does, and writes generated code. This file lists what the components could still do for it, in Dagster terms. None of it is specific to one orchestrator.

Measured on 292 DAGs from 14 public example repos: 284 convert and 8 are deferred (5 of those are runtime-partition mappings). About 225 of the 284 convert with nothing stubbed; about 60 contain at least one stubbed task. The corpus is mostly teaching examples, so use the ranking, not the percentages.

## What the converter does not need from you

These are built into the converter's generated runtime, so a converted project needs no component for them: fire-and-forget launch of another job, an SLA measured from the scheduled tick, running a callable in its own virtualenv or interpreter, mapped S3 and Snowflake steps, the setup and teardown scope, and the skip-aware gates. 0.10.164 has `trigger_job_asset` `wait: false` and `sla_asset` `start_from`; the converter can emit those components instead once you want converted projects to depend on the package.

## Delivered in 0.10.164 and 0.10.165, and what is left on the converter side

Everything the earlier version of this file asked for now exists. What remains is converter work, not component work:

| Capability | Component | Converter status |
|---|---|---|
| Asset whose partition key is chosen while it runs | `runtime_partition_asset` | The converter generates the same two-asset shape itself (the step is one asset, the dynamically partitioned catalog asset is an `AssetSpec` that receives `report_runless_asset_event` per key) and verified it with real Dagster runs: composite keys added with `add_partitions`, and a key taken from a run tag. Consumers with the identity mapping convert (eager automation on the same dynamic partition set). Rollup, fan-out, product and allowed-key mappings are not converted yet (5 DAGs) |
| Repeat a step until a condition | `loop_gate_asset` | Not wired: the extractor would need to read task-group loops (`max_iterations`, the `until` callable, the group's tasks). The component's `loop.index` and `loop.previous` now match Airflow 3.4 (index starts at 1, `previous` is the one preceding result), fixed in 0.10.166 on `main`. It still repeats one callable, not a whole task group, which is the remaining gap: an Airflow loop body is usually several tasks |
| S3 create object | `s3_create_object_asset` | The converter translates `S3CreateObjectOperator` itself (text or bytes, `replace`); compression, encryption and ACLs are stubbed |
| Read a non-direct upstream value by name | `get_upstream_value` | Does not fit graph-form DAGs: their values are op outputs and the assets are only materialized when the graph ends. Those pulls stay stubbed |
| Fire and forget launch, SLA from the scheduled tick | `trigger_job_asset` `wait: false`, `sla_asset` `start_from` | The converter has its own implementation in the generated runtime; it can emit the components instead |
| SQL column checks | `Enhanced Data Quality Checks` | Needs the components output mode (write the component YAML from the operator's column mapping). If `distinct_check`, or tolerances on `equal_to`/`geq_to`/`leq_to`, have no counterpart there, that is the only request |

Human-in-the-loop operators inside a run (pause, wait for a person's choice, continue) are not something Dagster can do today; the converter stubs them with a caveat and asks for nothing.

## Settings that components need to expose

`retry_policy_*`, `pool` and `timeout_seconds` landed in 0.10.163. Everything else an asset needs (automation condition, partitions, metadata, owners, tags, kinds, group, freshness policy, code version) is set through `post_processing` in `defs.yaml`, which already works for every component. Nothing further is needed here.

## Low priority

- `automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`.
- `k8s_job_asset` has no Pipes variant for streamed logs.
- Timeouts abandon the thread at the deadline and the work continues; say so in each README that offers one.

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.
